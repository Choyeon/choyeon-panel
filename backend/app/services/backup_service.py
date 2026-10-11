import gzip
import errno
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from contextlib import suppress
from pathlib import Path

from starlette.concurrency import run_in_threadpool

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

# 手工上传产物时各类型接受的扩展名，必须与 _restoreable 一一对齐：
# 传了恢复不了的文件只会躺在备份目录里占名额，前端只能把按钮置灰。
_UPLOAD_SUFFIXES = {
    "pg": (".dump.gz",),
    "sqlite": (".sqlite.gz",),
    "app": (".tar.gz", ".tgz"),
}


def _unit_base(bid: int) -> str:
    return f"panel-backup-{bid}"


def _unit_dir() -> str:
    """单元目录取 config.UNIT_DIR（CP_UNIT_DIR 可改），和 apps_service 用同一个开关。

    之前这里写死 /etc/systemd/system：改了 CP_UNIT_DIR 的人，应用 unit 跟着走、
    备份 timer 还留在原地，两套位置谁也说不清哪个是权威。运行时读，方便测试重定向。
    """
    return config.UNIT_DIR


def _target_missing(b: dict, app_ids: set, app_names: set) -> bool:
    """app / sqlite 类备份的目标必须还在 apps 表里。

    应用被删除后备份行不会跟着走，timer 每晚跑一次 backup_runner 就 SystemExit 一次，
    而面板上这条任务依旧显示"已启用"——只有翻 journalctl 才发现备份早就不产出了。
    """
    if b["kind"] not in ("app", "sqlite"):
        return False
    t = str(b["target"] or "")
    if b["kind"] == "sqlite" and t.isdigit():
        return int(t) not in app_ids
    return t not in app_names


def list_backups() -> list:
    rows = dbm.query("SELECT * FROM backups ORDER BY id")
    # 目标存在性一次取回集合，取代每行 1-2 条 SELECT（备份任务多时列表就是 N 次查询）
    apps = dbm.query("SELECT id, name FROM apps")
    app_ids = {r["id"] for r in apps}
    app_names = {r["name"] for r in apps}
    listing = []
    if os.path.isdir(BACKUP_DIR):
        try:
            listing = sorted(os.listdir(BACKUP_DIR))
        except OSError:
            listing = []
    out = []
    for b in rows:
        b["dir"] = BACKUP_DIR
        b["target_missing"] = _target_missing(b, app_ids, app_names)
        prefix = f"bk{b['id']}-"
        files = list(reversed([f for f in listing if f.startswith(prefix)]))[:30]
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
        out.append(b)
    return out


_BELONGS_RE = re.compile(r"^bk(\d+)-")


def list_files() -> dict:
    """所有任务的产物平铺成一页（备份文件管理页用），按时间倒序。

    任务列表里的 files 是"每条任务前 30 个"，看总量、按时间清理都不方便；
    这里反过来：以文件为主体，任务只是它的一个属性。
    任务行已被删除但文件还在的（job_missing）也列出来——那些正是没人管、
    只能靠手动删才腾得出来的空间。
    """
    files: list[dict] = []
    if not os.path.isdir(BACKUP_DIR):
        return {"dir": BACKUP_DIR, "files": files}
    jobs = {b["id"]: b for b in dbm.query("SELECT * FROM backups")}
    for name in os.listdir(BACKUP_DIR):
        m = _BELONGS_RE.match(name)
        if not m:
            continue
        bid = int(m.group(1))
        try:
            st = os.stat(f"{BACKUP_DIR}/{name}")
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        bk = jobs.get(bid)
        kind = bk["kind"] if bk else ""
        files.append({
            "name": name,
            "job_id": bid,
            "job_missing": bk is None,
            "kind": kind,
            "target": (_sqlite_target_label(bk["target"]) if kind == "sqlite" else bk["target"]) if bk else "",
            "size": st.st_size,
            "mtime": iso_ms(st.st_mtime),
            "restoreable": _restoreable(kind, name) if bk else False,
        })
    files.sort(key=lambda f: (f["mtime"], f["name"]), reverse=True)
    return {"dir": BACKUP_DIR, "files": files}


def _sqlite_target_label(target) -> str:
    """sqlite 的 target 存的是 app_id（也可能是历史遗留的应用名），统一显示成应用名。"""
    t = str(target or "")
    row = dbm.query_one("SELECT name FROM apps WHERE id=?", (int(t),)) if t.isdigit() else None
    if not row:
        row = dbm.query_one("SELECT name FROM apps WHERE name=?", (t,))
    return row["name"] if row else f"#{t}"


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


