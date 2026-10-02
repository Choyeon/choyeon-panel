import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent  # repo root: choyeon-panel/

PORT = int(os.environ.get("CP_PORT", "3210"))
HOST = os.environ.get("CP_HOST", "127.0.0.1")
TRUST_PROXY = os.environ.get("CP_TRUST_PROXY") == "1"
LOG = bool(os.environ.get("CP_LOG"))

DATA_DIR = os.environ.get("CP_DATA_DIR") or str(BASE / "data")
APP_ROOT = os.environ.get("CP_APP_ROOT") or "/root/www"
BACKUP_DIR = os.environ.get("CP_BACKUP_DIR") or "/root/backups/panel"
FILE_ROOTS = (os.environ.get("CP_FILE_ROOTS") or "/root/www,/etc/nginx,/root/backups/panel").split(",")
PYTHON = os.environ.get("CP_PYTHON") or "python3"

WEB_DIST = BASE / "web" / "dist"
SYS_HELPER = BASE / "backend" / "python" / "sys_helper.py"
UNIT_DIR = "/etc/systemd/system"
NGINX_CONF_DIRS = ["/etc/nginx/conf.d", "/etc/nginx/sites-available"]
TLS_DIR = "/etc/letsencrypt/live"
