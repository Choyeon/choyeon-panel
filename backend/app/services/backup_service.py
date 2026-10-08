import os
import sys
from contextlib import suppress
from pathlib import Path

from .. import config
from .. import database as dbm
from ..log import warn
from ..util import is_name, run, unit_env_value, unit_pct_escape
from .files_service import iso_ms
from .pg_service import is_ident

BACKUP_DIR = config.BACKUP_DIR
BACKEND_DIR = str(Path(config.BASE, "backend"))
_VENV_PY = str(Path(BACKEND_DIR, ".venv", "bin", "python"))
PYTHON_BIN = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable


def _unit_base(bid: int) -> str:
    return f"panel-backup-{bid}"


def _unit_dir() -> str:
    """单元目录取 config.UNIT_DIR（CP_UNIT_DIR 可改），和 apps_service 用同一个开关。

    之前这里写死 /etc/systemd/system：改了 CP_UNIT_DIR 的人，应用 unit 跟着走、
    备份 timer 还留在原地，两套位置谁也说不清哪个是权威。运行时读，方便测试重定向。
    """
    return config.UNIT_DIR


def _target_missing(b: dict) -> bool:
    """app 类备份的目标必须还在 apps 表里。

    应用被删除后备份行不会跟着走，timer 每晚跑一次 backup_runner 就 SystemExit 一次，
    而面板上这条任务依旧显示"已启用"——只有翻 journalctl 才发现备份早就不产出了。
    """
    return b["kind"] == "app" and not dbm.query_one("SELECT 1 FROM apps WHERE name=?", (b["target"],))


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
                    entries.append({"name": f, "size": st.st_size, "mtime": iso_ms(st.st_mtime)})
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
    if i.get("kind") not in ("pg", "app"):
        raise RuntimeError("kind 必须是 pg 或 app")
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
    await _sync_timer(row)
    return row


async def update_backup(bid: int, i: dict) -> dict:
    cur = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not cur:
        raise RuntimeError("备份任务不存在")
    _validate_fields(i)
    merged = {
        "kind": i["kind"] if i.get("kind") is not None else cur["kind"],
        "target": i["target"] if i.get("target") is not None else cur["target"],
    }
    if merged["kind"] not in ("pg", "app"):
        raise RuntimeError("kind 必须是 pg 或 app")
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
    await _sync_timer(row)
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


async def run_backup_now(bid: int) -> dict:
    r = await run(PYTHON_BIN, ["-m", "app.backup_runner", str(bid)], cwd=BACKEND_DIR, timeout=1800)
    return {"code": r["code"], "out": r["out"]}


async def sync_all_timers():
    for b in dbm.query("SELECT * FROM backups"):
        try:
            await _sync_timer(b)
        except Exception as e:  # noqa: BLE001
            warn(f"backup timer #{b['id']} sync failed: {e}")
