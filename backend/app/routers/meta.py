"""元信息接口：就绪探针、部署模板、安全与健康自检（doctor）。

doctor 只做**只读**检查，不修改任何系统状态；每项给出状态与修复建议命令，
供网页端「自检」页与 CLI `choyeonctl doctor` 共用。
"""

from __future__ import annotations

import glob
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config
from .. import database as dbm
from ..services import systemd_ops
from ..templates import list_templates
from ..util import disk_usage_pct, run

router = APIRouter()

SERVICE_NAME = "choyeon-panel"
CLI_LINK = Path("/usr/local/bin/choyeonctl")


@router.get("/api/ready")
async def ready():
    """Readiness：进程活着还不够，库要能查。失败返回 503 供反代/容器摘流量。"""
    checks = {}
    try:
        row = dbm.query_one("SELECT COUNT(*) c FROM users")
        checks["database"] = "ok" if row is not None else "fail"
    except Exception as e:  # noqa: BLE001 探针本身不能抛异常，交给上层统一降级
        checks["database"] = f"fail: {e}"

    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})


@router.get("/api/templates")
async def templates_list():
    return list_templates()


def _recent_backups() -> tuple[int, float | None]:
    """返回（备份份数，最近一份的 mtime）；目录不存在时返回 (0, None)。

    模式必须覆盖 backup_runner 真正写出的三类文件：
    `.sql.gz`（pg_dumpall）、`.dump.gz`（pg_dump -Fc）、`.tar.gz`（应用目录）。
    旧写法只匹配 `*.tar.gz` 与 `*.sql`，PG 备份一种都命中不了——只跑数据库备份的机器
    会被判成"还没有备份"，而且"最近一次备份是几天前"是按 tar 包算的，
    刚跑完的 PG 备份不算数，容易误报陈旧。
    """
    suffixes = (".tar.gz", ".sql.gz", ".dump.gz", ".sql", ".dump", ".gz")
    files: list[str] = []
    for p in glob.iglob(f"{config.BACKUP_DIR}/**/*", recursive=True):
        if p.endswith(suffixes) and os.path.isfile(p):
            files.append(p)
    if not files:
        return 0, None
    return len(files), max(os.path.getmtime(f) for f in files)


async def _check_listen() -> dict:
    if config.HOST not in ("127.0.0.1", "localhost", "::1"):
        return {
            "status": "warn",
            "detail": f"面板监听 {config.HOST}:{config.PORT}，直接暴露公网会绕过 nginx 的 TLS 与限速",
            "fix": "改 CP_HOST=127.0.0.1 后用 nginx 反代，仅放行 80/443",
        }
    return {"status": "pass", "detail": f"仅监听回环 {config.HOST}:{config.PORT}", "fix": ""}


async def _check_admin() -> dict:
    row = dbm.query_one("SELECT COUNT(*) c FROM users")
    n = row["c"] if row else 0
    if n == 0:
        return {
            "status": "fail",
            "detail": "未创建管理员，任何人都能抢注",
            "fix": "choyeonctl user create <name> --password <pw>",
        }
    return {"status": "pass", "detail": f"已创建 {n} 个账号", "fix": ""}


async def _check_service() -> dict:
    if not (shutil.which("systemctl") and os.path.isdir("/run/systemd/system")):
        return {
            "status": "warn",
            "detail": "未检测到 systemd（容器/WSL），需手动启动",
            "fix": "backend/.venv/bin/python -m app.main",
        }
    active = await systemd_ops.is_active(f"{SERVICE_NAME}.service")
    if not active:
        return {"status": "fail", "detail": f"{SERVICE_NAME} 未在运行", "fix": f"systemctl restart {SERVICE_NAME}"}
    return {"status": "pass", "detail": f"{SERVICE_NAME} 运行中", "fix": ""}


async def _check_nginx() -> dict:
    if not shutil.which("nginx"):
        return {
            "status": "warn",
            "detail": "未安装 nginx，面板只能本机访问",
            "fix": "apt install nginx（RHEL: dnf install nginx）",
        }
    r = await run("nginx", ["-t"], timeout=10)
    if r["code"] != 0:
        return {"status": "fail", "detail": "nginx -t 未通过，配置有误", "fix": "nginx -t 查看报错后修正"}
    return {"status": "pass", "detail": "nginx 配置校验通过", "fix": ""}


async def _check_firewall() -> dict:
    if shutil.which("ufw"):
        r = await run("ufw", ["status"], timeout=10)
        out = r["out"] or ""
        if "inactive" in out.lower():
            return {
                "status": "warn",
                "detail": "ufw 未启用",
                "fix": "ufw allow 80/tcp && ufw allow 443/tcp && ufw enable",
            }
        return {"status": "pass", "detail": "ufw 已启用", "fix": ""}
    if shutil.which("firewall-cmd"):
        r = await run("firewall-cmd", ["--state"], timeout=10)
        if r["code"] != 0:
            return {
                "status": "warn",
                "detail": "firewalld 未运行",
                "fix": "systemctl enable --now firewalld"
                       " && firewall-cmd --add-service=http --add-service=https --permanent",
            }
        return {"status": "pass", "detail": "firewalld 运行中", "fix": ""}
    return {"status": "warn", "detail": "未检测到 ufw/firewalld", "fix": "建议启用一种防火墙并只放行 80/443"}


