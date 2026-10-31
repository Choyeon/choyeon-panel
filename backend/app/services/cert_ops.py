"""证书总览的数据来源：certbot 优先，读不到就退回 openssl 直接读证书文件。

两条硬规则：
1. 查不到就如实说（reason / unknown），绝不编一张"看起来正常"的证书出来。面板上出现过
   「UI 声称续期由面板托管、机器上其实没有任何定时器」，证书一路静默过期到点才被发现。
2. 外部命令一律走 argv（util.run），证书名先过白名单再落审计，不拼 shell 字符串；
   给浏览器看的文本里不带本机绝对路径（OSError 只取异常类型名，不取 str(e)）。
"""

import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .. import config
from ..util import name_list, run
from .alerts_service import CERTBOT_TIMEOUT, parse_certbot, setting_int

# 「还剩几天算快到期」这条线不在这里定义，见 status_of()：它读的是告警配置 alert_ssl_days。
# 只用于切分 certbot 的输出块；到期天数/无效标记仍然复用 alerts_service.parse_certbot，
# 这里刻意不 import 它的私有正则——那个文件在并行改动，私有名随时会挪位置。
_CERT_MARK_RE = re.compile(r"Certificate Name: (\S+)")
_DOMAINS_RE = re.compile(r"^\s*Domains:\s*(.+)$", re.M)
_EXPIRY_RE = re.compile(r"Expiry Date:\s*(.+?)\s*(?:\(|$)", re.M)
_CERT_PATH_RE = re.compile(r"Certificate Path:\s*(\S+)")

_NOT_AFTER_RE = re.compile(r"notAfter=(.+)")
_ISSUER_RE = re.compile(r"^issuer=(.*)$", re.M)
_SUBJECT_RE = re.compile(r"^subject=(.*)$", re.M)
_DNS_RE = re.compile(r"DNS:([A-Za-z0-9.*_-]+)")
_OPENSSL_TIME = "%b %d %H:%M:%S %Y %Z"

# certbot 的 --cert-name 允许点号与连字符，但绝不能有斜杠或空白：
# 它既会进审计 detail，也可能被拼进后续 certbot 参数，先按最严的白名单收。
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

_TIMER_UNITS = ("certbot.timer", "certbot-renew.timer", "letsencrypt.timer", "letsencrypt-renew.timer")
_TIMER_UNIT_DIRS = ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")
_TIMER_HINTS = ("certbot", "letsencrypt")
_CRON_DIRS = ("/etc/cron.d", "/etc/cron.daily", "/etc/cron.hourly")


def valid_cert_name(name: str) -> bool:
    return bool(_VALID_NAME_RE.match(name or ""))


def status_of(days_left: int | None, threshold: int, invalid: bool = False) -> str:
    """ok / expiring / expired；天数拿不到时返回 unknown，而不是默认"没事"。

    `threshold` 就是那条生效的告警配置（`alert_ssl_days`，单位：天）。证书页标"即将到期"
    和巡检决定发不发通知，读的是同一个数；以前这里写死 21 而配置默认 14，于是把线调到 30
    时第 30 天通知已经响了、页面上还是"正常"，调到 7 时列表满屏"即将到期"却一条都不发。
    配置为 0 表示证书提醒已关：只判过没过期，不再标 expiring。
    """
    if days_left is None:
        return "expired" if invalid else "unknown"
    if days_left < 0:
        return "expired"
    if threshold > 0 and days_left <= threshold:
        return "expiring"
    return "ok"


def _days_left(expiry: datetime | None, now: datetime) -> int | None:
    if expiry is None:
        return None
    return int((expiry - now).total_seconds() // 86400)


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parse_openssl_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw.strip(), _OPENSSL_TIME)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC)


def _blocks(output: str) -> list[tuple[str, str]]:
    """按 "Certificate Name:" 把 certbot 输出切成 (name, 原文块)。"""
    marks = list(_CERT_MARK_RE.finditer(output))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(output)
        out.append((m.group(1), output[m.start():end]))
    return out


def _row(name, domains, issuer, expiry, days_left, status, invalid):
    return {
        "name": name,
        "domains": domains,
        "issuer": issuer,
        "expiry": expiry.isoformat() if expiry else None,
        "days_left": days_left,
        "status": status,
        "invalid": invalid,
        # certbot 的 lineage 名（== /etc/letsencrypt/live 下的目录名），续期与排障都靠它定位
        "certbot_id": name,
    }


def _split_domains(raw: str | None) -> list[str]:
    return [d for d in (raw or "").split() if d]


