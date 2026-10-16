"""端口暴露面对账：这台机器在听哪些端口、每个端口能不能从外面打进来。

为什么值得做：自检里已有的「监听地址」只看面板自己绑在哪，防火墙项只看规则表。
两者都对，合起来却答不出管理员真正怕的那个问题——"我有没有在不知情的情况下，
把一个只该本机访问的口子开到了公网"。一个忘了加 127.0.0.1 绑定的调试服务、一个
docker 顺手 publish 出来的端口，正好都是别人进这台机器的入口。

做法是把三份本已在机器上的事实拼起来：`ss -tlnp` 的实际监听表、防火墙此刻放行了
哪些端口、面板登记的应用端口与域名，再对每个端口给一个判词。

严格只读：不改绑定、不动防火墙。修复一律是给人复制的命令——把端口从全网收回到
回环要改的是那个服务自己的启动参数，面板替不了它，猜错了还会把一个正常服务关掉。

两处如实承认局限，不装成全知：
① 非 root 时 `ss` 不给占用进程，判词只能靠端口号，标 as_root=false；
② 判定只看本机防火墙，云上安全组、上游 NAT 都在本机视野之外，写进 notes。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from datetime import UTC, datetime

from .. import database as dbm
from .. import probe
from ..util import CTRL_TIMEOUT, run
from . import firewall_service

CACHE_TTL = 300.0
MAX_PORTS = 300
MAX_LIST_DETAIL = 6

INTERNAL = "internal"
EXPECTED = "expected"
BLOCKED = "blocked"
EXPOSED = "exposed"
# 第五档：防火墙那一份事实没读到，所以这个端口外面进不进得来**判不了**。
# 有了它才不必在"假绿灯（blocked）"和"假警报（exposed）"之间二选一——原来 `ufw status`
# 一超时，面板就把 rules=[] 当成"防火墙没放行"，把每个绑全网、其实被 ufw 拦得死死的端口
# 都盖章成安全，而这一页存在的意义就是揪出这种端口。
UNKNOWN = "unknown"

# 天生就要对外提供服务的进程 + 端口：web/反代监听 80/443 不是"多开的口子"，
# 把它算成风险会让这一页第一条就是噪音，之后没人再看。
PUBLIC_DAEMONS = {"nginx", "apache2", "httpd", "caddy", "openresty", "haproxy", "traefik", "envoy"}
WEB_PORTS = {80, 443, 8080, 8443}

# 容器发布端口：docker 在 PREROUTING 里做 DNAT，流量根本不经过 ufw 的那张 allow 表，
# 所以"防火墙没放行"对它是假安慰，一律按可达处理并说明原因。
DOCKER_PROXIES = {"docker-proxy", "dockerd"}

# 天生就要监听全网的链路本地协议：mDNS/LLMNR 绑在 0.0.0.0 上是设计如此，报文出不了
# 这一条网段。把它们算成"多开的口子"，第一次看这一页就会看到两条假警报，之后没人再信。
LINK_LOCAL_PORTS = {5353, 5355}

_LOCK = asyncio.Lock()
_cache: dict = probe.new_cache()

_NAME_RE = re.compile(r'"([^"]*)"')
_PID_RE = re.compile(r"\bpid=(\d+)")


def _bind_scope(host: str) -> str:
    """把 `ss` 的本地地址归成三档：回环 / 链路本地 / 对外（含全网与指定公网地址）。

    `%lo` 要认：systemd-resolved 在 Debian 上写的是 `127.0.0.53%lo`，只看前缀会把它
    误判成对外端口。`fe80::` 只在一条链路上有效，路由不到公网，也不算对外。
    """
    raw = host.strip("[]")
    if raw in ("*", "", "0.0.0.0", "::", "::0", "any"):
        return "wildcard"
    if raw.startswith("127.") or raw == "::1" or raw == "localhost" or raw.endswith("%lo"):
        return "loopback"
    if raw.startswith("fe80"):
        return "link-local"
    return "public-ip"


_SCOPE_RANK = {"loopback": 0, "link-local": 1, "public-ip": 2, "wildcard": 3}
_VERDICT_RANK = {EXPOSED: 0, UNKNOWN: 1, BLOCKED: 2, EXPECTED: 3, INTERNAL: 4}


def parse_ss(text: str) -> list[dict]:
    r"""`ss -tlnp` → 每个监听端口一条记录。

    真实行形如：
        LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=901,fd=6),("nginx",pid=900,fd=6))
    IPv6 的地址本身带冒号（`[::]:80`），所以用 rpartition 取最后一段当端口，不数空格列。
    同一端口在 v4/v6 上各监听一次是常态，这里按端口合并，把两个地址都收进 addrs。
    """
    by_port: dict[int, dict] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != "LISTEN":
            continue
        local = fields[3]
        host, sep, port_text = local.rpartition(":")
        if not sep or not port_text.isdigit():
            continue
        port = int(port_text)
        if not 1 <= port <= 65535:
            continue
        rest = " ".join(fields[5:])
        names = [n for n in _NAME_RE.findall(rest) if n]
        pids = sorted({int(m) for m in _PID_RE.findall(rest)})
        scope = _bind_scope(host)
        row = by_port.setdefault(
            port,
            {"port": port, "addrs": [], "scopes": set(), "names": set(), "pids": set()},
        )
        row["addrs"].append(f"{host}:{port}")
        row["scopes"].add(scope)
        row["names"].update(names)
        row["pids"].update(pids)
    rows: list[dict] = []
    for port, row in by_port.items():
        # 取最宽的一档作为端口的对外性：同一端口只要有一个地址能被外面碰到，就算能。
        widest = max(row["scopes"], key=lambda s: _SCOPE_RANK[s])
        rows.append(
            {
                "port": port,
                "addrs": sorted(row["addrs"]),
                "scope": widest,
                "external": widest in ("wildcard", "public-ip"),
                "processes": sorted(row["names"]),
                "pids": sorted(row["pids"]),
            }
        )
    return sorted(rows, key=lambda r: r["port"])[:MAX_PORTS]


def _app_ports() -> list[dict]:
    """面板登记过的应用端口：它们是"最该只监听回环"的一群——前面有 nginx。"""
    try:
        rows = dbm.query("SELECT id, name, port, domain FROM apps") or []
    except Exception:  # noqa: BLE001 - 表还没建好时不该让整个探测失败
        return []
    return [r for r in rows if r.get("port")]


async def _firewall() -> tuple[dict, object]:
    """拿防火墙现状，并返回一个"这个端口外面进得来吗"的判据。

    未启用防火墙时答案是"全都进得来"，不是"不知道"——ufw 关掉就是没有过滤，
    把这种机器上的每个对外端口都标成 unknown，等于把最该看的那台机器标成看不了。
    但"没读到"是第三件事：那句判据只能是"判不了"（返回 None），既不能说可达也不能说不通。
    """
    fw = await firewall_service.read()
    rules = fw.get("rules") or []
    active = fw.get("active")
    bad = firewall_service.unread(fw)
    blind = firewall_service.blind_allow_rules(rules)
    default_in = str(fw.get("default_incoming") or "").lower()

    def reachable(port: int, names: list[str]) -> tuple[bool | None, str]:
        if any(n in DOCKER_PROXIES for n in names):
            return True, "容器发布端口走 docker 的 DNAT，不经过 ufw 的放行表"
        if bad:
            return None, f"防火墙现状没读到（{bad}），这个端口能否从外面进来判不了"
        if active is not True:
            return True, "防火墙未启用，非回环地址上的端口都可达"
        if default_in == "allow":
            return True, "防火墙默认放行入站"
        if any(firewall_service.covers(r, port) for r in rules):
            return True, "有放行规则覆盖该端口"
        if blind:
            return (
                None,
                f"{len(blind)} 条 service 规则读不到它开哪些端口，这个端口是否被它们放行判不了",
            )
        return False, "防火墙没有放行该端口"

    return fw, reachable


def _verdict(
    row: dict,
    reachable: bool | None,
    why: str,
    expected: dict[int, str],
    app_by_port: dict,
) -> tuple[str, str, str]:
    """给一个监听端口下判词，并返回（判词，理由，修复命令）。

    判词只有五种，边界写死在这里，网页、CLI 和自检项才不会各说一套。
    `reachable` 是三态：True 可达、False 被挡、None 判不了（防火墙那份事实没读到）。
    """
    port = row["port"]
    names = row["processes"]
    # 非 root 时 ss 不给进程名，文案里就如实说"未知进程"，不编一个。
    who = "、".join(names) if names else "未知进程"
    if not row["external"]:
        return INTERNAL, "只监听本机地址，外部碰不到", ""
    app = app_by_port.get(port)
    if app is not None and app.get("domain"):
        # 有域名走反代还绑全网 = 任何人都能绕开 TLS 和限速直连源站。
        # 这条必须排在链路本地端口白名单前面：端口号 5353/5355 属于 mDNS/LLMNR 是因为
        # avahi 在那儿监听，而面板既然把一个配了域名的应用登记在这个端口上，那就是真的
        # 源站暴露——拿协议白名单把它盖成「报文出不了这条网段」等于替管理员把红灯涂绿。
        return (
            EXPOSED,
            f"面板应用「{app['name']}」配了域名反代，却监听在 {row['addrs'][0]}：可绕过 nginx 直连源站",
            f"把 {app['name']} 的绑定改成 127.0.0.1（改它的启动参数/环境变量），流量只走域名",
        )
    if port in LINK_LOCAL_PORTS:
        return INTERNAL, "链路本地协议（mDNS/LLMNR），报文出不了这一条网段", ""
    if port in expected:
        return EXPECTED, f"{expected[port]}，对外监听符合预期", ""
    if any(n in PUBLIC_DAEMONS for n in names) and port in WEB_PORTS:
        return EXPECTED, "web/反代服务的对外端口", ""
    if reachable is None:
        # 判不了就说判不了。这一档排在 blocked 前面是整批修复的落点：原来防火墙探测一失败，
        # rules 是空的、active 是 False，于是这些端口全被盖成"防火墙没放行"——那是本页最坏的
        # 一种错，因为它把一个可能正对着公网的口子报成安全。
        return (
            UNKNOWN,
            f"{who} 监听在 {row['addrs'][0]}，{why}",
            "先把防火墙状态读出来（choyeonctl firewall list），这一页才判得了外部可达性",
        )
    if not reachable:
        return (
            BLOCKED,
            f"{who} 监听在 {row['addrs'][0]}，但防火墙没放行",
            "本机上的进程仍然能直接访问它：确认不需要对外，就把它改绑 127.0.0.1",
        )
    if app is not None:
        return (
            EXPOSED,
            f"面板应用「{app['name']}」的端口 {port} 外部可达，且没有配域名走反代",
            f"确认这是有意对外；否则把 {app['name']} 改绑 127.0.0.1，再配域名走 nginx",
        )
    return (
        EXPOSED,
        f"{who} 监听在 {row['addrs'][0]}，外部可达（{why}）",
        "先确认用途：不该对外的改绑 127.0.0.1，该对外的在防火墙里显式放行并记下用途",
    )


def _summary(ports: list[dict]) -> dict:
    out = {"total": len(ports), INTERNAL: 0, EXPECTED: 0, BLOCKED: 0, EXPOSED: 0, UNKNOWN: 0}
    for p in ports:
        out[p["verdict"]] = out.get(p["verdict"], 0) + 1
    return out


async def audit(refresh: bool = False) -> dict:
    """带 TTL 的对账入口；并发与连点合并成一轮 `ss` + 防火墙规则读取（见 app/probe.py）。"""
    return await probe.read(_cache, _LOCK, _collect, refresh=refresh, ttl=CACHE_TTL)


async def _collect() -> dict:
    ss = await run("ss", ["-tlnp"], timeout=CTRL_TIMEOUT)
    # ss -p 只有 root 才给进程名；这不是失败，但结论的可信度必须跟着降。
    as_root = bool(shutil.which("ss")) and "users:(" in (ss["out"] or "")
    notes: list[str] = []
    if ss["code"] != 0:
        notes.append(f"ss 退出码 {ss['code']}：{(ss['out'] or '').strip()[:120]}")
    if not shutil.which("ss"):
        notes.append("这台机器上没有 ss（iproute2），无法给出监听表")

    fw, reachable = await _firewall()
    fw_bad = firewall_service.unread(fw)
    if fw_bad:
        notes.append(f"防火墙现状没读到（{fw_bad}）：这一轮所有要看放行表才能判的端口都标成未确认，不判 blocked")
    elif fw.get("active") is not True:
        notes.append(f"防火墙{'未安装' if fw.get('tool') == 'none' else '未启用'}，按全部可达判定")
    notes.append("判定只看本机防火墙；云上安全组、上游 NAT 与别的机器上的白名单都不在本机视野内")

    protected, reasons = firewall_service.protected_ports()
    expected = {int(p): reasons.get(p, "") for p in protected}
    app_rows = _app_ports()
    app_by_port = {int(r["port"]): r for r in app_rows}

    ports: list[dict] = []
    for row in parse_ss(ss["out"] or ""):
        reach, why = reachable(row["port"], row["processes"])
        verdict, reason, fix = _verdict(row, reach, why, expected, app_by_port)
        ports.append(
            {
                **row,
                "pids": sorted(row["pids"]),
                "reachable": reach,
                "verdict": verdict,
                "reason": reason,
                "fix": fix,
            }
        )

    ports.sort(key=lambda p: (_VERDICT_RANK[p["verdict"]], p["port"]))
    apps = [
        {
            "id": r["id"],
            "name": r["name"],
            "port": int(r["port"]),
            "domain": r.get("domain") or "",
            "listening": any(p["port"] == int(r["port"]) for p in ports),
            "verdict": next((p["verdict"] for p in ports if p["port"] == int(r["port"])), None),
        }
        for r in app_rows
    ]
    return {
        "ok": True,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "as_root": as_root,
        # 缺席判定要用到这两条：ss 根本不可用、或条目被 MAX_PORTS 封顶时，
        # "端口不在这份表里"就不能当成"没人听"。
        "ss_available": bool(shutil.which("ss")) and ss["code"] == 0,
        "ports_capped": len(ports) >= MAX_PORTS,
        "firewall": {
            "tool": fw.get("tool"),
            "active": fw.get("active"),
            "state_ok": bool(fw.get("state_ok")),
            "rules_ok": bool(fw.get("rules_ok")),
            "unread": fw_bad,
            "default_incoming": fw.get("default_incoming") or "",
        },
        "counts": _summary(ports),
        "ports": ports,
        "apps": apps,
        "notes": notes,
    }


def _port_label(row: dict) -> str:
    """给人看的端口标识：`3100(next-server:public-ip)`，进程名缺时留 `?`。"""
    who = "、".join(row["processes"]) or "?"
    return f"{row['port']}({who}:{row['scope']})"


def doctor_item(data: dict) -> dict:
    """把对账结果折成自检项。判据放服务层，CLI 与网页才不会各说一套。"""
    counts = data.get("counts") or {}
    ports = data.get("ports") or []
    exposed = [p for p in ports if p["verdict"] == EXPOSED]
    blocked = [p for p in ports if p["verdict"] == BLOCKED]
    unknown = [p for p in ports if p["verdict"] == UNKNOWN]
    if not ports:
        return {"status": "warn", "detail": "读不到监听端口表（缺 ss 或 ss 失败），无法判断暴露面", "fix": ""}
    if exposed:
        names = "、".join(_port_label(p) for p in exposed[:MAX_LIST_DETAIL])
        more = f" 等 {len(exposed)} 个" if len(exposed) > MAX_LIST_DETAIL else ""
        return {
            "status": "warn",
            "detail": f"{len(exposed)} 个端口绑在非回环地址且外部可达：{names}{more}",
            "fix": "choyeonctl ports",
        }
    if unknown:
        # 「一个都没暴露」和「一个都没判成」在绿灯上长得一模一样，而后者才是这台机器上的
        # 真实情况时，这一页就不能再报"没有对外多开的口子"——那句话把人引去干别的事了。
        why = (data.get("firewall") or {}).get("unread") or "防火墙状态没读到"
        return {
            "status": "warn",
            "detail": f"{len(unknown)} 个绑在非回环地址的端口判不了外部可达性（{why}）",
            "fix": "choyeonctl firewall list",
        }
    detail = f"{counts.get('total', 0)} 个监听端口，没有对外多开的口子"
    if blocked:
        detail += f"（另有 {len(blocked)} 个绑在全网但被防火墙挡住，建议改绑回环）"
    if not data.get("as_root"):
        detail += "；本次非 root 探测，看不到占用进程，判定不完整"
    return {"status": "pass", "detail": detail, "fix": ""}
