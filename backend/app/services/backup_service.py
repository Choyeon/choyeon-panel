import gzip
import os
import re
import shutil
import sqlite3
import sys
import tarfile
from contextlib import suppress
from pathlib import Path

from .. import backup_runner
from .. import config
from .. import database as dbm
from ..log import warn
from ..util import is_name, run, sh_escape, unit_env_value, unit_pct_escape
from .files_service import iso_ms
from .pg_service import is_ident, psql

BACKUP_DIR = config.BACKUP_DIR
BACKEND_DIR = str(Path(config.BASE, "backend"))
_VENV_PY = str(Path(BACKEND_DIR, ".venv", "bin", "python"))
PYTHON_BIN = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable

# 与 backups.kind 的 CHECK 约束保持一致（database.py 建表 + 迁移都按这份）
KINDS = ("pg", "app", "sqlite")


def _unit_base(bid: int) -> str:
    return f"panel-backup-{bid}"


def _unit_dir() -> str:
    """单元目录取 config.UNIT_DIR（CP_UNIT_DIR 可改），和 apps_service 用同一个开关。

    之前这里写死 /etc/systemd/system：改了 CP_UNIT_DIR 的人，应用 unit 跟着走、
    备份 timer 还留在原地，两套位置谁也说不清哪个是权威。运行时读，方便测试重定向。
    """
    return config.UNIT_DIR


def _target_missing(b: dict) -> bool:
    """app / sqlite 类备份的目标必须还在 apps 表里。

    应用被删除后备份行不会跟着走，timer 每晚跑一次 backup_runner 就 SystemExit 一次，
    而面板上这条任务依旧显示"已启用"——只有翻 journalctl 才发现备份早就不产出了。
    """
    if b["kind"] == "app":
        return not dbm.query_one("SELECT 1 FROM apps WHERE name=?", (b["target"],))
    if b["kind"] == "sqlite":
        t = str(b["target"] or "")
        if t.isdigit():
            return not dbm.query_one("SELECT 1 FROM apps WHERE id=?", (int(t),))
        return not dbm.query_one("SELECT 1 FROM apps WHERE name=?", (t,))
    return False


def list_backups() -> list:
    rows = dbm.query("SELECT * FROM backups ORDER BY id")
    out = []
    for b in rows:
        b["dir"] = BACKUP_DIR
        b["target_missing"] = _target_missing(b)
        if os.path.isdir(BACKUP_DIR):
            files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"bk{b['id']}-"))
            files = list(reversed(files))[:30]
            entries = []
            for f in files:
                try:
                    st = os.stat(f"{BACKUP_DIR}/{f}")
                    entries.append({
                        "name": f,
                        "size": st.st_size,
                        "mtime": iso_ms(st.st_mtime),
                        # 面板数据目录的备份（bk0-/panel-）与 .sql.gz 走不了自动恢复，
                        # 前端据此把"恢复"按钮置灰，而不是让人点了才知道不行。
                        "restoreable": _restoreable(b["kind"], f),
                    })
                except OSError:
                    continue
            b["files"] = entries
        else:
            b["files"] = []
        out.append(b)
    return out


def _write_if_changed(path: str, content: str):
    if os.path.exists(path) and Path(path).read_text() == content:
        return
    Path(path).write_text(content)