def _validate_target(kind: str, target, *, app_must_exist: bool = True):
    """kind/target 的配对校验，create 与 update 必须共用同一份。

    旧写法只在 create_backup 里校验，update_backup 只查了 schedule/hour/minute/keep，
    于是 `PATCH {"target": "app;DROP DATABASE x;--"}` 能一路写进库。
    这个 target 后面会原样拼进 `su - postgres -c 'pg_dump -Fc <target>'`
    （backup_runner 里另有一道校验挡住了执行侧），但真正的问题是：
    坏值进了库，timer 每次跑都在 runner 里 SystemExit，面板上却显示"任务已配置"。
    kind 同样能在 PATCH 里改成表 CHECK 之外的值——那时抛的是 sqlite3.IntegrityError，
    面板路由的宽 except 会把它兜成 400，可是任何直接调服务层的地方（sync_all_timers、
    以后的 CLI）拿到的是一个未捕获的数据库异常，而不是业务错误。

    `app_must_exist` 是"目标应用这一行还在不在"的开关，只有把 target **换成了别的值**
    时才该查（create 总是，update 见调用处）。线上探针实测：
    `kind='app'` 过去连格式都过、名字不存在也照样建（返回 200，跑一次备份才
    「应用 x 不存在」退出码 1），写错一个字母就变成一条每晚失败的任务。
    但反过来，把存在性校验无条件套在 update 上会拆掉 ⑤L 的 `target_missing`：
    应用删掉之后那条任务连"改备份时间"和"停用"都做不了（sqlite 类就是这样，
    `merged target` 永远是非空的历史 id），只剩删除一条路，而用户想留产物、
    先把计划关掉。所以：名字写错当场就挡，指向已删应用的老行仍然可以改配置。
    pg 库名只查格式不查存在性——面板自己能建库，"先配好备份计划再建库"是
    一条合理路径，而库不存在时 pg_dump 的失败在备份日志里看得见。
    """
    if kind == "pg" and target != "all" and not is_ident(target or ""):
        raise RuntimeError("PG 库名不合法")
    if kind == "app":
        t = str(target or "")
        if not is_name(t):
            raise RuntimeError("应用名不合法")
        if app_must_exist and not dbm.query_one("SELECT 1 FROM apps WHERE name=?", (t,)):
            raise RuntimeError(f"应用 {t} 不存在")
    if kind == "sqlite":
        # sqlite 的 target 存 app_id（应用改名后 name 型 target 会指向不存在的目录），
        # 同时兼容按应用名配置的历史写法。
        t = str(target or "")
        if t.isdigit():
            if app_must_exist and not dbm.query_one("SELECT 1 FROM apps WHERE id=?", (int(t),)):
                raise RuntimeError("应用不存在")
        elif not is_name(t) or (app_must_exist and not dbm.query_one("SELECT 1 FROM apps WHERE name=?", (t,))):
            raise RuntimeError("SQLite 备份目标应用不存在")


def rename_app(old_name: str, new_name: str) -> None:
    """应用改名后，把按应用名存目标的备份任务一起改名。

    `kind='app'` 的 target 存的就是应用名——`backup_runner` 按
    `SELECT path FROM apps WHERE name=?` 找目录，`_restore_app` 也一样。应用改名时
    这一行不会跟着动，于是它指向一个已经不存在的名字：timer 每晚跑一次 runner 就
    SystemExit 一次（"应用 x 不存在"），面板上这条任务还是"已启用"，只有翻
    journalctl 才发现备份早就不产出了。⑤L 加的 `target_missing` 只是把坏状态**显示**
    出来，改名这件事本身应该让任务继续跟着应用走。

    `kind='sqlite'` 现在按 app_id 存（改名不影响它），但 `_validate_target` 明写了
    "兼容历史的名型写法"，库里真有这种行，所以名型的那一份也一起带走。
    匹配用精确等值而不是 LIKE：应用名允许连字符，模糊匹配会误伤别的任务。
    """
    dbm.execute(
        "UPDATE backups SET target=? WHERE kind IN ('app','sqlite') AND target=?",
        (new_name, old_name),
    )


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
    # create 有、update 没有——这道不对称就是上面 _validate_target 说的漏洞，补齐。
    # 存在性只在 target **真的被换了** 时才查：换一个目标就得指对东西。
    # 编辑弹窗是把当前行整份回传的（Backups.vue 的 form 一直带着 target），
    # 若按"请求体有没有 target"判断，⑤L 的 target_missing 行就会连"改备份时间"
    # 和"停用"都做不了——只剩删除一条路，而用户可能想先留产物、把计划关掉。
    _validate_target(
        merged["kind"], merged["target"], app_must_exist=str(merged["target"]) != str(cur["target"])
    )
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


