import os
import sys
from pathlib import Path

from .. import config
from .. import database as dbm
from ..util import is_name, run
from .files_service import iso_ms
from .pg_service import is_ident

BACKUP_DIR = config.BACKUP_DIR
BACKEND_DIR = str(Path(config.BASE, "backend"))
_VENV_PY = str(Path(BACKEND_DIR, ".venv", "bin", "python"))
PYTHON_BIN = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable


def _unit_base(bid: int) -> str:
    return f"panel-backup-{bid}"


def list_backups() -> list:
    rows = dbm.query("SELECT * FROM backups ORDER BY id")
    out = []
    for b in rows:
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
    svc = f"/etc/systemd/system/{u}.service"
    tmr = f"/etc/systemd/system/{u}.timer"
    if b["schedule"] == "manual" or not b["enabled"]:
        await run("systemctl", ["disable", "--now", f"{u}.timer"], timeout=30)
        for f in (svc, tmr):
            if os.path.exists(f):
                os.unlink(f)
        await run("systemctl", ["daemon-reload"])
        return
    tz = None
    try:
        tz = Path("/etc/timezone").read_text().strip() or None
    except OSError:
        pass
    if not tz:
        link = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in link:
            tz = link.split("/zoneinfo/")[1]
    tz = tz or "UTC"
    cal = f"{'Mon ' if b['schedule'] == 'weekly' else ''}{int(b['hour']):02d}:{int(b['minute']):02d} {tz}"
    data = config.DATA_DIR
    _write_if_changed(
        svc,
        f"[Unit]\nDescription=choyeon-panel backup #{b['id']}\n[Service]\nType=oneshot\n"
        f"WorkingDirectory={BACKEND_DIR}\n"
        f"ExecStart={PYTHON_BIN} -m app.backup_runner {b['id']}\n"
        f"Environment=CP_DATA_DIR={data}\n",
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
    if i["kind"] == "pg" and i.get("target") != "all" and not is_ident(i.get("target") or ""):
        raise RuntimeError("PG 库名不合法")
    if i["kind"] == "app" and not is_name(i.get("target") or ""):
        raise RuntimeError("应用名不合法")
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
            0 if i.get("enabled") is False else 1,
        ),
    )
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (res,))
    await _sync_timer(row)
    return row


async def update_backup(bid: int, i: dict) -> dict:
    cur = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not cur:
        raise RuntimeError("备份任务不存在")
    _validate_fields(i)
    dbm.execute(
        "UPDATE backups SET kind=?,target=?,schedule=?,hour=?,minute=?,keep=?,enabled=? WHERE id=?",
        (
            i["kind"] if i.get("kind") is not None else cur["kind"],
            i["target"] if i.get("target") is not None else cur["target"],
            i["schedule"] if i.get("schedule") is not None else cur["schedule"],
            i["hour"] if i.get("hour") is not None else cur["hour"],
            i["minute"] if i.get("minute") is not None else cur["minute"],
            i["keep"] if i.get("keep") is not None else cur["keep"],
            cur["enabled"] if i.get("enabled") is None else (1 if i["enabled"] else 0),
            bid,
        ),
    )
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    await _sync_timer(row)
    return row


async def delete_backup(bid: int):
    u = _unit_base(bid)
    await run("systemctl", ["disable", "--now", f"{u}.timer"], timeout=30)
    for f in (f"/etc/systemd/system/{u}.service", f"/etc/systemd/system/{u}.timer"):
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
            print(f"backup timer #{b['id']} sync failed: {e}")
