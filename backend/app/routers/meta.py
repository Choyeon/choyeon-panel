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

from .. import aio_db, config
from .. import database as dbm
from ..log import warn
from ..services import (
    alerts_service,
    cert_ops,
    exposure_service,
    load_service,
    login_audit_service,
    memory_service,
    pending_service,
    storage_service,
    systemd_ops,
    updates_service,
)
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
        # /api/ready 在免登录名单里（deps 的公开前缀）。SQLite 的 OSError 带数据文件绝对路径，
        # PG 的连接失败原文带 host/port 甚至 `password authentication failed for user "x"`，
        # 原样回显等于让任何能连上端口的人读出基础设施细节。细节进日志，对外只有 fail。
        warn("[ready]", "database", type(e).__name__, str(e)[:500])
        checks["database"] = "fail"

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
    # 判据是「有没有 admin」而不是「有没有用户」：只剩 viewer 的机器（删过管理员）
    # 按 COUNT(*) 会显示 pass，而那种人其实已经进不去后台了。
    # 修复命令一律走 --password-stdin：--password 会把口令留在 ps、/proc/<pid>/cmdline
    # 和 shell 历史里，同机任何用户都读得到（AGENTS.md §7 同一条）。
    total = dbm.query_one("SELECT COUNT(*) c FROM users")["c"]
    admins = dbm.query_one("SELECT COUNT(*) c FROM users WHERE role='admin'")["c"]
    if admins == 0:
        return {
            "status": "fail",
            "detail": f"没有管理员账号（共 {total} 个账号）" if total else "未创建管理员，任何人都能抢注",
            "fix": "printf '%s' \"$PW\" | choyeonctl user create <name> --password-stdin --role admin",
        }
    return {"status": "pass", "detail": f"{admins} 个管理员 / 共 {total} 个账号", "fix": ""}


async def _check_service() -> dict:
    if not (shutil.which("systemctl") and os.path.isdir("/run/systemd/system")):
        return {
            "status": "warn",
            "detail": "未检测到 systemd（容器/WSL），需手动启动",
            "fix": "backend/.venv/bin/python -m app.main",
        }
    active = await systemd_ops.is_active(f"{SERVICE_NAME}.service")
    if not active:
        # 带上 reset-failed：进入 failed（StartLimitBurst=5）后光 restart 会被拒，
        # 照着这条 fix 敲的人会遇到第二个错误，以为修好了其实还没。
        return {"status": "fail", "detail": f"{SERVICE_NAME} 未在运行",
                "fix": f"systemctl reset-failed {SERVICE_NAME} && systemctl restart {SERVICE_NAME}"}
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
    # 两条线都取自设置页那条配置（alert_disk_pct）：曾经这里写着 90/75 两个字面量，
    # 管理员把线调到 70 之后，告警在响、网页仪表是红的，而 `choyeonctl doctor` 这里
    # 仍报 pass——同一块盘三个答案。
    dt = alerts_service.disk_thresholds()
    if pct >= dt["crit"]:
        return {
            "status": "fail",
            "detail": f"磁盘已用 {pct}%（要紧线 {dt['crit']}%），数据库写入可能失败",
            "fix": "清理旧备份与 journalctl --vacuum-size=200M",
        }
    if pct >= dt["warn"]:
        return {"status": "warn", "detail": f"磁盘已用 {pct}%（警戒线 {dt['warn']}%）",
                "fix": "关注增长趋势，必要时清理备份"}
    return {"status": "pass", "detail": f"磁盘已用 {pct}%（警戒线 {dt['warn']}%）", "fix": ""}


async def _check_frontend() -> dict:
    if Path(config.WEB_DIST / "index.html").exists():
        return {"status": "pass", "detail": "前端产物已构建", "fix": ""}
    return {"status": "fail", "detail": "web/dist 不存在，访问会 404", "fix": "cd web && npm run build"}