def save_upload(bid: int, filename: str, data: bytes, overwrite: bool = False) -> dict:
    """把外部产物（从另一台机器拷来的 dump）登记进某个备份任务名下。

    文件名必须补上 `bk<id>-` 前缀：恢复与删除都靠这个前缀判断归属（见 _backup_file_path），
    原样保存用户上传的名字会让文件躺在目录里却点不动恢复按钮。
    """
    job = dbm.query_one("SELECT id, kind, target FROM backups WHERE id=?", (bid,))
    if not job:
        raise RuntimeError("备份任务不存在")
    suffixes = _UPLOAD_SUFFIXES.get(job["kind"]) or ()
    if not suffixes:
        raise RuntimeError(f"不支持给 {job['kind']} 类任务上传产物")
    base = os.path.basename(str(filename or "")).strip()
    if not base or base.startswith(".") or base in (".", ".."):
        raise RuntimeError("文件名不合法")
    name = base if base.startswith(f"bk{bid}-") else f"bk{bid}-manual-{base}"
    if not name.endswith(suffixes):
        raise RuntimeError(f"{job['kind']} 类备份文件名要以 {'、'.join(suffixes)} 结尾")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise RuntimeError(f"文件超过 {config.MAX_UPLOAD_BYTES // 1024 // 1024}MB，请走服务器拷贝")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, name)
    existed = os.path.lexists(dest)
    if os.path.islink(dest) and not overwrite:
        raise RuntimeError("目标是符号链接，拒绝覆盖")
    if existed and not overwrite:
        raise RuntimeError(f"同名文件已存在：{name}")
    # 与 files_service.write_text 同一套理由：检查与写入之间换一个链接就能把内容写到目录外
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise RuntimeError("目标是符号链接，为避免写到别处已拒绝") from None
        raise RuntimeError(f"写入失败：{e.strerror or e}") from e
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return {"name": name, "size": len(data), "overwrote": existed}


async def delete_file(bid: int, filename: str) -> dict:
    """只删一个备份产物，不动任务、不动 timer。

    prune 是按文件名排序删最旧的，手动删掉旧的一份等于给下一轮腾出名额；
    备份目录不在 data/ 下，也没有别的接口能删它，所以这里必须自己把名字记进审计，
    事后才查得到是谁在什么时候清了哪一份。
    """
    path = _backup_file_path(bid, filename)
    os.unlink(path)
    return {"ok": True, "name": os.path.basename(path)}


_SQLITE_NAME_RE = re.compile(r"^bk\d+-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?-(.+)\.sqlite\.gz$")


def _restoreable(kind: str, name: str) -> bool:
    """这个备份文件能不能走自动恢复。

    pg 的 .sql.gz 是 pg_dumpall 的整库文本档，恢复意味着重建集群里所有库和角色，
    绝不能由一个按钮触发；面板自身数据库（bk0- 之类）也不该被服务覆盖。
    """
    if kind == "sqlite":
        return name.endswith(".sqlite.gz")
    if kind == "app":
        return name.endswith((".tar.gz", ".tgz"))
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


def _gunzip_to(src_file: str, dest: str) -> None:
    with gzip.open(src_file, "rb") as g, open(dest, "wb") as f:
        shutil.copyfileobj(g, f, 1024 * 1024)
        # 目标是线上库文件，os.replace 只保证"重命名"这一步原子，不保证内容已经
        # 离开页缓存：中间断电的话库里留下的就是一个截断文件，而面板已经报了成功。
        f.flush()
        os.fsync(f.fileno())


def _quick_check(path: str) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()


