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
4. 历史与通知是两条独立的线：历史每轮去重后落库（含恢复的 ok 行），通知按
   `sent:<key>` 做当日去重，资源阈值还要连续两轮超标才发。
   任何"为了少发通知而少记历史"的改动都会让网页端的告警列表失明。
5. 发过告警的对象必须发一条「已恢复」（`notified:<code>` 标记）：只有告警没有
   下文的通道，会逼着人反复登录面板确认，很快就被当成噪音忽略。
"""

import asyncio
import datetime
import json
import os
import re
import urllib.request
from contextlib import suppress

from .. import aio_db
from .. import config
from .. import database as dbm
from ..log import warn
from ..util import disk_usage_pct, is_http_url, is_unit, run
from . import system_metrics

# 存活探测只打本机：面板只监听 127.0.0.1，应用端口也绑回环，
# 探测目标写死回环地址可以避免"设置里填个外网 URL 就把面板变成端口扫描器"。
PROBE_HOST = "127.0.0.1"
PROBE_TIMEOUT = 3.0
PROBE_DEFAULT_EXPECT = "2xx/3xx"
LEVELS = ("ok", "warn", "crit")

# 资源阈值要连续超标这么多轮才推送。snapshot() 的 CPU 是"距上一次采样的差值"，
# 仪表盘在前台每 3 秒 poll 一次，所以巡检拿到的其实是几秒钟的瞬时值：备份压缩、
# 日志轮转、nginx reload 都能把它顶到 100%，为这种尖峰发一条 Telegram 只会让人
# 开始忽略告警。历史不受此限制——状态一变就记，网页端当轮就能看到，只是推送延后
# 一轮确认。连不上/单元 failed/证书过期是明确故障，不在这个缓冲之列。
RESOURCE_NOTIFY_ROUNDS = 2


def _today() -> str:
    return datetime.date.today().isoformat()


def _meta(**kv) -> str:
    """把取值压成一行 JSON 存进 meta。

    巡检每 30 分钟一轮，实时数值（CPU%、状态码）几乎每轮都在变；
    把它们写进 message 会让"同 code+level 且 message 未变则跳过"的去重失效，
    历史表于是每轮都长一行。数值放 meta，message 只写结论 + 阈值。
    """
    try:
        return json.dumps(kv, ensure_ascii=False)[:400]
    except (TypeError, ValueError):
        return ""


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


async def record(level: str, code: str, message: str, meta: str = "") -> int | None:
    """写一条告警历史；与同 code+level 的最近一行 message 相同则跳过。

    去重键取 code+level 而不是 code：crit → ok（恢复）与 crit → crit（依旧告警）
    必须能区分开，恢复要留下一行。返回新行 id，跳过或写库失败返回 None。
    """
    code = str(code or "")[:64]
    if level not in LEVELS or not code:
        return None
    try:
        last = await aio_db.query_one(
            "SELECT message FROM alerts_log WHERE code=? AND level=? ORDER BY id DESC LIMIT 1",
            (code, level),
        )
        if last is not None and last["message"] == message:
            return None
        return await aio_db.execute(
            "INSERT INTO alerts_log(ts,level,code,message,meta) VALUES(datetime('now'),?,?,?,?)",
            (level, code, str(message)[:500], str(meta)[:500]),
        )
    except Exception as e:  # noqa: BLE001
        # 历史写不进去不能连通知一起吞掉；报错只进面板日志，不外抛给巡检循环
        warn("告警历史写入失败:", type(e).__name__, e)
        return None


def _streak(code: str, trigger: bool) -> int:
    """同一告警连续触发的轮数，计数存 settings；一旦恢复就清零。

    只服务于去抖（见 RESOURCE_NOTIFY_ROUNDS）：历史每轮都照记，延后的只是推送。
    """
    key = "streak:" + code
    if not trigger:
        if dbm.get_setting(key):
            dbm.set_setting(key, "")
        return 0
    n = _int_setting(key, 0, 0, 999) + 1
    dbm.set_setting(key, str(n))
    return n


async def _check(key: str, trigger: bool, message: str, *, code: str | None = None,
                 level: str = "warn", ok_message: str | None = None, meta: str = "",
                 log_message: str | None = None, debounce: int = 1) -> None:
    """阈值判定的唯一入口：历史写 `log_message`，推送发 `message`。

    两者要分开是因为通知里必须带实时数值（"CPU 93%"才看得出严重程度），
    而历史里的 message 是去重键：带上每轮都在变的数字，去重就永不生效。
    数值同时进 meta，展开最新一条就能看到。

    `debounce` > 1 时，前若干轮只落历史不推送，避免瞬时尖峰变成通知噪音。

    恢复也要推送：`notified:<code>` 记的是"这个对象发过告警通知"，恢复时据此
    发一条「已恢复」并清掉标记。没有这条的话，用户收到"磁盘 91%"之后永远等不到
    下文，只能自己登录面板确认——而收到告警的人恰恰是不会天天开面板的人。
    """
    log_code = code or key
    if not trigger:
        if debounce > 1:
            _streak(log_code, False)
        if ok_message is None:
            return
        # record 返回 None = 与上一条 ok 完全相同（已恢复过），不重复发恢复通知
        if await record("ok", log_code, ok_message, meta) is None:
            return
        if dbm.get_setting("notified:" + log_code):
            dbm.set_setting("notified:" + log_code, "")
            await notify(f"已恢复：{ok_message}")
        return
    await record(level, log_code, message if log_message is None else log_message, meta)
    if debounce > 1 and _streak(log_code, True) < debounce:
        return
    if dbm.get_setting("sent:" + key) == _today():
        return
    # 只有真投递成功才标记"今天已发"，否则本次告警当天不会再重试
    if await notify(message):
        dbm.set_setting("sent:" + key, _today())
        dbm.set_setting("notified:" + log_code, _today())


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
                     f"磁盘使用率 {pct}%（阈值 {disk_pct}%）于 {os.uname().nodename}，路径 {config.DATA_DIR}",
                     code="disk", level="warn",
                     log_message=f"磁盘使用率超过阈值（阈值 {disk_pct}%）",
                     ok_message=f"磁盘使用率正常（阈值 {disk_pct}%）",
                     meta=_meta(pct=pct, threshold=disk_pct))

    failed = await run("systemctl", ["list-units", "--state=failed", "--no-legend", "--plain"])
    if failed["code"] == 0:
        units = _failed_units(failed["out"])
        await _check("failed-units", bool(units), f"存在 failed 状态服务: {', '.join(units)}",
                     code="failed-units", level="crit", ok_message="无 failed 状态服务",
                     meta=_meta(units=units[:20]))
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
                    await _check(f"ssl-{c['name']}", True, f"证书 {c['name']} 已过期或无效！",
                                 code=f"ssl:{c['name']}", level="crit",
                                 meta=_meta(cert=c["name"], days=c["days"]))
                elif low:
                    await _check(f"ssl-{c['name']}", True,
                                 f"证书 {c['name']} 将在 {c['days']} 天后到期（阈值 {ssl_days} 天）",
                                 code=f"ssl:{c['name']}", level="warn",
                                 meta=_meta(cert=c["name"], days=c["days"], threshold=ssl_days))
                else:
                    await _check(f"ssl-{c['name']}", False, "",
                                 code=f"ssl:{c['name']}",
                                 ok_message=f"证书 {c['name']} 有效期正常（阈值 {ssl_days} 天）",
                                 meta=_meta(cert=c["name"], days=c["days"]))

    await _check_resources()
    if _int_setting("alert_probe", 1, 0, 1) == 1:
        await run_probes()


async def _check_resources() -> None:
    """CPU / 内存 / load1 阈值。指标取不到就整项跳过，不报"一切正常"。

    snapshot() 的 CPU 是"距上一次调用的差值"，窗口取决于谁在 poll 指标：
    仪表盘开着时这里读到的是几秒的瞬时值，没人看时是整段均值。同一个指标两种
    口径，所以这三项都走 RESOURCE_NOTIFY_ROUNDS 去抖，别把瞬时尖峰当故障。
    """
    cpu_pct = _int_setting("alert_cpu_pct", 80, 1, 99)
    mem_pct = _int_setting("alert_mem_pct", 90, 1, 99)
    load_x = _int_setting("alert_load1_x", 2, 1, 32)
    try:
        snap = await system_metrics.snapshot()
    except Exception as e:  # noqa: BLE001
        warn("读取系统指标失败，跳过资源阈值巡检:", type(e).__name__, e)
        return

    hist = snap.get("history")
    if isinstance(hist, list) and len(hist) < 2:
        # 进程第一次采样没有差值基线，cpu 恒为 0.0：那是"没测到"不是"很空闲"，
        # 据此记一行"CPU 正常"等于谎报平安，等下一轮有基线了再说。
        return

    cpu = float(snap.get("cpu") or 0.0)
    mem_total = float(snap.get("memTotal") or 0)
    mem_used = float(snap.get("memUsed") or 0)
    mem = round(mem_used * 100 / mem_total) if mem_total > 0 else 0.0
    load1 = float((snap.get("load") or [0.0])[0])
    cores = int(snap.get("cpuCores") or os.cpu_count() or 1)
    load_limit = round(cores * load_x, 2)

    await _check("cpu", cpu >= cpu_pct, f"CPU 使用率 {cpu}%（阈值 {cpu_pct}%）",
                 code="cpu", level="warn",
                 log_message=f"CPU 使用率超过阈值（阈值 {cpu_pct}%）",
                 ok_message=f"CPU 使用率正常（阈值 {cpu_pct}%）",
                 meta=_meta(value=cpu, threshold=cpu_pct), debounce=RESOURCE_NOTIFY_ROUNDS)
    await _check("mem", mem >= mem_pct, f"内存使用率 {mem}%（阈值 {mem_pct}%）",
                 code="mem", level="warn",
                 log_message=f"内存使用率超过阈值（阈值 {mem_pct}%）",
                 ok_message=f"内存使用率正常（阈值 {mem_pct}%）",
                 meta=_meta(value=mem, threshold=mem_pct), debounce=RESOURCE_NOTIFY_ROUNDS)
    # load1 阈值按核数算：单核机器 load 1.5 已经很挤，8 核机器 load 8 才算满载
    await _check("load1", load1 >= load_limit,
                 f"1 分钟负载 {load1}（阈值 {load_limit}，{cores} 核 ×{load_x}）",
                 code="load1", level="warn",
                 log_message=f"1 分钟负载超过阈值（阈值 {load_limit}，{cores} 核 ×{load_x}）",
                 ok_message=f"1 分钟负载正常（阈值 {load_limit}，{cores} 核 ×{load_x}）",
                 meta=_meta(value=load1, threshold=load_limit, cores=cores),
                 debounce=RESOURCE_NOTIFY_ROUNDS)


def _parse_expect(raw: str) -> set[int]:
    """`2xx/3xx`、`200 204` 这类写法 → 允许的状态码集合；看不懂就回落 2xx/3xx。"""
    codes: set[int] = set()
    for m in re.finditer(r"([1-5])(?:xx|(\d{2}))", str(raw or "").lower()):
        base = int(m.group(1)) * 100
        if m.group(2):
            codes.add(base + int(m.group(2)))
        else:
            codes.update(range(base, base + 100))
    return codes or set(range(200, 400))


def _app_expectation(name: str) -> tuple[set[int], str]:
    """每应用可用 settings 键 `probe_expect:<应用名>` 覆写，全局键 `alert_probe_expect` 兜底。"""
    raw = (dbm.get_setting(f"probe_expect:{name}") or dbm.get_setting("alert_probe_expect")
           or PROBE_DEFAULT_EXPECT)
    return _parse_expect(raw), str(raw or PROBE_DEFAULT_EXPECT)[:32]


async def _probe_target(port: int) -> tuple[bool, int | None, str]:
    """对 127.0.0.1:port 发一次手写 GET /，返回 (是否有应答, 状态码或 None, 说明)。

    不引 httpx/requests：这里只要"有没有 HTTP 响应"这一层信息，
    asyncio.open_connection + 一行请求文本就够，少一个依赖就少一个供应链面。
    """
    request = (
        f"GET / HTTP/1.1\r\nHost: {PROBE_HOST}:{port}\r\n"
        "User-Agent: choyeon-panel-probe/1.0\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    )
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(PROBE_HOST, port), PROBE_TIMEOUT)
    except Exception as e:  # noqa: BLE001  # 拒连/超时/DNS 都算"无应答"，不带原文外泄
        return False, None, type(e).__name__
    try:
        writer.write(request.encode())
        await asyncio.wait_for(writer.drain(), PROBE_TIMEOUT)
        head = await asyncio.wait_for(reader.readline(), PROBE_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return True, None, f"{type(e).__name__}（连接建立但未取到状态行）"
    finally:
        with suppress(Exception):
            writer.close()
    parts = head.decode(errors="replace").split()
    if len(parts) >= 2 and parts[0].upper().startswith("HTTP/") and parts[1].isdigit():
        return True, int(parts[1]), ""
    return True, None, "响应首行不是 HTTP 状态行"


async def run_probes() -> list[dict]:
    """对所有配了端口的应用做存活探测，结果走同一套 alerts_log 去重路径。

    语义上"服务应答了"就不算挂：4xx/5xx 说明进程在、端口在、协议在，
    真正要报警的是连不上（进程没了 / 端口没起 / hang 到超时）。
    状态码不在预期内只记 warn 历史，不发通知——那是应用自己的业务状态，
    不是主机故障，发出去只会把通知通道刷成噪音。
    """
    try:
        apps = await aio_db.query(
            "SELECT id, name, port FROM apps WHERE port IS NOT NULL AND port > 0 ORDER BY id"
        )
    except Exception as e:  # noqa: BLE001
        warn("读取应用列表失败，跳过存活探测:", type(e).__name__, e)
        return []
    results: list[dict] = []
    for a in apps:
        name = str(a["name"])[:48]
        port = int(a["port"])
        code = f"probe:{name}"
        alive, status, detail = await _probe_target(port)
        if not alive:
            await _check(f"probe-{name}", True, f"应用 {name} 无响应（{PROBE_HOST}:{port}，{detail}）",
                         code=code, level="crit", meta=_meta(app=a["id"], port=port, error=detail))
            results.append({"name": name, "port": port, "alive": False, "status": None, "level": "crit"})
            continue
        expect, expect_text = _app_expectation(name)
        if status is None:
            await record("warn", code, f"应用 {name} 端口 {port} 有响应，但不是 HTTP",
                         meta=_meta(app=a["id"], port=port, detail=detail[:120]))
            results.append({"name": name, "port": port, "alive": True, "status": None, "level": "warn"})
        elif status in expect:
            await record("ok", code, f"应用 {name} 存活（端口 {port}）",
                         meta=_meta(app=a["id"], port=port, status=status, expect=expect_text))
            results.append({"name": name, "port": port, "alive": True, "status": status, "level": "ok"})
        else:
            await record("warn", code,
                         f"应用 {name} 应答 {status}，不在预期 {expect_text} 内（端口 {port}）",
                         meta=_meta(app=a["id"], port=port, status=status, expect=expect_text))
            results.append({"name": name, "port": port, "alive": True, "status": status, "level": "warn"})
    return results


async def notify_test() -> bool:
    return await notify(f"测试通知：来自 {os.uname().nodename} 的 choyeon panel，通道工作正常 ✅")


async def _checker_loop():
    await asyncio.sleep(60)
    while True:
        try:
            await run_checks()
        except Exception as e:  # noqa: BLE001
            warn("alert check failed:", type(e).__name__, e)
        try:
            # 历史表只有这里会瘦身；不裁剪的话去重也挡不住几个月的 crit 抖动
            days = _int_setting("alert_keep_days", 30, 1, 365)
            keep = _int_setting("alert_keep_rows", 2000, 100, 20000)
            await asyncio.to_thread(dbm.prune_alerts, days, keep)
        except Exception as e:  # noqa: BLE001
            warn("alert history prune failed:", type(e).__name__, e)
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