async def _sync_timer(b: dict):
    u = _unit_base(b["id"])
    unit_dir = _unit_dir()
    svc = f"{unit_dir}/{u}.service"
    tmr = f"{unit_dir}/{u}.timer"
    if b["schedule"] == "manual" or not b["enabled"]:
        await run("systemctl", ["disable", "--now", f"{u}.timer"], timeout=30)
        for f in (svc, tmr):
            if os.path.exists(f):
                os.unlink(f)
        await run("systemctl", ["daemon-reload"])
        return
    tz = None
    # /etc/timezone 在 RHEL 系与容器里常不存在，读不到属于正常情况
    with suppress(OSError):
        tz = Path("/etc/timezone").read_text().strip() or None
    if not tz:
        link = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in link:
            tz = link.split("/zoneinfo/")[1]
    tz = tz or "UTC"
    cal = f"{'Mon ' if b['schedule'] == 'weekly' else ''}{int(b['hour']):02d}:{int(b['minute']):02d} {tz}"
    data = config.DATA_DIR
    # 生成 unit 时的三处转义，都是沙箱真 systemd 实测出来的问题（CP_* 路径来自 .env / 环境变量，
    # 后端不做字符白名单，装了带空格或 % 的目录就会静默中招）：
    # - WorkingDirectory 不能加引号（加了直接报 "path is not absolute" 起不来），
    #   不加引号的空格反而没事；但 `%X` 会被展开成说明符，目录就不存在了 → unit_pct_escape。
    # - ExecStart 的二进制含空格、不加引号 → 203 起不来；引号包住即可，参数照常分词。
    # - Environment= 不加引号时值在第一个空格处截断（/tmp/sp dir/sub → /tmp/sp，
    #   备份写到别的目录且毫无提示），整条赋值用双引号包住；引号挡不住 `%` 展开
    #   （实测 /a%b 变成 /a<启动ID>），所以值还要 unit_env_value（转义 " 与 \、% 翻倍）。
    _wd = unit_pct_escape(BACKEND_DIR)
    _py = unit_pct_escape(PYTHON_BIN)
    _data = unit_env_value(data)
    _write_if_changed(
        svc,
        f"[Unit]\nDescription=choyeon-panel backup #{b['id']}\n[Service]\nType=oneshot\n"
        f"WorkingDirectory={_wd}\n"
        f'ExecStart="{_py}" -m app.backup_runner {b["id"]}\n'
        f'Environment="CP_DATA_DIR={_data}"\n',
    )
    _write_if_changed(
        tmr,
        f"[Unit]\nDescription=choyeon-panel backup timer #{b['id']}\n[Timer]\n"
        f"OnCalendar={cal}\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
    )
    await run("systemctl", ["daemon-reload"])
    r = await run("systemctl", ["enable", "--now", f"{u}.timer"], timeout=30)
    if b["enabled"] and r["code"] != 0:
        raise RuntimeError(r["out"])


def _validate_target(kind: str, target):
    """kind/target 的配对校验，create 与 update 必须共用同一份。

    旧写法只在 create_backup 里校验，update_backup 只查了 schedule/hour/minute/keep，
    于是 `PATCH {"target": "app;DROP DATABASE x;--"}` 能一路写进库。
    这个 target 后面会原样拼进 `su - postgres -c 'pg_dump -Fc <target>'`
    （backup_runner 里另有一道校验挡住了执行侧），但真正的问题是：
    坏值进了库，timer 每次跑都在 runner 里 SystemExit，面板上却显示"任务已配置"。
    kind 同样能在 PATCH 里改成表 CHECK 之外的值——那时抛的是 sqlite3.IntegrityError，
    面板路由的宽 except 会把它兜成 400，可是任何直接调服务层的地方（sync_all_timers、
    以后的 CLI）拿到的是一个未捕获的数据库异常，而不是业务错误。
    """
    if kind == "pg" and target != "all" and not is_ident(target or ""):
        raise RuntimeError("PG 库名不合法")
    if kind == "app" and not is_name(target or ""):
        raise RuntimeError("应用名不合法")
    if kind == "sqlite":
        # sqlite 的 target 存 app_id（应用改名后 name 型 target 会指向不存在的目录），
        # 同时兼容按应用名配置的历史写法。
        t = str(target or "")
        if t.isdigit():
            if not dbm.query_one("SELECT 1 FROM apps WHERE id=?", (int(t),)):
                raise RuntimeError("应用不存在")
        elif not is_name(t) or not dbm.query_one("SELECT 1 FROM apps WHERE name=?", (t,)):
            raise RuntimeError("SQLite 备份目标应用不存在")


def _validate_fields(i: dict):
    if i.get("schedule") is not None and i["schedule"] not in ("manual", "daily", "weekly"):
        raise RuntimeError("schedule 必须是 manual/daily/weekly")
    if i.get("hour") is not None and (not isinstance(i["hour"], int) or not 0 <= i["hour"] <= 23):
        raise RuntimeError("hour 必须在 0-23")
    if i.get("minute") is not None and (not isinstance(i["minute"], int) or not 0 <= i["minute"] <= 59):
        raise RuntimeError("minute 必须在 0-59")
    if i.get("keep") is not None and (not isinstance(i["keep"], int) or not 1 <= i["keep"] <= 365):
        raise RuntimeError("keep 必须在 1-365")