async def _check_permissions() -> dict:
    """密钥与数据库的落地权限，一项看三个对象。

    panel.db 里是 users.pass_hash 和 settings.jwt_secret，签名密钥一旦外泄就等于任何人都
    能签一枚 admin token，而 admin 有 root 终端；`.env` 里是同一批配置明文。这三样按默认
    umask 落地是 0755/0644，同机随便一个业务账户都能整份读走。
    """
    targets = (
        (Path(config.ENV_FILE), "0600"),
        (Path(config.DATA_DIR), "0700"),
        (Path(config.DATA_DIR) / "panel.db", "0600"),
    )
    bad = []
    for p, want in targets:
        try:
            mode = p.stat().st_mode & 0o777
        except OSError:
            continue  # 不存在的不判（开发机上根本没有 .env）
        if mode & 0o077:
            bad.append((p, mode, want))
    if not bad:
        return {"status": "pass", "detail": ".env 与数据目录权限正常", "fix": ""}
    return {
        "status": "warn",
        "detail": "；".join(f"{p} 是 {oct(m)}，同机其他用户可读" for p, m, _ in bad),
        "fix": " && ".join(f"chmod {w} {p}" for p, _, w in bad),
    }


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

    判定口径复用 cert_ops.timer_status()：站点页的续期提示和自检各查一套的话，会出现
    「网页说已托管、自检说没有」这种互相打脸的结果，而用户只会相信先看到的那个。
    """
    if not shutil.which("certbot"):
        return {"status": "pass", "detail": "未安装 certbot，无续期需求", "fix": ""}
    live = Path("/etc/letsencrypt/live")
    certs = sorted(p.parent.name for p in live.glob("*/fullchain.pem")) if live.is_dir() else []
    if not certs:
        return {"status": "pass", "detail": "certbot 已装但暂无证书", "fix": ""}
    names = ", ".join(certs[:5])
    t = await cert_ops.timer_status()
    if t["status"] == "installed":
        if t.get("checked") == "systemd-unit":
            # 单元文件在，但 list-timers 里没有它 = 没被 systemd 装载（没 enable），
            # 到期照样不会有人去续——这种情况不能说 pass。
            unit = t.get("unit") or "certbot.timer"
            return {
                "status": "warn",
                "detail": f"{len(certs)} 张证书的 {unit} 只有单元文件、未被 systemd 装载，不会自动续期",
                "fix": f"systemctl enable --now {unit}",
            }
        return {"status": "pass", "detail": f"{len(certs)} 张证书有自动续期（{names}）：{t['detail']}", "fix": ""}
    if t["status"] == "unknown":
        # 查不了 ≠ 没有。说成"没有"会让人去装一个其实已经在跑的定时器
        return {
            "status": "warn",
            "detail": f"无法确认 {len(certs)} 张证书有没有自动续期：{t['detail']}",
            "fix": "systemctl list-timers --all | grep -iE 'certbot|letsencrypt'",
        }
    return {
        "status": "warn",
        "detail": f"{len(certs)} 张证书没有任何自动续期入口，到期会直接失效",
        "fix": "面板「定时任务」页新建 certbot renew（每日一次），"
               "或写入 cron：0 3 * * * certbot renew --non-interactive",
    }


# 第三项是「库里哪些行**应该**在盘上有 unit」的判据：备份走 schedule/hour/minute，
# 定时任务只看 enabled。把 manual 备份和禁用任务算成"单元缺失"会变成永久性误报。
_TIMER_KINDS = (
    ("panel-cron", "cron_task", "enabled=1"),
    ("panel-backup", "backups", "enabled=1 AND schedule!='manual'"),
)


async def _check_timer_units() -> dict:
    """盘上的 panel-cron-* / panel-backup-* 单元必须和库里的行一一对应。

    本机实测残留过一对没有主人的 panel-cron timer（早期把 unit 目录写死成
    /etc/systemd/system 的测试留下的），它每 5 分钟真的执行一次 certbot renew：
    unit 文件在盘上就是 systemd 的事实，而面板的列表只读数据库，
    于是多出一个既看不见也关不掉的定时器。反方向同样要紧——库里的任务开着、
    unit 却没了（恢复备份、手工删文件），界面显示"已挂表"但它永远不会跑。
    """
    unit_dir = Path(config.UNIT_DIR)
    if not unit_dir.is_dir():
        return {"status": "pass", "detail": "本机无 systemd 单元目录，跳过定时器对账", "fix": ""}
    orphans: list[str] = []
    missing: list[str] = []
    wants = unit_dir / "timers.target.wants"
    for prefix, table, should_have in _TIMER_KINDS:
        # 表名与条件都来自上面的常量，不来自请求
        rows = await aio_db.query(f"SELECT id FROM {table} WHERE {should_have}")
        ids = {str(r["id"]) for r in rows}
        on_disk = set()
        for p in glob.glob(str(unit_dir / f"{prefix}-*.timer")):
            stem = Path(p).name[: -len(".timer")]
            tid = stem.rsplit("-", 1)[-1]
            on_disk.add(tid)
            if tid not in ids:
                live = "（仍在按计划执行）" if (wants / f"{stem}.timer").exists() else ""
                orphans.append(f"{stem}.timer{live}")
        for tid in sorted(ids - on_disk):
            missing.append(f"{prefix}-{tid}")
    if orphans or missing:
        parts = []
        if orphans:
            parts.append("没有对应任务的残留单元：" + "、".join(sorted(orphans)[:6]))
        if missing:
            parts.append("已启用但单元文件不存在：" + "、".join(sorted(missing)[:6]))
        fix = "；".join(
            f"systemctl disable --now {o.split('（')[0]} && rm -f {unit_dir}/{o.split('（')[0]} "
            f"{unit_dir}/{o.split('（')[0].replace('.timer', '.service')}"
            for o in sorted(orphans)[:6]
        ) or "在对应页面重新保存一次任务（面板会重新渲染 unit）"
        return {"status": "warn", "detail": "；".join(parts), "fix": fix}
    return {"status": "pass", "detail": "定时器单元与任务记录一一对应", "fix": ""}


async def _check_updates() -> dict:
    """待装补丁：把"这台机器还差哪些安全更新"变成自检里能看见的一行。

    严格只读（不 `apt update`、不装包，理由见 updates_service 的模块说明），结果走 TTL
    缓存，所以自检页反复刷新不会反复惊动包管理器，也不会和别的进程抢 dpkg 锁。
    """
    return updates_service.doctor_item(await updates_service.detect())


async def _check_ports() -> dict:
    """端口暴露面：把"哪些端口能被外面碰到"变成自检里能看见的一行。

    已有的 listen 项只看面板自己绑在哪，firewall 项只看规则表，两者都对，
    合起来却答不出"我有没有在不知情的情况下开了个口子"——这一项就是补这个洞。
    判定走 exposure_service，TTL 缓存，自检页反复刷新不会反复跑 ss 和 ufw status。
    """
    return exposure_service.doctor_item(await exposure_service.audit())


async def _check_login() -> dict:
    """登录安全：SSH 才是这台机器的正门，面板自己被 nginx 挡在 127.0.0.1 后面。

    这一项把四件平时各在一处的事拼成一行判词：sshd 的有效配置、authorized_keys
    的指纹与文件权限、账号面（空口令 / 多余 uid=0 / sudo 成员）、过去 24 小时的
    失败认证来源，外加 fail2ban 或 sshguard 到底在不在跑。
    全部只读：本机 SSH 是唯一入口，配置改错就是把人锁在门外，所以这里只给可复制的
    修复命令，绝不碰 sshd_config。
    """
    return login_audit_service.doctor_item(await login_audit_service.audit())


async def _check_storage() -> dict:
    """存储占用：disk 那一项只说"还剩多少"，说不出"是被谁占掉的"。

    这一项把面板数据、备份目录、journald 落盘处和 FILE_ROOTS 登记的目录各扫一遍，
    给每个根的一级子目录占用榜，再把数字折成判词（日志过大、备份攒了多少份、
    分区吃紧、单个超大文件）。严格只读：不删文件、不改配额，清理动作只给可复制的
    命令。走 TTL 缓存，自检页反复刷新不会反复扫盘。
    """
    return storage_service.doctor_item(await storage_service.audit())


async def _check_memory() -> dict:
    """内存压力：概览的内存仪表只说"占了多少"，这一项回答"有没有在缺、谁在拿着、杀过没有"。

    一台"已用 60%"的机器照样能因为直接回收卡住请求、因为一次突发分配被 OOM 杀掉一个进程，
    而这两件事在总量曲线上都不存在。这一项读 meminfo / vmstat 两个快照 / PSI / cgroup 的
    memory.* 与 pids.* / 每进程 RSS，把内核 OOM 计数摆到台面上。
    严格只读：不 kill 进程、不改 sysctl、不动 unit 的 MemoryMax/MemoryHigh——上限该设多少
    只有管理员知道业务峰值。走 TTL 缓存，反复刷新不会反复采样。
    """
    return memory_service.doctor_item(await memory_service.audit())


async def _check_pending() -> dict:
    """改动未生效：补丁装上了、配置改了，但还在跑的进程没吃到它。

    updates / ports / login / storage / memory 这几项回答的都是「现在是什么样」，管理员真正会
    踩的坑在另一头：apt 把 liblzma 升到 u2 了，可 postgres 是十几天前起的，还踩着那份已经被
    unlink 掉的旧库在跑；nginx 的 conf 昨晚改过，而它的 worker 是今早才换的。这类状态在别的
    页面上全是绿的——unit 在跑、端口在听、补丁清单也确实是空的。
    严格只读：不 restart 任何服务、不 reload nginx/sshd、不 daemon-reload、不 reboot。什么时候
    能断业务连接重启一个服务只有管理员知道，面板替你按下去就是在业务高峰里掐连接。走 TTL 缓存。
    """
    return pending_service.doctor_item(await pending_service.audit())



async def _check_load() -> dict:
    """负载与 CPU 归因：机器有多忙、这份忙是谁造成的、有没有被自己的 CPU 配额压住。

    概览的 CPU 曲线只说了"过去几分钟平均用了多少"，它回答不了两件相反的事：load 高是 CPU 不够还是
    磁盘在拖（前者加核有用，后者加核毫无用处），以及 2 核机器上的 load 4 到底算不算挤。更隐蔽的是
    第三种忙——unit 写着 CPUQuota=80% 时，进程被限流的那段时间既不在 run queue 也不进 D 状态，
    load 和曲线都好看，只有 cgroup 的 cpu.stat 在涨；面板自己就跑在这种配置下。
    严格只读：不 renice、不 kill、不 restart 服务、不改 CPUQuota= / cpu.weight。放宽配额是容量决策
    （这台机器只有 2 核，配额本来就是拿去保业务进程的），杀进程与重启会直接断业务。走 TTL 缓存。
    """
    return load_service.doctor_item(await load_service.audit())


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
    ("timer-units", "定时器对账", _check_timer_units),
    ("updates", "系统补丁", _check_updates),
    ("ports", "端口暴露面", _check_ports),
    ("login", "登录安全", _check_login),
    ("storage", "存储占用", _check_storage),
    ("memory", "内存压力", _check_memory),
    ("pending", "改动未生效", _check_pending),
    ("load", "负载与CPU", _check_load),
]

# 可一键修复的项：只放"执行固定命令、无歧义、失败可重试"的操作。
# firewall（可能锁死 SSH）、nginx（要人先看报错）、listen/admin/disk
# 都需要人来决策，不提供按钮，只给可复制的命令。
# timer-units 也不放：删 unit 文件等于删掉一个可能正在按计划执行的定时器，
# 先要人确认它到底属于哪一行任务。
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


@router.get("/api/updates")
async def updates(refresh: int = 0):
    """待装补丁清单：只读探测，viewer 也能看——它不返回任何可写信息。"""
    return await updates_service.detect(refresh=bool(refresh))


@router.get("/api/ports")
async def ports(refresh: int = 0):
    """监听端口与可达性判定：只读探测，viewer 也能看。"""
    return await exposure_service.audit(refresh=bool(refresh))


@router.get("/api/ssh-audit")
async def ssh_audit(refresh: int = 0):
    """登录安全对账：只读探测，viewer 也能看；修复动作一律回 SSH 里手工做。"""
    return await login_audit_service.audit(refresh=bool(refresh))


@router.get("/api/storage")
async def storage(refresh: int = 0):
    """存储占用对账：只读扫描，viewer 也能看；清理动作一律回 CLI 里手工做。"""
    return await storage_service.audit(refresh=bool(refresh))


@router.get("/api/memory")
async def memory(refresh: int = 0):
    """内存压力与 OOM 对账：只读探测，viewer 也能看；处置命令一律回 CLI 或 SSH 里手工做。"""
    return await memory_service.audit(refresh=bool(refresh))


@router.get("/api/pending")
async def pending(refresh: int = 0):
    """改动未生效对账：只读探测，viewer 也能看；重启/重载动作一律回 CLI 或 SSH 里手工做。"""
    return await pending_service.audit(refresh=bool(refresh))


@router.get("/api/load")
async def load(refresh: int = 0):
    """负载与 CPU 归因对账：只读探测，viewer 也能看；处置命令一律回 CLI 或 SSH 里手工做。"""
    return await load_service.audit(refresh=bool(refresh))



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
            # 必须先 reset-failed：unit 配了 StartLimitBurst=5，服务反复崩溃进入 failed
            # 之后，systemctl restart 会直接被 "Start request repeated too quickly" 拒绝。
            # 旧代码在这种情况下照样回"正在重启"，按钮按下去面板永远起不来——而这正是
            # 这个按钮唯一要救的场景。
            await asyncio.create_subprocess_shell(
                f"systemctl reset-failed {SERVICE_NAME}; systemctl restart {SERVICE_NAME}",
                start_new_session=True,
            )

        asyncio.create_task(_restart())
        return {"ok": True, "restarting": True, "message": "面板服务正在重启，约 5 秒后自动恢复"}

    await aio_db.audit(req.state.cp_sub, "doctor:fix", fid)
    check = next((fn for cid, _, fn in _CHECKS if cid == fid), None)
    item = None
    if check:
        res = await check()
        title = next((t for cid, t, _ in _CHECKS if cid == fid), fid)
        item = {
            "id": fid, "title": title, "status": res["status"],
            "detail": res["detail"], "fix": res["fix"], "fixable": True,
        }
    return {"ok": r["code"] == 0, "message": msg, "item": item}