async def _restore_sqlite(bk: dict, src_file: str, log: list) -> None:
    from . import apps_service, systemd_ops

    app = _sqlite_app(bk["target"])
    unit = apps_service.unit_name(app)
    ts = backup_runner.stamp()

    # 恢复会把目标库整份覆盖，出事时用户唯一能退回的就是覆盖前的那一版，
    # 所以这一步不能省、也不能放在覆盖之后。用在线备份 API 而不是 cp：
    # 正在被写入的库直接复制出来的文件恢复时会报 database image is malformed。
    # 整目录 walk 在几千文件的 node_modules 上要几百毫秒，不该压着事件循环。
    cands = await run_in_threadpool(backup_runner.find_sqlite_dbs, app["path"])
    if not cands:
        raise RuntimeError(f"{app['path']} 下没有找到数据库文件（*.db / *.sqlite*）")
    m = _SQLITE_NAME_RE.match(os.path.basename(src_file))
    if m:
        stem = m.group(1)
        # 留档名带来源库的名字再加 -pre-restore 后缀（见下方 pre 的命名），
        # 这样点它的"恢复"能退回被本次覆盖掉的那一版，而不是报找不到库。
        if stem.endswith("-pre-restore"):
            stem = stem[: -len("-pre-restore")]
        dst = next((c for c in cands if os.path.splitext(os.path.basename(c))[0] == stem), None)
        if not dst:
            raise RuntimeError(f"目录里找不到与备份同名的数据库 {stem}.db；现有候选：{', '.join(cands[:4])}")
    else:
        # 旧格式（文件名不带源库名）只能按"最大的那个库"定位，如实说明而不是假装精确
        dst = cands[0]
        log.append(f"!! 备份文件名未记录源库名，按目录内最大库定位：{dst}")

    # 留档写在备份目录里、用 bk<id>-<时间戳>-<库名>-pre-restore 命名：备份文件页只列这个
    # 目录，界面又写着"可退回本次自动留存的 pre-restore 快照"——快照落在应用目录里
    # 就等于用户看不见它，恢复失败后既找不到也点不动。名字排在最新一位，
    # prune 按名字排序删最旧的，它要等真实备份轮替过 keep 名额才会被清掉。
    # 必须走 _free_path 而不是直接拼名：从留档回滚时两次的时间戳可能落在同一秒，
    # 而留档名与源文件名同形——把留档写在源文件上等于先把它覆盖成当前内容，
    # 于是"退回上一版"变成原地不动，且唯一的回滚数据就此消失。
    db_stem = os.path.splitext(os.path.basename(dst))[0]
    pre = backup_runner._free_path(bk["id"], ts, f"-{db_stem}-pre-restore.sqlite.gz", os.path.dirname(src_file))
    # 下面三步都是百 MB 级的同步读改写（在线备份 + gzip、解压、全库 quick_check），
    # 直接在事件循环里跑会让整个面板在恢复期间停响应，所以全部丢给工作线程。
    await run_in_threadpool(backup_runner.sqlite_snapshot, dst, pre)
    log.append(f"==> 已留存当前库：{os.path.basename(pre)}")

    # 暂存必须写在**目标库所在的同一个目录**里。旧写法写在备份目录
    # （f"{src_file}.restoring-<ts>"），而下一步是 os.replace(tmp, dst)——
    # 跨文件系统时它直接抛 OSError(EXDEV)。把 CP_BACKUP_DIR 放到另一块盘或 NFS
    # 上是很常见的配置（备份目录的存在意义就是和数据分开），于是恢复偏偏在
    # 最危险的那一步失败：此刻服务已经停了、目标库的 -wal/-shm 已经删了，
    # 用户拿到一句 "Cross-device link"，还要自己猜数据现在是什么状态。
    # hidden + .tmp 的名字不会被 find_sqlite_dbs 认成候选库（它按 *.db/*.sqlite*
    # 的后缀筛），异常残留时也不会再躺进备份文件页。
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".cp-restore-", suffix=".tmp")
    os.close(fd)
    try:
        await run_in_threadpool(_gunzip_to, src_file, tmp)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    mode = os.stat(dst).st_mode & 0o777
    try:
        ok = await run_in_threadpool(_quick_check, tmp)
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
        # 只有真的停过才去启动：service_action("stop") 自己失败时服务从未被我们动过，
        # 这时补一个 start 等于把运维特意停着的服务悄悄拉起来（与 _restore_app 保持一致）。
        if stopped:
            try:
                await systemd_ops.service_action(unit, "start")
                log.append(f"==> 已启动 {unit}")
            except Exception as e:  # noqa: BLE001
                log.append(f"!! 服务启动失败，请手工启动 {unit}：{e}")
        else:
            log.append(f"==> {unit} 恢复前未在运行，保持停止")