async def create_backup(i: dict) -> dict:
    if i.get("kind") not in KINDS:
        raise RuntimeError("kind 必须是 pg/app/sqlite")
    _validate_target(i["kind"], i.get("target"))
    _validate_fields(i)
    res = dbm.execute(
        "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) VALUES(?,?,?,?,?,?,?)",
        (
            i["kind"],
            i.get("target"),
            i.get("schedule") or "daily",
            i.get("hour") if i.get("hour") is not None else 3,
            i.get("minute") if i.get("minute") is not None else 30,
            i.get("keep") if i.get("keep") is not None else 7,
            # 与 update_backup 同一套真值判断。旧写法 `is False` 只认真假的 False：
            # 实测 create 传 {"enabled": 0} 会被记成 1，PATCH 传同样 payload 记成 0，
            # 同一个请求体在 POST/PATCH 下语义相反（JSON 里没有 Python 的 False 时，
            # 数字 0、空串是常见的"关"写法）。缺省仍然视为开启。
            1 if (i.get("enabled") is None or i["enabled"]) else 0,
        ),
    )
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (res,))
    if not row:
        raise RuntimeError("备份任务创建后未能读回，请检查数据库")
    try:
        await _sync_timer(row)
    except Exception as e:  # noqa: BLE001
        # timer 没落地却把行留在库里，列表就会显示一条"已启用但永远不会跑"的任务，
        # 而且用户完全没有线索。撤销这条半成品再报错。
        await _discard(row["id"])
        raise RuntimeError(f"备份计划落地失败，任务已撤销：{e}") from e
    return row


async def _discard(bid: int):
    """删掉一条 timer 未成功落地的备份任务（systemd 侧残留一并清）。"""
    # 清理尽力而为，最终以库里的行被删掉为准
    with suppress(Exception):
        await delete_backup(bid)
    dbm.execute("DELETE FROM backups WHERE id=?", (bid,))


async def update_backup(bid: int, i: dict) -> dict:
    cur = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not cur:
        raise RuntimeError("备份任务不存在")
    _validate_fields(i)
    merged = {
        "kind": i["kind"] if i.get("kind") is not None else cur["kind"],
        "target": i["target"] if i.get("target") is not None else cur["target"],
    }
    if merged["kind"] not in KINDS:
        raise RuntimeError("kind 必须是 pg/app/sqlite")
    # create 有、update 没有——这道不对称就是上面 _validate_target 说的漏洞，补齐
    _validate_target(merged["kind"], merged["target"])
    dbm.execute(
        "UPDATE backups SET kind=?,target=?,schedule=?,hour=?,minute=?,keep=?,enabled=? WHERE id=?",
        (
            merged["kind"],
            merged["target"],
            i["schedule"] if i.get("schedule") is not None else cur["schedule"],
            i["hour"] if i.get("hour") is not None else cur["hour"],
            i["minute"] if i.get("minute") is not None else cur["minute"],
            i["keep"] if i.get("keep") is not None else cur["keep"],
            cur["enabled"] if i.get("enabled") is None else (1 if i["enabled"] else 0),
            bid,
        ),
    )
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not row:
        raise RuntimeError("备份任务更新后未能读回，请检查数据库")
    try:
        await _sync_timer(row)
    except Exception as e:  # noqa: BLE001
        # 库里已是新值、systemd 还是旧 timer，两边不一致且界面上看不出来：
        # 把库还原成改动前那一行，再按同一口径报错。
        dbm.execute(
            "UPDATE backups SET kind=?,target=?,schedule=?,hour=?,minute=?,keep=?,enabled=? WHERE id=?",
            (cur["kind"], cur["target"], cur["schedule"], cur["hour"], cur["minute"], cur["keep"], cur["enabled"], bid),
        )
        # 还原时连 timer 也同步不上就是环境问题，别盖掉原始报错
        with suppress(Exception):
            await _sync_timer(cur)
        raise RuntimeError(f"备份计划更新失败，已还原为改动前的配置：{e}") from e
    return row


