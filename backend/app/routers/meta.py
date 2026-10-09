"""元信息接口：就绪探针、部署模板、安全与健康自检（doctor）。

doctor 只做**只读**检查，不修改任何系统状态；每项给出状态与修复建议命令，
供网页端「自检」页与 CLI `choyeonctl doctor` 共用。
"""

from __future__ import annotations

import asyncio
import glob
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config
from .. import aio_db
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
            # 面板自带 systemd timer（备份页给任务开启定时即渲染 panel-backup-<id>.timer，
            # 且备份列表会显示下次触发时间）；crontab + backup.sh 是脱离面板的备选，
            # 不能再当成首选建议——那是"备份"功能还不存在时写下的话。
            "fix": "在「备份」页给备份任务开启定时（systemd timer），"
                   f"或先手动补一份：choyeonctl backup run；"
                   f"也可 crontab 加 0 4 * * * {config.BASE}/scripts/backup.sh",
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


async def _check_cert_renewal() -> dict:
    """证书装了不代表会自动续期——面板 UI 曾写着"每日定时任务已托管"，而机器上并没有 timer。

    这里只回答一个问题：有没有人（系统 timer / cron / 面板自建 timer）会按期续期。
    查不到就如实 warn，不猜、也不算 pass。
    """
    if not shutil.which("certbot"):
        return {"status": "pass", "detail": "未安装 certbot，无续期需求", "fix": ""}
    live = Path("/etc/letsencrypt/live")
    certs = sorted(live.glob("*/fullchain.pem")) if live.is_dir() else []
    if not certs:
        return {"status": "pass", "detail": "certbot 已装但暂无证书", "fix": ""}
    names = ", ".join(p.parent.name for p in certs[:5])
    for unit in ("certbot.timer", "certbot-renew.timer", "choyeon-certbot.timer"):
        r = await run("systemctl", ["is-enabled", unit], timeout=10)
        if (r["out"] or "").strip() == "enabled":
            return {"status": "pass", "detail": f"{len(certs)} 张证书由 {unit} 续期（{names}）", "fix": ""}
    cron = await run("crontab", ["-l"], timeout=10)
    if "certbot" in (cron["out"] or ""):
        return {"status": "pass", "detail": f"{len(certs)} 张证书由 crontab 续期（{names}）", "fix": ""}
    return {
        "status": "warn",
        "detail": f"{len(certs)} 张证书没有任何自动续期入口，到期会直接失效",
        "fix": "choyeonctl app ... 或 systemctl enable --now certbot.timer",
    }


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
    ("cert-renew", "证书续期", _check_cert_renewal),
]

# 可一键修复的项：只放"执行固定命令、无歧义、失败可重试"的操作。
# firewall（可能锁死 SSH）、nginx（要人先看报错）、listen/admin/disk
# 都需要人来决策，不提供按钮，只给可复制的命令。
FIXABLE = {"cli", "permissions", "service", "frontend"}


async def run_checks() -> dict:
    """执行全部检查并返回汇总。网页端 /api/doctor 与 CLI `choyeonctl doctor` 共用。"""
    items = []
    for cid, title, fn in _CHECKS:
        try:
            res = await fn()
        except Exception as e:  # noqa: BLE001 单项检查失败不应拖垮整个自检
            res = {"status": "warn", "detail": f"检查出错：{e}", "fix": ""}
        items.append(
            {
                "id": cid, "title": title, "status": res["status"],
                "detail": res["detail"], "fix": res["fix"], "fixable": cid in FIXABLE,
            }
        )

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


@router.post("/api/doctor/fix")
async def doctor_fix(req: Request):
    """一键修复白名单项：命令全部是服务端拼好的固定 argv，不接收外部命令。"""
    if req.state.cp_role != "admin":
        return JSONResponse(status_code=403, content={"error": "仅管理员可执行修复"})
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = {}
    fid = str((body or {}).get("id") or "")
    if fid not in FIXABLE:
        return JSONResponse(status_code=400, content={"error": "该项不支持一键修复，请按修复命令手动处理"})

    if fid == "cli":
        target = Path(config.BASE) / "bin" / "choyeonctl"
        if not target.exists():
            return JSONResponse(status_code=400, content={"error": f"未找到 {target}，仓库可能被移动"})
        r = await run("ln", ["-sf", str(target), str(CLI_LINK)])
        msg = "已重建 choyeonctl 链接" if r["code"] == 0 else f"ln 失败：{r['out']}"
    elif fid == "permissions":
        env_file = Path(config.ENV_FILE)
        if not env_file.exists():
            return JSONResponse(status_code=400, content={"error": ".env 不存在，无需修复"})
        r = await run("chmod", ["0600", str(env_file)])
        msg = "已收紧 .env 权限为 0600" if r["code"] == 0 else f"chmod 失败：{r['out']}"
    elif fid == "frontend":
        r = await run("bash", ["-c", f"cd {config.BASE}/web && npm run build"], timeout=900)
        if r["code"] != 0:
            return JSONResponse(status_code=400, content={"error": f"前端构建失败：{r['out'][-400:]}"})
        msg = "前端已重新构建"
    else:  # service：重启自己——响应必须赶在进程退出前发出去
        await aio_db.audit(req.state.cp_sub, "doctor:fix", fid)

        async def _restart():
            # 延迟 1s：先把 JSON 响应发完再 kill，否则客户端只会收到连接中断
            await asyncio.sleep(1)
            await asyncio.create_subprocess_shell(
                f"systemctl restart {SERVICE_NAME}", start_new_session=True,
            )

        asyncio.create_task(_restart())
        return {"ok": True, "restarting": True, "message": "面板服务正在重启，约 5 秒后自动恢复"}

    await aio_db.audit(req.state.cp_sub, "doctor:fix", fid)
    check = next((fn for cid, _, fn in _CHECKS if cid == fid), None)
    item = None
    if check:
        res = await check()
        title = next((t for cid, t, _ in _CHECKS if cid == fid), fid)
        item = {"id": fid, "title": title, "status": res["status"], "detail": res["detail"], "fix": res["fix"], "fixable": True}
    return {"ok": r["code"] == 0, "message": msg, "item": item}
