import os
import re
import shutil
import time
from contextlib import suppress

from ..util import PROBE_TIMEOUT, run

VERBS = {"start", "stop", "restart", "reload", "enable", "disable"}

# ---------- 服务状态判词的唯一出处 ----------
# ActiveState 的取值不止 active/inactive：还有 activating、deactivating、reloading、failed，
# 而面板原来只问"是不是 active"，于是同一个事实在站内有两套答案。
# - Type=oneshot 正在执行时 ActiveState 是 **activating**（生产实测，跑完才回 inactive）：
#   按老口径"正在跑的那一轮"等于"没在运行"，定时任务的重叠执行闸因此形同虚设。
# - Type=simple 起立的那几秒同样是 activating：恢复备份时判成"没在跑"就跳过停服务，
#   文件是直接盖在一个正要 exec 的进程上的。
# - deactivating / reloading 时主进程都还活着。
# "健康运行中"（列表与详情要显示的）和"现在占着文件与端口"（动手前必须确认的）是两个问题，
# 所以这里给出两个判词，调用点不再各自比较字符串。
RUNNING_STATES = ("active", "reloading")
BUSY_STATES = RUNNING_STATES + ("activating", "deactivating")
# BUSY_STATES 减 RUNNING_STATES：正在起立或正在收，既不能说"运行中"，也不能按已停处理
# （此刻 restart 打断的是它自己）。调用点用这个集合，不要再抄一遍元组。
TRANSITION_STATES = ("activating", "deactivating")
# unit 被删过、机器迁移过、名字写错了：systemd 报 LoadState=not-found，而 ActiveState 只是
# inactive。只看后者会把"这台机器上从来没有这个服务"显示成"服务已停止，点启动即可"，
# 而点下去只会得到一句 systemctl 的 "not loaded"。
MISSING_LOAD_STATES = ("not-found",)
# 探测失败（本机没有 systemd、systemctl 报错、超时）时按未知处理：不能把"查不到"说成
# "已停止"（整屏假红），也不能说成"没在跑"（恢复因此跳过停服务，把文件盖在活着的服务上）。
UNKNOWN_STATE = {"state": "unknown", "load": "", "active": False, "busy": False,
                 "missing": False, "mainPid": 0}


def has_systemd() -> bool:
    """本机有没有可管理的 systemd（容器 / WSL 里没有 /run/systemd/system）。

    判定只留这一处：以前自检项自己写一份，恢复流程则靠"systemctl 查失败"反推，
    后者在容器里恰好蒙对，在 systemctl 卡死/超时的真机器上就是把"不知道"当成"没在跑"，
    于是直接往活着的服务写的文件上盖备份。
    """
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


def timezone_name() -> str:
    """本机时区名，给 unit 的 OnCalendar 用——避免"面板说本地时间、timer 按 UTC 跑"的错位。

    /etc/timezone 在 RHEL 系与容器里常不存在，读不到属于正常情况，退回 /etc/localtime 的
    zoneinfo 软链目标。两条都拿不到就回 UTC：unit 里写错时区不是"不起作用"，而是"每天在另一个
    钟点起作用"，这类错最难查，所以宁可落回一个明确说得出名字的默认值，也不要空串（空串的
    OnCalendar 会直接被 systemd 拒掉，或者按 UTC 跑，两种都比 UTC 更难看）。
    """
    try:
        with open("/etc/timezone", encoding="utf-8") as fh:
            tz = fh.read().strip()
        if tz:
            return tz
    except OSError:
        pass
    try:
        link = os.path.realpath("/etc/localtime")
    except OSError:
        return "UTC"
    return link.split("/zoneinfo/")[1] if "/zoneinfo/" in link else "UTC"


async def reset_failed(unit: str) -> None:
    """清掉 systemd 的启动失败计数，本身失败不抛（调用方接下来要 start，成不成由那边报）。

    进入 failed 的 unit 会被 StartLimitBurst 直接拒掉（"Start request repeated too quickly"），
    而用户分不清是自己命令写错还是面板坏了；对正常停止的服务这是空操作。
    """
    with suppress(Exception):
        await run("systemctl", ["reset-failed", "--", unit], timeout=30)