async def delete_backup(bid: int):
    # 不存在就报错，和 update_backup / delete_app 同一口径。旧写法对 999999 静默走
    # 一遍 systemctl disable + daemon-reload 再 DELETE 0 行，对外照样返回 {"ok": true}：
    # 链接失效、另一标签页已删，调用方都会以为删掉了。
    if not dbm.query_one("SELECT id FROM backups WHERE id=?", (bid,)):
        raise RuntimeError("备份任务不存在")
    u = _unit_base(bid)
    unit_dir = _unit_dir()
    await run("systemctl", ["disable", "--now", f"{u}.timer"], timeout=30)
    for f in (f"{unit_dir}/{u}.service", f"{unit_dir}/{u}.timer"):
        if os.path.exists(f):
            os.unlink(f)
    await run("systemctl", ["daemon-reload"])
    dbm.execute("DELETE FROM backups WHERE id=?", (bid,))


_SQLITE_NAME_RE = re.compile(r"^bk\d+-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?-(.+)\.sqlite\.gz$")


def _restoreable(kind: str, name: str) -> bool:
    """这个备份文件能不能走自动恢复。

    pg 的 .sql.gz 是 pg_dumpall 的整库文本档，恢复意味着重建集群里所有库和角色，
    绝不能由一个按钮触发；面板自身数据库（bk0- 之类）也不该被服务覆盖。
    """
    if kind == "sqlite":
        return name.endswith(".sqlite.gz")
    if kind == "app":
        return name.endswith(".tar.gz")
    if kind == "pg":
        return name.endswith(".dump.gz")
    return False


def _backup_file_path(bid: int, filename: str) -> str:
    """恢复只允许读取"本任务名下、真实落在备份目录里"的普通文件。

    filename 完全来自请求体：直接拼路径就是任意文件读取（`../../etc/shadow`），
    而且它会成为 tar 解包与 pg_restore 的输入。这里同时管住目录穿越与软链接跳出。
    """
    if not filename or filename != os.path.basename(filename):
        raise RuntimeError("备份文件名不合法")
    if not filename.startswith(f"bk{bid}-"):
        raise RuntimeError("该文件不属于这个备份任务")
    real = os.path.realpath(os.path.join(BACKUP_DIR, filename))
    if os.path.dirname(real) != os.path.realpath(BACKUP_DIR):
        raise RuntimeError("备份文件名不合法")
    if not os.path.isfile(real):
        raise RuntimeError("备份文件不存在（可能已被保留策略清理）")
    return real


def _sqlite_app(target):
    t = str(target or "")
    app = None
    if t.isdigit():
        app = dbm.query_one("SELECT * FROM apps WHERE id=?", (int(t),))
    if not app:
        app = dbm.query_one("SELECT * FROM apps WHERE name=?", (t,))
    if not app:
        raise RuntimeError("备份目标应用已不存在")
    if not os.path.isdir(app["path"]):
        raise RuntimeError(f"应用目录不存在：{app['path']}")
    return app


async def _sh_script(script: str, timeout: int = 1800) -> dict:
    """bash + pipefail 执行整段脚本，返回 {code, out}（pipefail 才不会吞管道左侧失败）。"""
    return await run("/bin/bash", ["-o", "pipefail", "-c", script], timeout=timeout)


