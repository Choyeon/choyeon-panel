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
   历史的去重对象是**该 code 最近一行**（不限 level）：crit → ok → 同一条 crit
   必须重新落一行，否则"好了又坏"在列表里长得和"还在正常"一模一样。
5. 发过告警的对象必须发一条「已恢复」（`notified:<code>` 标记）：只有告警没有
   下文的通道，会逼着人反复登录面板确认，很快就被当成噪音忽略。
   阈值巡检和存活探测共用 `_recover()`，两条路径不能各写一份恢复逻辑。
6. 告警历史的裁剪只有 `_checker_loop` 一处：它按用户设置的 `alert_keep_days` /
   `alert_keep_rows` 裁。housekeeping 里再裁一次用的是函数默认值，
   设置就会变成死配置（⑤K 实测：keep 改成 365 天，30 天前的记录照样每 10 分钟被删）。
"""

import asyncio
import datetime
import json
import os
import re
import urllib.request
from contextlib import suppress

from .. import aio_db, config
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

# 要紧档只有一条配置线之上的这一档：管理员配的是"该发通知了"，页面上的红/自检的 fail
# 要再高一档才有意义，否则把线设到 60% 的机器会天天红。差的这 10 个点写在这里一次，
# 前端与自检都读 `disk_thresholds()` / `resource_thresholds()`，不许再各写一份。
CRIT_STEP = 10

# 数值型告警配置的「默认值 + 可填区间」只写这一处，(默认, 下限, 上限)。
# 同一件事原来写着四遍：巡检侧 `_int_setting(key, 默认, lo, hi)` 的三个实参、
# `extras.ALERT_INT_RANGE` 的 (lo, hi)、设置页 `ALERT_DEFAULTS` 的默认值、模板里
# `NInputNumber` 的 `:min/:max` 字面量。四份之间没有任何机制保证一起改，于是
# 库里没写过这个键时页面显示 80 而巡检用的是别的数；管理员照页面提示填的数，
# 保存时被 A 那份区间拦下、或者通过 B 那份校验却被读取侧悄悄钳掉。
# 现在默认值和区间都由这里给出：`setting_int()` 用它读，`alert_limits()` 用它显示。
ALERT_LIMITS: dict[str, tuple[int, int, int]] = {
    "alert_disk_pct": (85, 50, 99),
    "alert_ssl_days": (14, 0, 60),
    "alert_cpu_pct": (80, 1, 99),
    "alert_mem_pct": (90, 1, 99),
    "alert_load1_x": (2, 1, 32),
    "alert_probe": (1, 0, 1),
    "alert_keep_days": (30, 1, 365),
    "alert_keep_rows": (2000, 100, 20000),
}


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


def setting_int(key: str) -> int:
    """按 `ALERT_LIMITS` 读一条数值配置：默认值与钳制区间都取自那份登记表。

    调用方不再自带 `(默认, lo, hi)` 三个实参——那三个数就是配置的另一份出处，
    改了登记表不改实参（或反之）就会得到"页面显示一个数、巡检按另一个数办事"。
    登记表里没有的键（例如 `streak:` 这类内部计数器）请继续直接用 `_int_setting`。
    """
    default, lo, hi = ALERT_LIMITS[key]
    return _int_setting(key, default, lo, hi)


def alert_limits() -> dict:
    """给设置页的默认值与可填区间：就是 `setting_int()` 读取时使用的那一份。

    页面上「空值显示成默认值」必须和后端「空值按默认值办」是同一个默认，
    否则管理员看到 80、告警按 85 响，还以为是告警错了。区间同理：能保存的
    区间和会被悄悄钳掉的区间不能是两个。
    """
    return {k: {"default": d, "min": lo, "max": hi}
            for k, (d, lo, hi) in ALERT_LIMITS.items()}


def disk_thresholds() -> dict:
    """「这块盘要满了」只有一条线，出处是管理员在设置页配的 `alert_disk_pct`。

    同一个百分比过去写着四处：自检项的 75/90 字面量、巡检里的 `alert_disk_pct`（默认 85、
    可配 50-99）、存储对账的 `DISK_TIGHT_PCT = 90`，前端还各自抄了一份 80/90 与 90。
    后果是一台把线调到 70 的机器上，告警在响、概览的磁盘仪表仍是品牌色、存储页签写着
    「没有异常占用」——最该被看见的那块盘给出了三个互相矛盾的答案。
    现在 warn 就是那条生效配置，crit 比它高 10 档（封顶 99）用于「要紧/红」这一格，
    两处都不再另设常量；要改线只在设置页改一次。
    """
    line = setting_int("alert_disk_pct")
    return {"warn": line, "crit": min(99, line + CRIT_STEP)}


def resource_thresholds() -> dict:
    """CPU 与内存的两档线，口径与 `disk_thresholds()` 一致。

    概览的圆环原先自己写着 70/85 与 80/90 当配色档位，和设置页那两条配置（默认 80、90）
    没有半点关系：把 CPU 线调到 60 的机器，告警早就在响，仪表却还是品牌蓝——
    同一台机器上「CPU 65%」既是告警又是"一切正常"。现在 warn 就是配置线、
    crit 是它上面 10 个点，仪表的变色和巡检的判定读同一对数。
    负载不在这张表里：负载环画的是「相对核数」（load1 = 核数即 100%），
    那一格的黄/红说的是物理饱和（排队），而 `alert_load1_x` 是饱和之上的倍数，
    两者口径不同，硬并成一个出处反而会把仪表钉在永远不会变红的 200%。
    """
    cpu = setting_int("alert_cpu_pct")
    mem = setting_int("alert_mem_pct")
    return {"cpu_warn": cpu, "cpu_crit": min(99, cpu + CRIT_STEP),
            "mem_warn": mem, "mem_crit": min(99, mem + CRIT_STEP)}


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
    """写一条告警历史；与**该 code 最近一行**同级同文才跳过（比较不限 level）。

    去重键取 code 的最近一行而不是 code+level，是因为故障会好了又坏：
    09:00 磁盘超标（crit 行）→ 09:30 清理（ok 行）→ 15:00 备份又把盘写满，
    按 code+level 比的是"上一条 crit"，那条文字没变，于是这一轮**一行历史都不落**，
    告警列表最新一行停在「磁盘使用率正常」而盘正在满——网页端就此失明（约定 4）。
    反过来 crit → crit（依旧告警）仍然会被跳过：最近一行就是同级同文。
    恢复要留下一行 ok，这一条靠 level 不同来保证。
    """
    code = str(code or "")[:64]
    if level not in LEVELS or not code:
        return None
    try:
        last = await aio_db.query_one(
            "SELECT level, message FROM alerts_log WHERE code=? ORDER BY id DESC LIMIT 1",
            (code,),
        )
        if last is not None and last["level"] == level and last["message"] == message:
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


async def _recover(code: str, ok_message: str, meta: str = "") -> None:
    """恢复的一侧：落一行 ok 历史，并对"发过告警"的对象补一条「已恢复」。

    `record` 返回 None 表示与最近一行完全相同（已经恢复过），不再重复发。
    `notified:<code>` 记的是"这个对象发过告警通知"，据此决定要不要发恢复并清掉标记：
    只有告警没有下文的通道，会逼着人反复登录面板确认，很快就被当成噪音忽略（约定 5）。
    巡检与存活探测都必须走这里——探测的正常分支过去直接 `record("ok", …)`，
    于是 `notified:probe:<应用>` 永远留着、恢复通知永远不发，同一个标记还会
    在 settings 里越积越多（没有清理入口）。
    """
    if await record("ok", code, ok_message, meta) is None:
        return
    if dbm.get_setting("notified:" + code):
        dbm.set_setting("notified:" + code, "")
        await notify(f"已恢复：{ok_message}")


async def _check(key: str, trigger: bool, message: str, *, code: str | None = None,
                 level: str = "warn", ok_message: str | None = None, meta: str = "",
                 log_message: str | None = None, debounce: int = 1) -> None:
    """阈值判定的唯一入口：历史写 `log_message`，推送发 `message`。

    两者要分开是因为通知里必须带实时数值（"CPU 93%"才看得出严重程度），
    而历史里的 message 是去重键：带上每轮都在变的数字，去重就永不生效。
    数值同时进 meta，展开最新一条就能看到。

    `debounce` > 1 时，前若干轮只落历史不推送，避免瞬时尖峰变成通知噪音。
    """
    log_code = code or key
    if not trigger:
        if debounce > 1:
            _streak(log_code, False)
        if ok_message is not None:
            await _recover(log_code, ok_message, meta)
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
    dth = disk_thresholds()
    disk_pct = dth["warn"]
    if pct is None:
        warn("无法读取磁盘使用率，跳过磁盘告警")
    else:
        # 越过要紧线就升成 crit：通知渠道（Telegram / webhook）按 level 区分对待，
        # 而"还能写"和"快写不下了"是两件事，都发 warn 就只能靠人读数字。
        await _check(f"disk-{pct // 5}", pct >= disk_pct,
                     f"磁盘使用率 {pct}%（警戒线 {disk_pct}%，要紧线 {dth['crit']}%）"
                     f"于 {os.uname().nodename}，路径 {config.DATA_DIR}",
                     code="disk", level="crit" if pct >= dth["crit"] else "warn",
                     log_message=f"磁盘使用率超过阈值（阈值 {disk_pct}%）",
                     ok_message=f"磁盘使用率正常（阈值 {disk_pct}%）",
                     meta=_meta(pct=pct, threshold=disk_pct, crit=dth["crit"]))

    failed = await run("systemctl", ["list-units", "--state=failed", "--no-legend", "--plain"])
    if failed["code"] == 0:
        units = _failed_units(failed["out"])
        await _check("failed-units", bool(units), f"存在 failed 状态服务: {', '.join(units)}",
                     code="failed-units", level="crit", ok_message="无 failed 状态服务",
                     meta=_meta(units=units[:20]))
    else:
        warn("systemctl 不可用，跳过 failed 单元巡检:", failed["out"][:120])

    ssl_days = setting_int("alert_ssl_days")  # 0 = 关闭证书巡检
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
    if setting_int("alert_probe") == 1:
        await run_probes()


async def _check_resources() -> None:
    """CPU / 内存 / load1 阈值。指标取不到就整项跳过，不报"一切正常"。

    snapshot() 的 CPU 是"距上一次调用的差值"，窗口取决于谁在 poll 指标：
    仪表盘开着时这里读到的是几秒的瞬时值，没人看时是整段均值。同一个指标两种
    口径，所以这三项都走 RESOURCE_NOTIFY_ROUNDS 去抖，别把瞬时尖峰当故障。
    """
    rt = resource_thresholds()
    cpu_pct, mem_pct = rt["cpu_warn"], rt["mem_warn"]
    load_x = setting_int("alert_load1_x")
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

    await _check("cpu", cpu >= cpu_pct,
                 f"CPU 使用率 {cpu}%（阈值 {cpu_pct}%，要紧档 {rt['cpu_crit']}%）",
                 code="cpu", level="warn",
                 log_message=f"CPU 使用率超过阈值（阈值 {cpu_pct}%）",
                 ok_message=f"CPU 使用率正常（阈值 {cpu_pct}%）",
                 meta=_meta(value=cpu, threshold=cpu_pct, crit=rt["cpu_crit"]),
                 debounce=RESOURCE_NOTIFY_ROUNDS)
    await _check("mem", mem >= mem_pct,
                 f"内存使用率 {mem}%（阈值 {mem_pct}%，要紧档 {rt['mem_crit']}%）",
                 code="mem", level="warn",
                 log_message=f"内存使用率超过阈值（阈值 {mem_pct}%）",
                 ok_message=f"内存使用率正常（阈值 {mem_pct}%）",
                 meta=_meta(value=mem, threshold=mem_pct, crit=rt["mem_crit"]),
                 debounce=RESOURCE_NOTIFY_ROUNDS)
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


def probe_marker_keys(name: str) -> tuple[str, ...]:
    """一个应用的存活探测在 settings 里占用的全部键。

    四处键名都只由**应用名**决定（`run_probes` 用 `probe-<名>` 做 sent 的 key、
    用 `probe:<名>` 做 code；`_app_expectation` 读 `probe_expect:<名>`），
    名字还要跟着 `run_probes` 截断到 48 字符——不截断的话长名字应用的这两个键
    算出来就不是同一份，清理会漏掉正要清的那个。
    """
    n = str(name or "")[:48]
    return (f"probe_expect:{n}", f"sent:probe-{n}", f"streak:probe:{n}", f"notified:probe:{n}")


def forget_app(name: str) -> None:
    """应用被删掉时，把它按名字编的告警标记一并清掉。

    不清的两个后果（⑤L 实测）：
    - 幽灵行：`probe_expect:<名>` 残留在 Settings→探活覆写 的列表里，指向一个不存在的应用；
    - 同名继承：重建同名应用直接拿到旧的 `sent:`（当天已发过 → 第一次无响应静默）、
      旧的 `streak:`（去抖轮数从中间开始）与 `notified:`（永远不会有人消费它）。

    告警历史（alerts_log 的 `probe:<名>` 行）**不**跟着删：历史是另一条线，
    由 `_checker_loop` 按 `alert_keep_days`/`alert_keep_rows` 统一裁剪（约定 4、6）。
    """
    for key in probe_marker_keys(name):
        dbm.delete_setting(key)


def rename_app(old_name: str, new_name: str) -> None:
    """应用改名时，把按名字编的告警标记搬到新名字下。

    这四个键只由应用名决定（见 `probe_marker_keys`），而改名走的是普通的
    `UPDATE apps SET name=?`，没有任何代码会跟着动它们，实测后果是三种"面板看着
    正常、告警已经坏了"：
    - `probe_expect:<旧名>`：管理员单独给这个应用设过的预期状态码就此失效，探活悄悄
      回落到全局 `alert_probe_expect`；Settings 里那条覆写还在、看着是生效的。
    - `notified:probe:<旧名>`：改名前正处于"已发过告警"的状态时，这个标记此后再无人
      消费（新 code 是 `probe:<新名>`），「已恢复」永远不发——约定 5 断在这里。
    - `sent:probe-<旧名>`：留在旧名上是死行，新名字从"今天没发过"重新开始，
      改名当天同一次故障可能收到两条通知。

    搬的是值，不是历史：`alerts_log` 里 `probe:<旧名>` 的行不跟着改（约定 4，
    历史属于改名那一刻已经发生的事）。空值（`get_setting` 拿到 None/""）不覆盖
    新名字上已有的值，只把旧行删掉——新键有值说明它自己已经在用了。
    """
    old_keys = probe_marker_keys(old_name)
    new_keys = probe_marker_keys(new_name)
    if old_keys == new_keys:
        return
    for old_key, new_key in zip(old_keys, new_keys, strict=False):
        val = dbm.get_setting(old_key)
        if val:
            dbm.set_setting(new_key, val)
        dbm.delete_setting(old_key)


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
            # 恢复走 _recover 而不是裸 record：上一轮"无响应"发过通知就得补一条「已恢复」，
            # 并清掉 notified:probe:<应用>，否则这条标记再也没有人消费。
            await _recover(code, f"应用 {name} 存活（端口 {port}）",
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
            days = setting_int("alert_keep_days")
            keep = setting_int("alert_keep_rows")
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
