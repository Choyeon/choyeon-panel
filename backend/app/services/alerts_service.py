import asyncio
import datetime
import json
import os
import re
import urllib.request

from .. import database as dbm
from ..log import warn
from ..util import run


def _today() -> str:
    return datetime.date.today().isoformat()


def _post(url: str, payload: dict):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


async def notify(text: str):
    bot = dbm.get_setting("alert_telegram_bot")
    chat = dbm.get_setting("alert_telegram_chat")
    hook = dbm.get_setting("alert_webhook_url")
    if bot and chat:
        try:
            await asyncio.to_thread(_post, f"https://api.telegram.org/bot{bot}/sendMessage",
                                    {"chat_id": chat, "text": f"[choyeon-panel] {text}"})
        except Exception as e:  # noqa: BLE001
            warn("telegram notify failed:", e)
    if hook:
        try:
            await asyncio.to_thread(_post, hook, {"text": text})
        except Exception as e:  # noqa: BLE001
            warn("webhook notify failed:", e)


async def _check(key: str, trigger: bool, message: str):
    if not trigger:
        return
    if dbm.get_setting("sent:" + key) == _today():
        return
    dbm.set_setting("sent:" + key, _today())
    await notify(message)


async def run_checks():
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    used_pct = round((total - st.f_bfree * st.f_frsize) / total * 100)
    disk_pct = int(dbm.get_setting("alert_disk_pct") or 85)
    await _check(f"disk-{used_pct // 5}", used_pct >= disk_pct,
                 f"磁盘使用率 {used_pct}%（阈值 {disk_pct}%）于 {os.uname().nodename}")

    failed = await run("systemctl", ["list-units", "--state=failed", "--no-legend", "--plain"])
    units = [ln.strip().split()[0] for ln in failed["out"].split("\n") if ln.strip()]
    await _check("failed-units", bool(units), f"存在 failed 状态服务: {', '.join(units)}")

    ssl_days = int(dbm.get_setting("alert_ssl_days") or 14)
    if ssl_days > 0:
        certs = await run("certbot", ["certificates"])
        separator = "-" * 79  # certbot 输出里用于分隔每张证书的分隔线
        blocks = [b for b in certs["out"].split(separator) if "Certificate Name" in b]
        for b in blocks:
            name = re.search(r"Certificate Name: (\S+)", b)
            days = re.search(r"Days Left: (\d+)", b)
            if not name:
                continue
            name = name.group(1)
            invalid = bool(re.search(r"EXPIRED|invalid", b, re.I))
            days_num = int(days.group(1)) if days else None
            low = days_num is not None and days_num <= ssl_days
            if invalid:
                msg = f"证书 {name} 已过期或无效！"
            elif days_num is not None:
                msg = f"证书 {name} 将在 {days_num} 天后到期（阈值 {ssl_days} 天）"
            else:
                continue
            await _check(f"ssl-{name}", invalid or low, msg)


async def notify_test():
    await notify(f"测试通知：来自 {os.uname().nodename} 的 choyeon panel，通道工作正常 ✅")


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
