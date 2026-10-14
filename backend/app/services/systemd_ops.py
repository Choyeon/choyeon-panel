import os
import re
import shutil
import time
from contextlib import suppress

from ..util import run

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
    r = await run("systemctl", ["list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager"])
    out = []
    for line in r["out"].split("\n"):
        if not line.strip():
            continue
        f = re.split(r"\s+", line.strip())
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
    """
    if not units:
        return {}
    r = await run(
        "systemctl",
        ["show", "-p", "Id", "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID", "--", *units],
        timeout=10,
    )
    out = {p["Id"]: derive_state(p) for p in parse_show(r["out"])}
    for u in units:
        # systemctl 没给这一条（查失败、超时、本机没有 systemd）就是"未知"，不是"已停止"
        out.setdefault(u, dict(UNKNOWN_STATE))
    return out


async def service_action(unit: str, verb: str) -> str:
    if verb not in VERBS:
        raise RuntimeError(f"verb not allowed: {verb}")
    r = await run("systemctl", [verb, "--", unit], timeout=60)
    if r["code"] != 0:
        raise RuntimeError(r["out"] or f"systemctl {verb} failed")
    return r["out"]