async def _restore_sqlite(bk: dict, src_file: str, log: list) -> None:
    from . import apps_service, systemd_ops

    app = _sqlite_app(bk["target"])
    unit = apps_service.unit_name(app)
    ts = backup_runner.stamp()

    # 恢复会把目标库整份覆盖，出事时用户唯一能退回的就是覆盖前的那一版，
    # 所以这一步不能省、也不能放在覆盖之后。用在线备份 API 而不是 cp：
    # 正在被写入的库直接复制出来的文件恢复时会报 database image is malformed。
    cands = backup_runner.find_sqlite_dbs(app["path"])
    if not cands:
        raise RuntimeError(f"{app['path']} 下没有找到数据库文件（*.db / *.sqlite*）")
    m = _SQLITE_NAME_RE.match(os.path.basename(src_file))
    if m:
        stem = m.group(1)
        dst = next((c for c in cands if os.path.splitext(os.path.basename(c))[0] == stem), None)
        if not dst:
            raise RuntimeError(f"目录里找不到与备份同名的数据库 {stem}.db；现有候选：{', '.join(cands[:4])}")
    else:
        # 旧格式（文件名不带源库名）只能按"最大的那个库"定位，如实说明而不是假装精确
        dst = cands[0]
        log.append(f"!! 备份文件名未记录源库名，按目录内最大库定位：{dst}")

    pre = f"{dst}.pre-restore-{ts}.sqlite.gz"
    backup_runner.sqlite_snapshot(dst, pre)
    log.append(f"==> 已留存当前库：{os.path.basename(pre)}")

    tmp = f"{src_file}.restoring-{ts}"
    with gzip.open(src_file, "rb") as g, open(tmp, "wb") as f:
        shutil.copyfileobj(g, f, 1024 * 1024)
    mode = os.stat(dst).st_mode & 0o777
    try:
        con = sqlite3.connect(tmp)
        try:
            ok = con.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        os.unlink(tmp)
        raise RuntimeError(f"快照文件无法打开，未做任何改动：{e}")
    if ok != "ok":
        os.unlink(tmp)
        raise RuntimeError(f"快照完整性检查未通过（{ok}），已放弃恢复")

    stopped = False
    try:
        if await systemd_ops.is_active(unit):
            await systemd_ops.service_action(unit, "stop")
            stopped = True
            log.append(f"==> 已停止 {unit}")
        else:
            log.append(f"==> {unit} 未在运行，跳过停止")
        # 残留的 WAL/SHM 属于旧库：留着它们，服务一打开就会把旧帧套到刚写回的库上
        for ext in ("-wal", "-shm", "-journal"):
            if os.path.exists(dst + ext):
                os.unlink(dst + ext)
        os.replace(tmp, dst)
        os.chmod(dst, mode)
        log.append(f"==> 数据已写回 {dst}")
    except Exception:
        for p in (tmp,):
            if os.path.exists(p):
                with suppress(OSError):
                    os.unlink(p)
        raise
    finally:
        try:
            await systemd_ops.service_action(unit, "start")
            log.append(f"==> 已启动 {unit}" + ("（恢复前为运行状态）" if stopped else ""))
        except Exception as e:  # noqa: BLE001
            log.append(f"!! 服务启动失败，请手工启动 {unit}：{e}")