def _tar_checked(tf: tarfile.TarFile) -> list:
    """整份归档索引 → 可解包的成员列表，不安全的一律拒绝。

    链接型条目会把写入引到目录之外（备份里一个 node_modules 软链就能覆盖系统文件），
    所以只放行普通文件与目录。
    """
    members = []
    for m in tf.getmembers():
        name = m.name.replace("\\", "/")
        parts = [p for p in name.split("/") if p]
        if name.startswith("/") or ".." in parts or not parts:
            raise RuntimeError(f"归档含不安全路径：{m.name}")
        if not (m.isfile() or m.isdir()):
            raise RuntimeError(f"归档含不支持的条目类型：{m.name}")
        members.append(m)
    return members


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
        # 暂存写在备份目录（root 0700）而不是应用目录里：那里是一份含口令的副本，
        # 落在应用可读路径上、恢复完还一直躺着，prune 与备份页也都看不见它。
        env_keep = backup_runner._free_path(bk["id"], ts, "-pre-restore.env", os.path.dirname(src_file))
        await run_in_threadpool(shutil.copy2, env, env_keep)

    # 读索引和解包都是整份归档量级的同步读写（一个 node_modules 就是几百 MB），
    # 在循环上跑会让整个面板在恢复期间停响应。三步共用同一个 TarFile 句柄：
    # 成员条目的数据偏移量是按它自己的文件对象算的，换个实例再解包就会读错位置。
    tf = await run_in_threadpool(tarfile.open, src_file, "r:gz")
    try:
        members = await run_in_threadpool(_tar_checked, tf)
        stopped = False
        restored = False
        try:
            if await systemd_ops.is_active(unit):
                await systemd_ops.service_action(unit, "stop")
                stopped = True
                log.append(f"==> 已停止 {unit}")
            else:
                log.append(f"==> {unit} 未在运行，跳过停止")
            await run_in_threadpool(tf.extractall, dest, members)
            restored = True
            log.append(f"==> 已解包 {len(members)} 个条目到 {dest}")
        finally:
            if env_keep:
                # 不管解包成没成，环境变量都得回到现值：半程失败时归档里那份
                # 旧 .panel.env 可能已经落下去了，留着它服务下次起来用的是别的口令。
                # 这句回写可能整个失败（磁盘满、权限），而旧写法无条件记
                # 「保留当前 .panel.env」——那句话在此刻是假的，用户会以为生效的
                # 还是现值，实际用的是归档里备份当时的口令。按结果如实记。
                env_kept = False
                with suppress(Exception):
                    await run_in_threadpool(shutil.copy2, env_keep, env)
                    env_kept = True
                with suppress(OSError):
                    os.unlink(env_keep)
                if env_kept:
                    log.append("==> 保留当前 .panel.env（未使用归档内的环境变量）")
                else:
                    log.append(
                        f"!! 当前 .panel.env 回写失败，归档里那份（备份当时的值）已生效："
                        f"启动前请核对 {env} 里的口令与端口"
                    )
            if stopped and restored:
                try:
                    await systemd_ops.service_action(unit, "start")
                    log.append(f"==> 已启动 {unit}")
                except Exception as e:  # noqa: BLE001
                    log.append(f"!! 服务启动失败，请手工启动 {unit}：{e}")
            elif stopped:
                # 半恢复的目录里可能缺几百个文件，把它启动起来等于让服务在残骸上
                # 写数据、跑迁移。宁可停在停止态并说清楚，也不要"看起来恢复了"。
                log.append(f"!! 解包中断：{dest} 可能处于半恢复状态，服务保持停止；请重跑恢复或改用更早的备份")
    finally:
        tf.close()


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
    pre = backup_runner._free_path(bk["id"], ts, "-pre-restore.dump.gz", BACKUP_DIR)
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

    def drop_create() -> str:
        """drop → createdb 一段。set -e 保证 drop 没成功就不会建库。

        原来 drop/createdb/restore 与恢复数据写在同一段脚本里，一次非零退出既可能是
        "drop 被占用的人挡住、现库完好"，也可能是"现库已删、新内容只 restore 了一半"，
        而这两种情况该做的事完全相反。拆成两段就分得清：第一段失败没碰过库，
        第二段失败库已经是空壳，必须用留档重建。
        """
        return "\n".join(
            [
                "set -e",
                f"{su(f'dropdb --if-exists --force {db}')} || {su(f'dropdb --if-exists {db}')}",
                su(f"createdb -O {owner} {db}"),
            ]
        )

    def load(restore_src: str, exit_on_error: bool = True) -> str:
        eo = " --exit-on-error" if exit_on_error else ""
        return f"gzip -dc {sh_escape(restore_src)} | {su(f'pg_restore -d {db} --no-privileges{eo}')}"

    # 先换成空库，再灌数据：分成两段才知道现库到底有没有被碰过
    r = await _sh_script(drop_create(), timeout=600)
    if r["code"] != 0:
        raise RuntimeError(f"重建空库失败（退出码 {r['code']}），当前库未被改动：{r['out'][-500:]}")

    r = await _sh_script(load(src_file), timeout=3600)
    if r["code"] != 0:
        # 此刻库已被 drop 且 restore 只走了一半：只报"失败"等于留下一个没人接管的空壳。
        # 恢复前那份留档就是为这一步准备的，先尽力装回去，再如实报告两次退出码。
        rb = await _sh_script(drop_create() + "\n" + load(pre, exit_on_error=False), timeout=3600)
        if rb["code"] == 0:
            raise RuntimeError(
                f"恢复失败（退出码 {r['code']}），已用 {os.path.basename(pre)} 把恢复前的库重建回去：{r['out'][-500:]}"
            )
        raise RuntimeError(
            f"恢复与回退均失败（{r['code']}/{rb['code']}），库 {db} 现为空壳或不完整；"
            f"请手工执行 gzip -dc {os.path.basename(pre)} | sudo -u postgres pg_restore -d {db} "
            f"--clean --if-exists：{r['out'][-300:]} / {rb['out'][-300:]}"
        )
    log.append(f"==> 库 {db} 已从 {os.path.basename(src_file)} 恢复（属主 {owner}）")