def parse_show(raw: str) -> list[dict]:
    """把 systemctl show 的输出收成属性表列表。

    必须先把每个空行分隔的块整体解析，再按块里的 Id 归属：属性顺序是 systemd 自己定的
    （实测 MainPID 排在 Id 前面），"见到 Id 才开始建块"会把排在 Id 之前的属性整个丢掉
    （MainPID 恒为 0），多 unit 时还会串到上一个块里去。
    """
    blocks = []
    for block in raw.split("\n\n"):
        props: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, val = line.partition("=")
            if sep:
                props[key.strip()] = val.strip()
        if props.get("Id"):
            blocks.append(props)
    return blocks


def derive_state(props: dict) -> dict:
    state = props.get("ActiveState") or ""
    load = props.get("LoadState") or ""
    try:
        pid = int(props.get("MainPID") or 0)
    except ValueError:
        pid = 0
    return {
        "state": state,
        "load": load,
        "active": state in RUNNING_STATES,
        "busy": state in BUSY_STATES,
        "missing": load in MISSING_LOAD_STATES,
        "mainPid": pid if pid > 0 else 0,
    }

# list-unit-files 结果的短 TTL 缓存：见 unit_files() 的说明。
_UF_TTL = 30.0
_uf_cache: tuple[float, dict] = (0.0, {})


async def list_services() -> list:
    r = await run(
        "systemctl",
        ["list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager"],
        timeout=PROBE_TIMEOUT,
    )
    if r["code"] != 0:
        # 失败正文不能当表格数据往下解析。正常输出每行是 UNIT/LOAD/ACTIVE/SUB/DESCRIPTION
        # 五列，`run()` 在超时或命令不存在时返回的却是一句短正文（"systemctl 超时（5s，…）"），
        # 直接喂给下面的 `f[3]` 就是 IndexError；把它吞成空列表更糟——"这台机器一个服务都没有"
        # 会被读成绿灯。口径与 unit_files() 一致：查不到就如实报错。
        raise RuntimeError(f"systemctl list-units 失败（退出码 {r['code']}）：{r['out'][:200]}")
    out = []
    for line in r["out"].split("\n"):
        if not line.strip():
            continue
        f = re.split(r"\s+", line.strip())
        # 列数不够的行不是服务行（systemd 偶尔在 --no-legend 之间夹一行提示），跳过而不是 IndexError。
        if len(f) < 4:
            continue
        out.append({"unit": f[0], "load": f[1], "active": f[2], "sub": f[3], "desc": " ".join(f[4:])})
    return out


async def unit_files(force: bool = False) -> dict:
    """unit → 开机自启状态（enabled/disabled/masked/static/alias/generated/…）。

    自启状态**不在** list-units 的输出里（那五个列是 UNIT/LOAD/ACTIVE/SUB/DESCRIPTION，
    `--all` 只是把未加载的 unit 也列出来），所以前端原来那个「自启」按钮没有任何状态来源：
    既不能显示当前是否已启用，也无法在改完之后回读。这里单独走 list-unit-files。

    整表一次调用（约 200 行）而不是逐行 `systemctl is-enabled`，配 30s TTL：
    服务管理页每 15s 刷一次，逐行查就是每个周期多起几百个 systemctl 进程。
    `force=True` 只给写完立刻回读的用（enable/disable 之后按钮要当场变状态）。
    """
    global _uf_cache
    now = time.monotonic()
    if not force and _uf_cache[1] and now - _uf_cache[0] < _UF_TTL:
        return _uf_cache[1]
    r = await run(
        "systemctl",
        ["list-unit-files", "--type=service", "--no-legend", "--plain", "--no-pager"],
        timeout=20,
    )
    out: dict[str, str] = {}
    if r["code"] == 0:
        for line in r["out"].split("\n"):
            if not line.strip():
                continue
            f = re.split(r"\s+", line.strip())
            if len(f) >= 2:
                out[f[0]] = f[1]
        _uf_cache = (now, out)
    elif _uf_cache[1]:
        # 拿不到就沿用上一份：把「查不到」显示成「全部未启用」比不更新更糟
        return _uf_cache[1]
    return out