async def _openssl_x509(path: str) -> dict:
    """读单张证书文件的 enddate / issuer / SAN。

    老版本 openssl 不认 `-ext subjectAltName`，整条命令会失败；这种失败和"文件不存在"
    在输出上分不开，所以先用全参数集，失败再退回最小参数集重试一次。
    """
    args = ["x509", "-in", path, "-noout", "-enddate", "-issuer", "-subject", "-ext", "subjectAltName"]
    r = await run("openssl", args, timeout=30)
    if r["code"] != 0:
        r = await run("openssl", ["x509", "-in", path, "-noout", "-enddate", "-issuer"], timeout=30)
    if r["code"] != 0:
        return {}
    out = r["out"] or ""
    m = _NOT_AFTER_RE.search(out)
    mi = _ISSUER_RE.search(out)
    domains = []
    for d in _DNS_RE.findall(out):
        if d not in domains:
            domains.append(d)
    if not domains:
        ms = _SUBJECT_RE.search(out)
        cn = _field_value(ms.group(1), "CN") if ms else None
        if cn:
            domains = [cn]
    return {
        "expiry": _parse_openssl_time(m.group(1)) if m else None,
        "issuer": _issuer_label(mi.group(1)) if mi else None,
        "domains": domains,
    }


def _field_value(dn: str, key: str) -> str | None:
    for kv in dn.split(","):
        k, _, v = kv.partition("=")
        if k.strip().upper() == key and v.strip():
            return v.strip()
    return None


def _issuer_label(raw: str) -> str | None:
    """签发者取机构名（O），退化到 CN，再退化到原文——一长串 DN 在表格里没法读。"""
    return _field_value(raw, "O") or _field_value(raw, "CN") or (raw.strip() or None)


async def _from_certbot(now: datetime, threshold: int) -> tuple[list | None, str | None]:
    """(certs, reason)；certs 为 None 表示 certbot 这条来源不可用，[] 表示可用但确实没证书。"""
    r = await run("certbot", ["certificates"], timeout=CERTBOT_TIMEOUT)
    if r["code"] != 0:
        # 只带退出码与一小段摘要：certbot 的报错正文里满是 /etc/letsencrypt/... 绝对路径
        return None, f"certbot 不可用（exit {r['code']}）"
    summary = {c["name"]: c for c in parse_certbot(r["out"] or "")}
    rows = []
    for name, block in _blocks(r["out"] or ""):
        info = summary.get(name) or {}
        m = _EXPIRY_RE.search(block)
        expiry = _parse_iso(m.group(1)) if m else None
        days_left = _days_left(expiry, now)
        if days_left is None:
            days_left = info.get("days")  # certbot 自己算好的整天数，比没有强
        invalid = bool(info.get("invalid"))
        dm = _DOMAINS_RE.search(block)
        domains = _split_domains(dm.group(1) if dm else None) or [name]
        pm = _CERT_PATH_RE.search(block)
        issuer = None
        if pm:
            issuer = (await _openssl_x509(pm.group(1))).get("issuer")
        rows.append(_row(name, domains, issuer, expiry, days_left,
                         status_of(days_left, threshold, invalid), invalid))
    return rows, None


async def _from_openssl(now: datetime, threshold: int) -> tuple[list | None, str | None]:
    """certbot 缺失的机器：直接把 TLS_DIR 下每张 fullchain.pem 读出来。"""
    live = Path(config.TLS_DIR)
    try:
        entries = sorted(p.name for p in live.iterdir() if (p / "fullchain.pem").is_file())
    except OSError as e:
        # 只要异常类型：str(e) 会把本机绝对路径写进接口响应
        return None, f"证书目录不可读（{type(e).__name__}）"
    rows = []
    for name in entries:
        meta = await _openssl_x509(f"{config.TLS_DIR}/{name}/fullchain.pem")
        expiry = meta.get("expiry")
        days_left = _days_left(expiry, now)
        rows.append(_row(name, meta.get("domains") or [name], meta.get("issuer"), expiry, days_left,
                         status_of(days_left, threshold), False))
    return rows, None


async def list_certs(now: datetime | None = None, threshold: int | None = None) -> dict:
    """返回 {"certs": [...], "source": "certbot|openssl|none", "reason": str|None,
    "expiring_days": int}。

    `expiring_days` 就是这批证书被判"即将到期"用的那条线（来自 `alert_ssl_days`），
    随载荷一起给出去，页面才不用再自己抄一个数字、也不用猜后端按什么判的。
    """
    ref = now or datetime.now(UTC)
    line = setting_int("alert_ssl_days") if threshold is None else threshold
    certbot_certs, certbot_reason = await _from_certbot(ref, line)
    if certbot_certs:
        return {"certs": certbot_certs, "source": "certbot", "reason": None, "expiring_days": line}

    live_certs, live_reason = await _from_openssl(ref, line)
    if live_certs:
        # certbot 不在也能给出准确清单，但口径变了（没有 lineage 之外的元数据），reason 要带回去
        return {"certs": live_certs, "source": "openssl", "reason": certbot_reason, "expiring_days": line}

    if certbot_reason is None and live_reason is None:
        return {"certs": [], "source": "none", "expiring_days": line,
                "reason": "本机没有证书（certbot 与证书目录都为空）"}
    if certbot_reason is None:
        return {"certs": [], "source": "certbot", "expiring_days": line,
                "reason": "certbot 报告本机没有证书"}
    return {"certs": [], "source": "none", "expiring_days": line,
            "reason": f"{certbot_reason}；{live_reason or '证书目录为空'}"}