async def _check_backup() -> dict:
    count, mtime = _recent_backups()
    if count == 0:
        return {"status": "warn", "detail": f"{config.BACKUP_DIR} 下还没有备份", "fix": "choyeonctl backup run"}
    days = (time.time() - (mtime or 0)) / 86400
    if days > 7:
        return {
            "status": "warn",
            "detail": f"最近一次备份已是 {days:.0f} 天前（共 {count} 份）",
            "fix": f"配置定时备份：crontab 加 0 4 * * * {config.BASE}/scripts/backup.sh",
        }
    return {"status": "pass", "detail": f"共 {count} 份备份，最近 {days:.1f} 天前", "fix": ""}


async def _check_disk() -> dict:
    pct = disk_usage_pct(config.DATA_DIR)
    if pct is None:
        return {"status": "warn", "detail": f"无法读取 {config.DATA_DIR} 所在分区", "fix": ""}
    if pct >= 90:
        return {
            "status": "fail",
            "detail": f"磁盘已用 {pct}%，数据库写入可能失败",
            "fix": "清理旧备份与 journalctl --vacuum-size=200M",
        }
    if pct >= 75:
        return {"status": "warn", "detail": f"磁盘已用 {pct}%", "fix": "关注增长趋势，必要时清理备份"}
    return {"status": "pass", "detail": f"磁盘已用 {pct}%", "fix": ""}


async def _check_frontend() -> dict:
    if Path(config.WEB_DIST / "index.html").exists():
        return {"status": "pass", "detail": "前端产物已构建", "fix": ""}
    return {"status": "fail", "detail": "web/dist 不存在，访问会 404", "fix": "cd web && npm run build"}


async def _check_permissions() -> dict:
    env_file = Path(config.ENV_FILE)
    if env_file.exists() and (env_file.stat().st_mode & 0o077):
        return {
            "status": "warn",
            "detail": f"{env_file} 权限过宽，密钥可能被同机其他用户读取",
            "fix": f"chmod 0600 {env_file}",
        }
    return {"status": "pass", "detail": ".env 权限正常（或不存在）", "fix": ""}


async def _check_cli() -> dict:
    """AGENTS.md 的每条命令都以 `choyeonctl` 开头，但这个入口只有 install.sh 会建。

    只走过升级流程的实例会停在 "choyeonctl: command not found"（实测本机就是这样），
    自动化按文档执行就会卡在第一步；悬空软链接更糟，报的是 "No such file or directory"，
    看起来像是脚本本身坏了。
    """
    target = Path(config.BASE) / "bin" / "choyeonctl"
    if os.geteuid() != 0 or not target.exists():
        return {"status": "pass", "detail": "非安装环境，跳过全局 CLI 检查", "fix": ""}
    fix = f"ln -sf {target} {CLI_LINK}"
    if CLI_LINK.is_symlink() and not CLI_LINK.exists():
        return {"status": "fail", "detail": f"{CLI_LINK} 是悬空软链接，choyeonctl 直接报错", "fix": fix}
    if not CLI_LINK.exists():
        return {"status": "warn", "detail": "choyeonctl 不在 PATH 上，只能用 ./bin/choyeonctl", "fix": fix}
    return {"status": "pass", "detail": "choyeonctl 已链接到 PATH", "fix": ""}


_CHECKS = [
    ("listen", "监听地址", _check_listen),
    ("admin", "管理员账号", _check_admin),
    ("service", "面板服务", _check_service),
    ("frontend", "前端产物", _check_frontend),
    ("nginx", "nginx 配置", _check_nginx),
    ("firewall", "防火墙", _check_firewall),
    ("backup", "备份状态", _check_backup),
    ("disk", "磁盘余量", _check_disk),
    ("permissions", "文件权限", _check_permissions),
    ("cli", "全局 CLI", _check_cli),
]


async def run_checks() -> dict:
    """执行全部检查并返回汇总。网页端 /api/doctor 与 CLI `choyeonctl doctor` 共用。"""
    items = []
    for cid, title, fn in _CHECKS:
        try:
            res = await fn()
        except Exception as e:  # noqa: BLE001 单项检查失败不应拖垮整个自检
            res = {"status": "warn", "detail": f"检查出错：{e}", "fix": ""}
        items.append({"id": cid, "title": title, "status": res["status"], "detail": res["detail"], "fix": res["fix"]})

    counts = {
        "pass": sum(1 for i in items if i["status"] == "pass"),
        "warn": sum(1 for i in items if i["status"] == "warn"),
        "fail": sum(1 for i in items if i["status"] == "fail"),
    }
    overall = "fail" if counts["fail"] else ("warn" if counts["warn"] else "pass")
    return {"status": overall, "counts": counts, "items": items}


@router.get("/api/doctor")
async def doctor(req: Request):
    if req.state.cp_role != "admin":
        return JSONResponse(status_code=403, content={"error": "仅管理员可执行自检"})
    return await run_checks()