async def is_active(unit: str) -> bool:
    """列表与详情用的"运行中"。见 RUNNING_STATES 的说明。"""
    return (await unit_state(unit))["active"]


async def is_busy(unit: str) -> bool:
    """"现在有没有进程活着或正在起停"——覆盖文件、恢复数据库之前必须按这个问。"""
    return (await unit_state(unit))["busy"]


async def unit_state(unit: str) -> dict:
    return (await unit_states([unit])).get(unit) or dict(UNKNOWN_STATE)


async def unit_states(units: list) -> dict:
    """一次 systemctl show 拿回多个 unit 的状态：{unit: {state, load, active, busy, missing, mainPid}}。

    应用列表原本每个 app 起两次 systemctl（is-active + show MainPID），N 个应用就是 2N 个进程；
    批量后固定一次。判词全部从 ActiveState/LoadState 现推，不再用 `systemctl is-active` 的退出码：
    退出码只会回答"0 / 非 0"，把"正在启动""这个 unit 根本不存在""systemctl 查失败了"
    压成同一个答案，而面板对这三种情况要说的话完全不同。

    `--` 结束选项解析是必须的：unit 名即使以 - 开头（旧版本校验放宽时写进库的记录）也只会被
    当作单元名。实测 `systemctl status -ping.service` 返回 0 并打印整机状态——少了这道标记，
    面板就把"选项解析成功"读成"这个服务在跑"。本机已验证 start/stop/restart/reload/
    enable/disable/is-active/show/reset-failed 都接受 `--`。

    返回值只按**请求时给的名字**建键，且每个名字对应自己一份状态表。这里必须按 systemd 回显的
    `Names`（含 alias）和 Id 反查，不能拿 Id 当键：本机上 `systemctl show -p Id -- sshd` 回的是
    `Id=ssh.service`（sshd 是 alias），`-- fail2ban` 回 `Id=fail2ban.service` 而请求写的是不带
    后缀的 `fail2ban`。以前按 Id 建键、再对请求名 setdefault 一份"未知"，这两个请求都拿到 UNKNOWN，
    于是登录安全自检把"正在跑的 sshd"读成"查不到"，把在跑爆破防护的判断直接判死。
    """
    if not units:
        return {}
    r = await run(
        "systemctl",
        ["show", "-p", "Id", "-p", "Names", "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID",
         "--", *units],
        timeout=PROBE_TIMEOUT,
    )
    by_name: dict[str, dict] = {}
    for props in parse_show(r["out"]):
        state = derive_state(props)
        for name in [props["Id"], *(props.get("Names") or "").split()]:
            if name:
                by_name.setdefault(name, state)
                # 无后缀的 unit（模板实例、socket、被省略的 .service）也挂上带后缀的形式
                if "." not in name:
                    by_name.setdefault(f"{name}.service", state)
    out: dict[str, dict] = {}
    for u in units:
        # systemctl 没给这一条（查失败、超时、本机没有 systemd）就是"未知"，不是"已停止"
        st = by_name.get(u)
        if st is None and "." not in u:
            st = by_name.get(f"{u}.service")
        out[u] = dict(st) if st is not None else dict(UNKNOWN_STATE)
    return out


async def service_action(unit: str, verb: str) -> str:
    if verb not in VERBS:
        raise RuntimeError(f"verb not allowed: {verb}")
    r = await run("systemctl", [verb, "--", unit], timeout=60)
    if r["code"] != 0:
        raise RuntimeError(r["out"] or f"systemctl {verb} failed")
    return r["out"]


