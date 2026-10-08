"""告警通道与巡检。

三条硬性约定（都是实测踩过的坑）：
1. 只走 http/https。urllib 默认 handler 认 `file://`，webhook 填 `file:///etc/shadow`
   会让面板带着自己的权限去读本地文件；发送前和保存时各校验一次。
2. `notify()` 返回是否真的投递成功。巡检据此决定要不要写"今天已发过"的标记——
   先前是发之前先标记，通道一抖当天告警就永久丢了。
3. 解析外部命令输出一律先确认命令跑成功（code=0），再把文本当结构化数据。
   systemctl 不在/没权限时 `run()` 返回 code!=0 但 out 是报错文本，
   旧解析把 "System has not been booted..." 拆成单元名 `System`，
   于是每 30 分钟推送一次"存在 failed 状态服务: System, Failed"。
"""

import asyncio
import datetime
import json
import os
import re
import urllib.request

from .. import config
from .. import database as dbm
from ..log import warn
from ..util import disk_usage_pct, is_http_url, is_unit, run


def _today() -> str:
    return datetime.date.today().isoformat()


def _int_setting(key: str, default: int, lo: int, hi: int) -> int:
    """读阈值并钳制到 [lo, hi]。

    保存路径已有校验，但这里必须再兜一层：老库里可能留着脏值（校验上线前的数据），
    `int('abc')` 抛 ValueError 会被巡检循环整轮吞掉，表现是"告警从此不响"，
    而网页端看不出任何异常——这是最难排查的失败模式，宁可回落到默认值。
    """
    raw = (dbm.get_setting(key) or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        warn(f"{key}={raw[:20]!r} 不是整数，回落到默认 {default}")
        return default
    if not lo <= n <= hi:
        warn(f"{key}={n} 超出 {lo}-{hi}，已钳制")
    return max(lo, min(n, hi))


def _post(url: str, payload: dict):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _telegram_configured(bot: str, chat: str) -> bool:
    """bot token 形如 `123456:ABC-DEF...`；填反了（chat 当 token）时 URL 会拼出畸形请求。"""
    return bool(bot and chat) and bool(re.match(r"^\d{6,}:[\w-]{20,}$", bot.strip()))


async def notify(text: str) -> bool:
    """推送一条告警，返回是否至少有一个通道投递成功。"""
    bot = (dbm.get_setting("alert_telegram_bot") or "").strip()
    chat = (dbm.get_setting("alert_telegram_chat") or "").strip()
    hook = (dbm.get_setting("alert_webhook_url") or "").strip()
    delivered = False
    if bot and chat:
        if not _telegram_configured(bot, chat):
            warn("telegram 配置不合法（应为 <bot_id>:<token> 形式），已跳过")
        else:
            try:
                await asyncio.to_thread(_post, f"https://api.telegram.org/bot{bot}/sendMessage",
                                        {"chat_id": chat, "text": f"[choyeon-panel] {text}"})
                delivered = True
            except Exception as e:  # noqa: BLE001
                warn("telegram notify failed:", e)
    if hook:
        if not is_http_url(hook):
            warn(f"webhook URL 必须是 http(s)，已拒绝并跳过：{hook[:60]}")
        else:
            try:
                await asyncio.to_thread(_post, hook, {"text": text})
                delivered = True
            except Exception as e:  # noqa: BLE001
                warn("webhook notify failed:", e)
    return delivered


async def _check(key: str, trigger: bool, message: str) -> None:
    if not trigger:
        return
    if dbm.get_setting("sent:" + key) == _today():
        return
    # 只有真投递成功才标记"今天已发"，否则本次告警当天不会再重试
    if await notify(message):
        dbm.set_setting("sent:" + key, _today())


# systemctl 输出的第一列一定是「带类型后缀的单元名」，正文不是。
# 只靠 is_unit 不够：UNIT_RE 允许纯字母，"System"、"Failed" 同样通过。
UNIT_TYPES = (".service", ".socket", ".target", ".device", ".mount", ".automount",
              ".swap", ".timer", ".path", ".slice", ".scope")


def _failed_units(out: str) -> list[str]:
    """从 `systemctl list-units --state=failed --no-legend --plain` 的输出取单元名。

    光靠 `split()[0]` 会把报错正文的首词当成单元——实测 systemd 不可用时
    "System has not been booted..." 解析出 `System`、"Failed to connect to bus"
    解析出 `Failed`，然后每 30 分钟推送一次「存在 failed 状态服务: System, Failed」。
    两道防线：调用方先确认 code==0，这里再要求必须是带类型后缀的合法单元名。
    """
    units: list[str] = []
    for line in out.splitlines():
        cols = line.strip().split()
        if not cols:
            continue
        name = cols[0]
        if not name.endswith(UNIT_TYPES) or not is_unit(name):
            continue
        if name not in units:
            units.append(name)
    return units


# certbot 的 `certificates` 输出里每张证书形如：
#   Certificate Name: a.example.com
#     Expiry Date: 2026-12-01 00:00:00+00:00 (VALID: 55 days)
# 状态括号里可能是 `VALID: 55 days` / `VALID: 1 day` / `VALID: 3 hour(s)`（不足一天）
# / `INVALID: EXPIRED, TEST_CERT`。旧代码找的是 `Days Left:` —— 任何 certbot 版本
# 都没打印过这个字段，所以 SSL 巡检一直是静默空转，证书过期也不会报警。
_CERT_NAME_RE = re.compile(r"Certificate Name: (\S+)")
_STATUS_RE = re.compile(r"\((VALID|INVALID)[^)]*\)")
_DAYS_RE = re.compile(r"(\d+)\s*day")
_HOUR_RE = re.compile(r"(\d+)\s*hour")


def parse_certbot(output: str) -> list[dict]:
    """把 certbot certificates 的文本切成 [{name, days, invalid}]。

    `INVALID: TEST_CERT` 只是说明这张是 --staging 测试证书，不是到期问题，
    按天推一条"证书已过期或无效"纯属噪音，因此只认 EXPIRED / REVOKED。
    """
    marks = list(_CERT_NAME_RE.finditer(output))
    certs: list[dict] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(output)
        block = output[m.start():end]
        status = _STATUS_RE.search(block)
        invalid = status is not None and status.group(1) == "INVALID" and bool(
            re.search(r"EXPIRED|REVOKED", block))
        days = None
        if not invalid:
            d = _DAYS_RE.search(block)
            h = _HOUR_RE.search(block)
            if d:
                days = int(d.group(1))
            elif h:
                days = 0  # 不足一天，按最紧急处理
        certs.append({"name": m.group(1), "days": days, "invalid": invalid})
    return certs


async def run_checks():
    pct = disk_usage_pct(config.DATA_DIR)
    disk_pct = _int_setting("alert_disk_pct", 85, 50, 99)
    if pct is None:
        warn("无法读取磁盘使用率，跳过磁盘告警")
    else:
        await _check(f"disk-{pct // 5}", pct >= disk_pct,
                     f"磁盘使用率 {pct}%（阈值 {disk_pct}%）于 {os.uname().nodename}，路径 {config.DATA_DIR}")

    failed = await run("systemctl", ["list-units", "--state=failed", "--no-legend", "--plain"])
    if failed["code"] == 0:
        units = _failed_units(failed["out"])
        await _check("failed-units", bool(units), f"存在 failed 状态服务: {', '.join(units)}")
    else:
        warn("systemctl 不可用，跳过 failed 单元巡检:", failed["out"][:120])

    ssl_days = _int_setting("alert_ssl_days", 14, 0, 60)  # 0 = 关闭证书巡检
    if ssl_days > 0:
        certs = await run("certbot", ["certificates"])
        if certs["code"] != 0:
            warn("certbot 不可用，跳过证书巡检:", certs["out"][:120])
        else:
            for c in parse_certbot(certs["out"]):
                low = c["days"] is not None and c["days"] <= ssl_days
                if c["invalid"]:
                    msg = f"证书 {c['name']} 已过期或无效！"
                elif low:
                    msg = f"证书 {c['name']} 将在 {c['days']} 天后到期（阈值 {ssl_days} 天）"
                else:
                    continue
                await _check(f"ssl-{c['name']}", True, msg)


async def notify_test() -> bool:
    return await notify(f"测试通知：来自 {os.uname().nodename} 的 choyeon panel，通道工作正常 ✅")


async def _checker_loop():
    await asyncio.sleep(60)
    while True:
        try:
            await run_checks()
        except Exception as e:  # noqa: BLE001
            warn("alert check failed:", e)
        await asyncio.sleep(30 * 60)


_checker_task = None


def start_checker():
    global _checker_task
    if _checker_task is None or _checker_task.done():
        _checker_task = asyncio.create_task(_checker_loop())
    return _checker_task


def stop_checker():
    """停止巡检协程（应用关闭时调用），避免遗留 pending task。"""
    global _checker_task
    if _checker_task is not None and not _checker_task.done():
        _checker_task.cancel()
    _checker_task = None
