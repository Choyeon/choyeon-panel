"""CLI 备份执行器：python3 -m app.backup_runner <backupId>（由 systemd timer 调用）。"""
import datetime
import os
import re
import subprocess
import sys

from . import database as dbm
from .config import BACKUP_DIR
from .util import sh_escape

EXCLUDES = ["node_modules", ".venv", ".next", ".output"]


def sh(cmd: str):
    return subprocess.run(["/bin/sh", "-c", cmd], check=True, stdout=subprocess.DEVNULL)


def main(bk: dict):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H-%M")
    ts = ts.replace(":", "-")
    if bk["kind"] == "pg":
        if bk["target"] == "all":
            file = f"{BACKUP_DIR}/bk{bk['id']}-{ts}.sql.gz"
            sh(f"su -s /bin/sh postgres -c 'pg_dumpall' | gzip > {sh_escape(file)}")
        else:
            if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", bk["target"]):
                raise SystemExit("库名不合法")
            file = f"{BACKUP_DIR}/bk{bk['id']}-{ts}.dump.gz"
            sh(f"su -s /bin/sh postgres -c 'pg_dump -Fc {bk['target']}' | gzip > {sh_escape(file)}")
        dbm.audit("system", "backup:pg", bk["target"])
    elif bk["kind"] == "app":
        app = dbm.query_one("SELECT path FROM apps WHERE name=?", (bk["target"],))
        if not app:
            raise SystemExit(f"应用 {bk['target']} 不存在")
        file = f"{BACKUP_DIR}/bk{bk['id']}-{ts}.tar.gz"
        excl = " ".join(f"--exclude={e}" for e in EXCLUDES)
        sh(f"tar czf {sh_escape(file)} {excl} -C {sh_escape(app['path'])} .")
        dbm.audit("system", "backup:app", bk["target"])
    prune(bk)


def prune(bk: dict):
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"bk{bk['id']}-"))
    while len(files) > int(bk["keep"]):
        f = files.pop(0)
        try:
            os.unlink(f"{BACKUP_DIR}/{f}")
            print(f"pruned {f}")
        except OSError:
            pass


if __name__ == "__main__":
    try:
        bid = int(sys.argv[1])
    except (IndexError, ValueError):
        print("usage: python3 -m app.backup_runner <backupId>", file=sys.stderr)
        sys.exit(2)
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not row:
        print("backup not found", file=sys.stderr)
        sys.exit(2)
    try:
        main(row)
        print("backup ok")
    except subprocess.CalledProcessError as e:
        print(f"backup failed: exit {e.returncode}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"backup failed: {e}", file=sys.stderr)
        sys.exit(1)
