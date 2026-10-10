import re
import time

from ..util import run

VERBS = {"start", "stop", "restart", "reload", "enable", "disable"}

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
    r = await run("systemctl", ["is-active", unit])
    return r["code"] == 0


async def unit_states(units: list) -> dict:
    """一次 systemctl show 拿回多个 unit 的 active/MainPID。

    应用列表原本每个 app 起两次 systemctl（is-active + show MainPID），
    N 个应用就是 2N 个进程；批量后固定一次，返回
    {unit: {"active": bool, "mainPid": int}}，缺失的 unit 按未运行补默认值。

    必须先把每个空行分隔的块整体解析成属性表，再按块里的 Id 归属：
    `systemctl show` 输出的属性顺序是 systemd 自己定的（实测 MainPID 排在 Id 前面），
    "见到 Id 才开始建块"的写法会把排在 Id 之前的属性整个丢掉（MainPID 恒为 0），
    多 unit 时还会串到上一个块里去。
    """
    if not units:
        return {}
    r = await run(
        "systemctl",
        ["show", "-p", "Id", "-p", "ActiveState", "-p", "MainPID", *units],
        timeout=10,
    )
    out: dict[str, dict] = {}
    for block in r["out"].split("\n\n"):
        props: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, val = line.partition("=")
            if sep:
                props[key.strip()] = val.strip()
        unit = props.get("Id")
        if not unit:
            continue
        try:
            pid = int(props.get("MainPID") or 0)
        except ValueError:
            pid = 0
        out[unit] = {"active": props.get("ActiveState") == "active", "mainPid": pid}
    for u in units:
        out.setdefault(u, {"active": False, "mainPid": 0})
    return out


async def service_action(unit: str, verb: str) -> str:
    if verb not in VERBS:
        raise RuntimeError(f"verb not allowed: {verb}")
    r = await run("systemctl", [verb, unit], timeout=60)
    if r["code"] != 0:
        raise RuntimeError(r["out"] or f"systemctl {verb} failed")
    return r["out"]
