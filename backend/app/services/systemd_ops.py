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


async def service_action(unit: str, verb: str) -> str:
    if verb not in VERBS:
        raise RuntimeError(f"verb not allowed: {verb}")
    r = await run("systemctl", [verb, unit], timeout=60)
    if r["code"] != 0:
        raise RuntimeError(r["out"] or f"systemctl {verb} failed")
    return r["out"]