async def _check_cron() -> dict | None:
    """cron 这条线索：None 表示"查不了"，调用方据此区分 missing 与 unknown。

    把"读不到"说成"没配置"是最坏的一类误报——用户会以为只要去装定时器，
    而实际是面板根本没有权限/命令去确认。
    """
    readable = False
    for d in _CRON_DIRS:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        readable = True
        for n in sorted(names):
            try:
                text = Path(f"{d}/{n}").read_text(errors="replace")
            except OSError:
                continue
            low = text.lower()
            if "certbot" in low and "renew" in low:
                return {"status": "installed", "checked": "cron", "detail": f"cron 任务 {n} 里有 certbot renew"}
    ct = await run("crontab", ["-l"], timeout=20)
    if ct["code"] == 0 or ct["code"] == 1:
        # exit 1 = 该用户确实没有 crontab，属于"查到了，没有"；127 才是命令不存在
        readable = True
        low = (ct["out"] or "").lower()
        if "certbot" in low and "renew" in low:
            return {"status": "installed", "checked": "crontab", "detail": "当前用户 crontab 里有 certbot renew"}
    if not readable:
        return None
    return {"status": "missing", "checked": "cron", "detail": "cron 里也没有 certbot renew 任务"}


async def timer_status() -> dict:
    """这台机器到底有没有在自动续期 —— 只报实测到的事实。

    status: installed / missing / unknown。unknown 专供"两种检查方式都跑不了"的机器
    （无 systemd 的容器、没有 cron 的精简镜像），此时 UI 不能宣称续期已被托管。
    """
    listed = await run("systemctl", ["list-timers", "--all", "--no-legend", "--plain"], timeout=20)
    if listed["code"] == 0:
        for line in (listed["out"] or "").splitlines():
            low = line.lower()
            if any(h in low for h in _TIMER_HINTS):
                return {"status": "installed", "checked": "systemd", "detail": line.strip()[:200]}
        for d in _TIMER_UNIT_DIRS:
            for u in _TIMER_UNITS:
                if os.path.exists(f"{d}/{u}"):
                    # unit 带出去给自检用：文件在 list-timers 里没出现，说明它没被装载
                    # （多半没 enable），修复命令就是 enable 这一个单元，不能说成"已托管"。
                    return {"status": "installed", "checked": "systemd-unit", "unit": u,
                            "detail": f"存在 {u} 单元文件"}
        cron = await _check_cron()
        if cron:
            return cron
        return {"status": "missing", "checked": "systemd", "detail": "systemd 定时器与 cron 里都没有 certbot 续期任务"}

    cron = await _check_cron()
    if cron:
        return cron
    return {
        "status": "unknown",
        "checked": None,
        "detail": f"systemd 定时器查询失败（exit {listed['code']}），且本机没有可检查的 cron",
    }


def earliest_expiry(certs: list[dict]) -> dict | None:
    """给 UI 的「最早到期」芯片用：取 days_left 最小的一张（None 不参与比较）。"""
    best = None
    for c in certs:
        d = c.get("days_left")
        if d is None:
            continue
        if best is None or d < best["days_left"]:
            best = c
    return best


def renewal_hint(timer: dict | None, certs: list[dict]) -> str:
    """续期能力的诚实结论，供 UI 顶栏直接显示（不做乐观假设）。"""
    expiring = [c for c in certs if c.get("status") in ("expiring", "expired")]
    st = (timer or {}).get("status")
    if st == "installed":
        return "系统已安装续期定时器，certbot 会自动尝试续期；面板的续期按钮用于立即验证一次。"
    if expiring:
        # 总数写在前面：这句是页面顶栏唯一会说"有几张要到期"的地方，
        # 只点三个名字而不说一共几张，第四张到期的人看到的就是"三张"。
        names = name_list([c["name"] for c in expiring], 3, sep=", ", unit="张")
        return (f"未检测到续期定时器，且 {len(expiring)} 张证书即将或已经到期：{names}。"
                "请尽快手动配置 systemd timer 或 cron。")
    if st == "missing":
        return "未检测到续期定时器：证书到期不会自动续，需要自己配置 systemd timer / cron，或到期前手动续期。"
    return "无法确认本机是否安装了续期定时器（systemd 与 cron 都查不到），请自行核实后再依赖自动续期。"
