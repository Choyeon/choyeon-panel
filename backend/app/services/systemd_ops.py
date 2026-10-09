import re

from ..util import run

VERBS = {"start", "stop", "restart", "reload", "enable", "disable"}


async def list_services() -> list:
    r = await run("systemctl", ["list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager"])
    out = []
    for line in r["out"].split("\n"):
        if not line.strip():
            continue
        f = re.split(r"\s+", line.strip())
        out.append({"unit": f[0], "load": f[1], "active": f[2], "sub": f[3], "desc": " ".join(f[4:])})
    return out


async def is_active(unit: str) -> bool:
    r = await run("systemctl", ["is-active", unit])
    return r["code"] == 0


async def unit_states(units: list) -> dict:
    """一次 systemctl show 拿回多个 unit 的 active/MainPID。

    应用列表原本每个 app 起两次 systemctl（is-active + show MainPID），
    N 个应用就是 2N 个进程；批量后固定一次，返回
    {unit: {"active": bool, "mainPid": int}}，缺失的 unit 按未运行补默认值。
    """
    if not units:
        return {}
    r = await run(
        "systemctl",
        ["show", "-p", "Id", "-p", "ActiveState", "-p", "MainPID", *units],
        timeout=10,
    )
    out: dict[str, dict] = {}
    cur: dict | None = None
    for line in r["out"].split("\n"):
        if not line.strip():
            cur = None  # 空行分隔各 unit 的属性块
            continue
        key, sep, val = line.partition("=")
        if not sep:
            continue
        val = val.strip()
        if key == "Id":
            cur = {"active": False, "mainPid": 0}
            out[val] = cur
            continue
        if cur is None:
            continue
        if key == "ActiveState":
            cur["active"] = val == "active"
        elif key == "MainPID":
            try:
                cur["mainPid"] = int(val)
            except ValueError:
                cur["mainPid"] = 0
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