# ---------- 「这个进程属于哪个 systemd 单元」的唯一出处 ----------
# 此前 memory / load / pending 三个服务各写了一份 /proc/<pid>/cgroup 解析，而它们的答案并不一致：
# 同一个 `0::/user.slice/user-0.slice/user@0.service/init.scope`，pending 的正则抓整行第一个
# `.service` 得到 user@0.service（于是「改动未生效」劝人去重启 systemd 的用户管理器），memory 与
# load 取叶子得到 init.scope。2026-10-17 在这台生产机上 145 个进程里有 13 个属于这类形态，
# 三个读者 13/13 全分歧。定稿时在这台机器上复测（54 个带单元形态的进程）：与旧 pending 规则的
# 分歧只剩会话进程这一类 2 条，也就是说改的就是那一条缺陷，没有顺手改变别的归属。
# 同一轮复测还钉住了第二个形态：`0::/system.slice/systemd-udevd.service/udev`——单元目录下再开的
# 子组，叶子不带后缀。只看叶子会让这台正在跑的 udevd 从归因表里消失，所以规则是「最深的、带已知
# 后缀的那一层」，而不是「叶子」。
PANEL_SERVICE = "choyeon-panel"
PANEL_UNIT = f"{PANEL_SERVICE}.service"
UNIT_SUFFIXES = (".service", ".scope", ".slice")


def unit_of_cgroup(raw: str) -> tuple[str | None, str]:
    """/proc/<pid>/cgroup → (单元名, 类型)。

    cgroup v2 只有一行 `0::/system.slice/panel-rosetta-backend.service`。**从最深的组件往前**
    找第一个带已知后缀的：.service 是单元；.scope 是一次性会话（sshd 的 session-*.scope 就在
    这里——它不属于任何面板应用，但内存是真占了，所以如实标成 scope 而不是丢掉）；.slice 是分组。

    为什么要往前找而不是只看叶子，是两个都真实存在的形态（2026-10-17 生产机原样抓回）：
    - `0::/user.slice/user-0.slice/user@0.service/init.scope`：叶子是 scope。只看第一个后缀会
      答成 user@0.service——那是 root 的 systemd 用户管理器，页面会劝人 restart 它，而 restart
      它等于踢掉这台机器上所有 ssh 会话。这就是本批要修的缺陷。
    - `0::/system.slice/systemd-udevd.service/udev`：单元目录下再开的子组，叶子没有后缀。只认
      叶子会答成「归不到单元」，把一台好端端在跑的 udevd 从归因表里抹掉。
    两个要求合起来只剩一个答案：最深的、带后缀的那一层。

    解析不出（整条路径都没有后缀的容器路径、v1 的多行形态、根 cgroup）给 (None, ...)，
    让调用方按「归不到单元」处理。
    """
    line = ""
    for one in (raw or "").splitlines():
        if one.startswith("0::"):
            line = one
            break
    if not line:
        return None, "none"
    segs = [seg for seg in line.partition("::")[2].split("/") if seg not in ("", ".")]
    for seg in reversed(segs):
        for suf in UNIT_SUFFIXES:
            if seg.endswith(suf):
                return seg, suf[1:]
    return (None, "root") if not segs else (None, "other")


def panel_unit() -> str:
    """面板自己被算进哪个 systemd 单元——站内所有"面板自己"的判词只问这里。

    本进程的 .service 叶子是权威：以 systemd 单元跑起来时它就是那个单元，管理员改名了判词也跟着改。
    只认 .service 是因为"面板自己"必须是一个服务——sshd 会话的 session-*.scope、容器里的根 cgroup
    都不是面板，而 CLI 与手工 uvicorn 恰恰就跑在那里面：那里没有"我属于哪个单元"这个事实，而这两处
    要查的从来是"面板服务"而不是"我这条命令"，所以退回本仓库安装的单元名。
    """
    with suppress(OSError):
        with open("/proc/self/cgroup", encoding="utf-8", errors="ignore") as fh:
            name, kind = unit_of_cgroup(fh.read())
        if name and kind == "service":
            return name
    return PANEL_UNIT