async def _restore_app(bk: dict, src_file: str, log: list) -> None:
    from . import apps_service, systemd_ops

    app = dbm.query_one("SELECT * FROM apps WHERE name=?", (bk["target"],))
    if not app:
        raise RuntimeError("备份目标应用已不存在")
    dest = app["path"]
    if not os.path.isdir(dest):
        raise RuntimeError(f"应用目录不存在：{dest}")
    unit = apps_service.unit_name(app)
    ts = backup_runner.stamp()

    env = os.path.join(dest, ".panel.env")
    env_keep = None
    if os.path.exists(env):
        # 归档里的 .panel.env 是备份当时的环境变量，而口令/端口可能早就改过了，
        # 覆盖回去会让服务突然起不来且毫无提示。先留存现值，解包后再还原现值。
        env_keep = f"{env}.pre-restore-{ts}"
        shutil.copy2(env, env_keep)

    with tarfile.open(src_file, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            name = m.name.replace("\\", "/")
            parts = [p for p in name.split("/") if p]
            if name.startswith("/") or ".." in parts or not parts:
                raise RuntimeError(f"归档含不安全路径：{m.name}")
            if not (m.isfile() or m.isdir()):
                # 链接型条目会把写入引到目录之外（备份里一个 node_modules 软链就能覆盖系统文件）
                raise RuntimeError(f"归档含不支持的条目类型：{m.name}")
            members.append(m)
        stopped = False
        try:
            if await systemd_ops.is_active(unit):
                await systemd_ops.service_action(unit, "stop")
                stopped = True
                log.append(f"==> 已停止 {unit}")
            tf.extractall(path=dest, members=members)
            log.append(f"==> 已解包 {len(members)} 个条目到 {dest}")
            if env_keep:
                shutil.copy2(env_keep, env)
                log.append("==> 保留当前 .panel.env（未使用归档内的环境变量）")
        finally:
            if stopped:
                try:
                    await systemd_ops.service_action(unit, "start")
                    log.append(f"==> 已启动 {unit}")
                except Exception as e:  # noqa: BLE001
                    log.append(f"!! 服务启动失败，请手工启动 {unit}：{e}")


async def _restore_pg(bk: dict, src_file: str, log: list) -> None:
    db = bk["target"]
    if db == "all":
        raise RuntimeError("整库备份（pg_dumpall）请手工恢复：gzip -dc 文件 | sudo -u postgres psql -v ON_ERROR_STOP=1")
    if not is_ident(db):
        raise RuntimeError("库名不合法")
    ts = backup_runner.stamp()
    # 文件名保持 bk<id>-<时间戳>-pre-restore.dump.gz：prune 是按名字排序删最旧的，
    # 这里用当前时间戳正好排在末尾；写成 bk<id>-pre-restore-... 会排到所有真实备份之后，
    # 等于每次恢复都留下一个永不轮替的文件把 keep 名额吃掉。
    pre = os.path.join(BACKUP_DIR, f"bk{bk['id']}-{ts}-pre-restore.dump.gz")
    # 恢复会 drop 目标库，先给当前内容做一份同目录的 dump 作为退路
    code = (await _sh_script(backup_runner.dump_cmd(f"su -s /bin/sh postgres -c 'pg_dump -Fc {db}'", pre)))["code"]
    if code != 0:
        raise RuntimeError(f"当前库快照失败（exit {code}），未做任何改动")
    log.append(f"==> 已留存当前库：{os.path.basename(pre)}")

    owner = ""
    with suppress(Exception):
        owner = (await psql(f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{db}';")).strip()
    if not is_ident(owner):
        owner = "postgres"

    # PG 15+ 的 dropdb --force 会先断开现有连接；旧版本没有这个参数，退回普通 dropdb，
    # 这时若报 "other users are connected" 就是真有人在用，如实报错而不是强杀。
    # set -e 保证 drop 没成功就不会 createdb/restore（否则会在残留库上叠加恢复内容）。
    def su(inner: str) -> str:
        return f"su -s /bin/sh postgres -c {sh_escape(inner)}"

    script = "\n".join(
        [
            "set -e",
            f"{su(f'dropdb --if-exists --force {db}')} || {su(f'dropdb --if-exists {db}')}",
            su(f"createdb -O {owner} {db}"),
            f"gzip -dc {sh_escape(src_file)} | {su(f'pg_restore -d {db} --no-privileges --exit-on-error')}",
        ]
    )
    r = await _sh_script(script, timeout=3600)
    if r["code"] != 0:
        raise RuntimeError(
            f"恢复失败（退出码 {r['code']}），当前库已被删除，可用 {os.path.basename(pre)} 重试恢复：{r['out'][-500:]}"
        )
    log.append(f"==> 库 {db} 已从 {os.path.basename(src_file)} 恢复（属主 {owner}）")


async def restore_backup(bid: int, filename: str) -> dict:
    """用指定备份文件把目标覆盖回备份当时的状态（admin 手动触发，不可逆操作）。"""
    bk = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not bk:
        raise RuntimeError("备份任务不存在")
    path = _backup_file_path(bid, filename)
    if not _restoreable(bk["kind"], os.path.basename(path)):
        raise RuntimeError("该备份类型不支持自动恢复，请手工恢复")
    log: list[str] = [f"==> 恢复 {os.path.basename(path)}（kind={bk['kind']}, target={bk['target']}）"]
    if bk["kind"] == "sqlite":
        await _restore_sqlite(bk, path, log)
    elif bk["kind"] == "app":
        await _restore_app(bk, path, log)
    else:
        await _restore_pg(bk, path, log)
    dbm.audit("system", "backup:restore", f"{bid}:{os.path.basename(path)}")
    return {"ok": True, "log": "\n".join(log)}


async def run_backup_now(bid: int) -> dict:
    r = await run(PYTHON_BIN, ["-m", "app.backup_runner", str(bid)], cwd=BACKEND_DIR, timeout=1800)
    return {"code": r["code"], "out": r["out"]}


async def sync_all_timers():
    for b in dbm.query("SELECT * FROM backups"):
        try:
            await _sync_timer(b)
        except Exception as e:  # noqa: BLE001
            warn(f"backup timer #{b['id']} sync failed: {e}")