async def restore_backup(bid: int, filename: str, actor: str = "system") -> dict:
    """用指定备份文件把目标覆盖回备份当时的状态（admin 手动触发，不可逆操作）。"""
    bk = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not bk:
        raise RuntimeError("备份任务不存在")
    path = _backup_file_path(bid, filename)
    if not _restoreable(bk["kind"], os.path.basename(path)):
        raise RuntimeError("该备份类型不支持自动恢复，请手工恢复")
    log: list[str] = [f"==> 恢复 {os.path.basename(path)}（kind={bk['kind']}, target={bk['target']}）"]
    try:
        if bk["kind"] == "sqlite":
            await _restore_sqlite(bk, path, log)
        elif bk["kind"] == "app":
            await _restore_app(bk, path, log)
        else:
            await _restore_pg(bk, path, log)
    except Exception as e:  # noqa: BLE001
        # 半途失败时 log 里那句「解包中断：目录可能处于半恢复状态，服务保持停止」是用户
        # 唯一能照着处理的现场说明。旧写法只把异常往上抛，这段说明连同整个 log 一起丢了，
        # 界面上一行 "恢复失败: …"，没人知道服务其实被恢复流程停掉了、目录是半新的。
        raise RuntimeError(f"{e}\n\n执行记录：\n" + "\n".join(log)) from e
    # actor 由路由传当前登录用户：覆盖线上数据这种事记成 "system" 等于没记
    dbm.audit(actor, "backup:restore", f"{bid}:{os.path.basename(path)}")
    # 数据确实装上了，但日志里有 `!!` 行 = 有一件事要人接手（服务没起来、按最大库定位、
    # 归档环境变量生效）。这些行过去只藏在 log 里，而界面收到 ok:true 就一律报
    # 「恢复完成」——绿色成功提示正好把要处理的那行盖住。单独带出来给前端定调性。
    return {"ok": True, "warn": [line for line in log if line.startswith("!!")], "log": "\n".join(log)}


async def run_backup_now(bid: int) -> dict:
    r = await run(PYTHON_BIN, ["-m", "app.backup_runner", str(bid)], cwd=BACKEND_DIR, timeout=1800)
    return {"code": r["code"], "out": r["out"]}


async def sync_all_timers():
    for b in dbm.query("SELECT * FROM backups"):
        try:
            await _sync_timer(b)
        except Exception as e:  # noqa: BLE001
            warn(f"backup timer #{b['id']} sync failed: {e}")
