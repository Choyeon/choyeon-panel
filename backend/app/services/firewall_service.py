"""防火墙管理：ufw / firewalld 两套后端 + 自锁保护。

这一层存在的理由不是"替管理员执行 ufw"，而是执行前替他回答"这条规则删掉后我还进得来吗"。
面板此前对防火墙只读，设置页上写的是"开启/改规则请 SSH 手动操作，以免误锁 SSH"——那句
免责声明本身就是缺陷：误锁的风险控制不了（ufw 从来没有"只锁别人"的规则），能做的是在写操作
前把会导致锁死的端口挑出来并拒绝执行。见 `protected_ports` 与 `lockout_reasons`。

所有命令都走 argv（`util.run`），不经过 shell；端口、协议、来源、注释先校验再拼接，来源统一
用 `ipaddress` 归一化（因此永远不接受域名），备注禁止以 `-` 开头，否则会被当成命令选项。
"""

from __future__ import annotations

import ipaddress
import re
from glob import glob
from pathlib import Path

from .. import config
from ..errors import BadInput, ExecFailed, Forbidden
from ..util import run

UFW = "ufw"
FIREWALLD = "firewalld"
PORT_RE = re.compile(r"^\d+$")
COMMENT_RE = re.compile(r"^[\w .:@#/+\u4e00-\u9fff-]*$")
MAX_COMMENT = 40
MAX_RANGE_SPAN = 1000
SSHD_CONFIG = "/etc/ssh/sshd_config"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
NGINX_CONF_GLOBS = ("/etc/nginx/conf.d/*.conf", "/etc/nginx/sites-enabled/*")
MAX_NGINX_FILES = 40
_SERVER_HEAD_RE = re.compile(r"\bserver\s*\{", re.I)
_PROXY_PASS_RE = re.compile(r"proxy_pass\s+[^;\s]*?://([^;/\s]+)", re.I)
_LISTEN_RE = re.compile(r"^[ \t]*listen[ \t]+([^;\n]+);", re.I | re.M)
_LISTEN_PORT_RE = re.compile(r"(?:^|[:\]])(\d{1,5})$")


# --------------------------------------------------------------------- 检测
async def detect() -> str | None:
    for tool, probe in ((UFW, "ufw"), (FIREWALLD, "firewall-cmd")):
        if (await run("which", [probe], timeout=10))["code"] == 0:
            return tool
    return None


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in LOOPBACK_HOSTS


def sshd_ports() -> set[int]:
    """sshd 实际监听的端口，用于"绝不能锁掉 SSH"的判定。

    Port 可以重复出现，发行版也常把片段放在 Include 的目录里（Ubuntu 的 sshd_config.d）。
    读不到就按 22 处理——宁可多保护一个端口，也不能少保护一个。
    """
    ports: set[int] = set()
    seen: set[str] = set()

    def collect(path: Path, depth: int):
        if depth > 1 or str(path) in seen or not path.is_file():
            return
        seen.add(str(path))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            parts = s.split()
            key = parts[0].lower() if parts else ""
            if len(parts) == 2 and key == "port" and PORT_RE.match(parts[1]):
                p = int(parts[1])
                if 1 <= p <= 65535:
                    ports.add(p)
            elif len(parts) >= 2 and key == "include" and depth == 0:
                for token in parts[1:]:
                    target = token if Path(token).is_absolute() else str(path.parent / token)
                    for hit in sorted(glob(target))[:20]:
                        collect(Path(hit), depth + 1)

    collect(Path(SSHD_CONFIG), 0)
    return ports or {22}


def arrival_ports(request) -> set[int]:
    """管理员此刻正从哪个端口进出这台机器。

    面板只监听回环，浏览器地址栏上的 443 才是真正被防火墙过滤的入口，而这个端口后端拿不到
    ——`req.url` 是反代内部重写后的值。Origin/Referer/X-Forwarded-Host 里带着浏览器实际使用的
    端口，删掉对应的放行规则等于把当前这个会话的自己关在门外。
    """
    ports: set[int] = set()
    headers = getattr(request, "headers", {}) or {}
    client = getattr(getattr(request, "client", None), "host", "") or ""
    candidates = [headers.get("origin", ""), headers.get("referer", ""), headers.get("x-forwarded-host", "")]
    if client and not _loopback(client):
        candidates.append(headers.get("host", ""))
    for raw in candidates:
        value = (raw or "").strip()
        if not value:
            continue
        scheme = ""
        if "://" in value:
            scheme, value = value.split("://", 1)
        value = value.split("/", 1)[0]
        if not value:
            continue
        head, _, tail = value.rpartition(":")
        if head and PORT_RE.match(tail):
            ports.add(int(tail))
        elif scheme.lower() == "https":
            ports.add(443)
    return ports


def _server_blocks(text: str) -> list[str]:
    """把 nginx 配置切成 server 块的内容（不含外层花括号），注释先剥掉。

    按花括号配对而不是按行切：一个文件里通常有好几个 server 块（443 反代 + 80 跳转），
    只有真正 proxy_pass 到面板的那个块的 listen 才是入口——80 那块只做 301，删了它的
    放行规则不至于进不了面板。
    """
    stripped = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    blocks: list[str] = []
    for m in _SERVER_HEAD_RE.finditer(stripped):
        start = m.end()
        depth = 0
        i = start - 1
        while i < len(stripped):
            ch = stripped[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(stripped[start:i])
                    break
            i += 1
    return blocks


def _targets_panel(block: str) -> bool:
    """这个 server 块是否把请求转给面板自己（回环地址 + 面板端口）。"""
    for raw in _PROXY_PASS_RE.findall(block):
        host, _, port = raw.rpartition(":")
        if port == str(config.PORT) and (not host or host.strip("[]") in LOOPBACK_HOSTS):
            return True
    return False


def panel_entry_ports() -> set[int]:
    """反代到面板的 nginx server 块实际监听的端口。

    `arrival_ports` 只在请求带 Origin/Referer 时才看得到浏览器用的端口；CLI、脚本或任何
    不带这些头的调用看到的管理员是"隐形"的，而这台机器上真正被 ufw 过滤的是 nginx 那一侧
    的 443。配置就在本机，直接读——把 443 的放行规则删掉等于把面板的门关上。
    """
    ports: set[int] = set()
    for pattern in NGINX_CONF_GLOBS:
        for path in sorted(glob(pattern))[:MAX_NGINX_FILES]:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for block in _server_blocks(text):
                if not _targets_panel(block):
                    continue
                for value in _LISTEN_RE.findall(block):
                    for token in value.split():
                        m = _LISTEN_PORT_RE.search(token)
                        if m and 1 <= int(m.group(1)) <= 65535:
                            ports.add(int(m.group(1)))
    return ports


def protected_ports(request=None) -> tuple[set[int], dict[int, str]]:
    """绝不能被删除/拒绝、且启用防火墙前必须已放行的端口，以及各自的理由。

    面板端口只在绑定到非回环地址时才纳入保护：ufw 从不过滤 lo，一台按 AGENTS.md 只监听
    127.0.0.1 的机器上，把 3210 列进"启用前必须放行"会凭空挡住一次正常启用。
    """
    reasons: dict[int, str] = {p: "sshd 监听端口" for p in sshd_ports()}
    if not _loopback(config.HOST):
        reasons[config.PORT] = "面板自身端口"
    for p in panel_entry_ports():
        reasons.setdefault(p, "反代到面板的 nginx 入口端口")
    if request is not None:
        for p in arrival_ports(request):
            reasons.setdefault(p, "你当前访问面板所用的端口")
    return set(reasons), reasons


# --------------------------------------------------------------------- 规则模型
def _sig(to: str, action: str, src: str) -> str:
    return f"{to}|{action}|{src}".lower()


def _parse_port_spec(raw: str) -> tuple[int | None, int | None, str]:
    """`8000:8100/tcp` → (8000, 8100, 'tcp')；`any` 或非数字 → (None, None, proto)。"""
    body, _, proto_raw = raw.partition("/")
    proto = proto_raw.lower() if proto_raw.lower() in ("tcp", "udp") else "both"
    if body in ("any", "*", ""):
        return None, None, proto
    if "-" in body and ":" not in body:  # firewalld 用短横线表示区间
        lo, _, hi = body.partition("-")
        if PORT_RE.match(lo) and PORT_RE.match(hi):
            return int(lo), int(hi), proto
    if ":" in body:
        lo, _, hi = body.partition(":")
        if PORT_RE.match(lo) and PORT_RE.match(hi):
            return int(lo), int(hi), proto
        return None, None, proto
    return (int(body), None, proto) if PORT_RE.match(body) else (None, None, proto)


def _rule(**kw) -> dict:
    base = {
        "num": 0,
        "kind": "port",
        "token": "",
        "raw": "",
        "action": "allow",
        "direction": "in",
        "proto": "both",
        "port": None,
        "port_to": None,
        "source": "Anywhere",
        "family": "ipv4",
        "comment": "",
        "protected": False,
    }
    base.update(kw)
    tail = "|ipv6" if base.get("family") == "ipv6" else ""
    base["sig"] = _sig(base["raw"], base["action"].upper(), base["source"]) + tail
    return base


def parse_ufw(text: str) -> list[dict]:
    r"""`ufw status numbered` → 结构化规则。

    列间是两个以上空格（`(v6)` 与 `# 注释` 都靠这个切开），所以按 `\s{2,}` 分列而不是固定
    宽度：ufw 改版本时列宽会变，分隔符不会。
    """
    rules: list[dict] = []
    for line in text.splitlines():
        m = re.match(r"^\[\s*(\d+)\]\s+(.*)$", line.rstrip())
        if not m:
            continue
        cols = [c.strip() for c in re.split(r"\s{2,}", m.group(2).strip()) if c.strip()]
        if not cols:
            continue
        to = cols[0]
        family = "ipv4"
        if to.endswith("(v6)"):
            to = to[: -len("(v6)")].strip()
            family = "ipv6"
        action, direction = "allow", "in"
        rest = cols[1:]
        if rest:
            words = rest[0].split()
            # LIMIT 也得认：`ufw limit 22/tcp` 在状态表里的动作列就是 "LIMIT IN"（线上实测），
            # 只认 ALLOW/DENY 的话这一格会被当成来源，规则表里的来源变成 "LIMIT IN Anywhere"。
            if words and words[0].upper() in ("ALLOW", "DENY", "LIMIT"):
                action = words[0].lower()
                direction = words[1].lower() if len(words) > 1 else "in"
                rest = rest[1:]
        tail = " ".join(rest).strip()
        src_part, _, comment_part = tail.partition("#")
        source = src_part.strip() or "Anywhere"
        if family == "ipv6" and source.endswith("(v6)"):
            source = source[: -len("(v6)")].strip() or "Anywhere"
            family = "ipv6"
        port, port_to, proto = _parse_port_spec(to)
        rules.append(
            _rule(
                num=int(m.group(1)),
                raw=to,
                action=action,
                direction=direction if direction in ("in", "out") else "in",
                proto=proto,
                port=port,
                port_to=port_to,
                source=source,
                family=family,
                comment=comment_part.strip(),
            )
        )
    return rules


_RICH_SRC = re.compile(r'source address="([^"]+)"')
_RICH_PORT = re.compile(r'port port="([^"]+)"')
_RICH_PROTO = re.compile(r'protocol="([^"]+)"')


def _rich_rule(body: str, num: int, scope: str) -> dict:
    src_m = _RICH_SRC.search(body)
    port_m = _RICH_PORT.search(body)
    proto_m = _RICH_PROTO.search(body)
    port, port_to = (None, None)
    if port_m:
        port, port_to, _ = _parse_port_spec(port_m.group(1))
    proto_raw = proto_m.group(1).lower() if proto_m else "both"
    return _rule(
        num=num,
        kind="rich",
        token=body,
        raw=body,
        action="deny" if re.search(r"\b(drop|reject)\b", body) else "allow",
        proto=proto_raw if proto_raw in ("tcp", "udp") else "both",
        port=port,
        port_to=port_to,
        source=src_m.group(1) if src_m else "Anywhere",
        family="ipv6" if 'family="ipv6"' in body else "ipv4",
        scope=scope,
    )


def parse_firewalld(list_all: str, scope: str = "runtime") -> list[dict]:
    """`firewall-cmd [--permanent] --list-all` → 结构化规则（services / ports / rich rules）。

    富规则是 `rich rules:` 段落下面的缩进行，本身不含 `key:` 结构，所以按 `rule ` 前缀单独
    识别——只按段落解析会在缩进行上全部漏掉（第一版就漏了，靠探针输出才发现）。
    """
    rules: list[dict] = []
    for line in list_all.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("rule "):
            rules.append(_rich_rule(s, len(rules) + 1, scope))
            continue
        if ":" not in s:
            continue
        key, _, val = s.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "services":
            for name in val.split():
                rules.append(_rule(num=len(rules) + 1, kind="service", token=name, raw=f"service:{name}", scope=scope))
        elif key == "ports":
            for token in val.split():
                port, port_to, proto = _parse_port_spec(token)
                rules.append(
                    _rule(
                        num=len(rules) + 1,
                        kind="port",
                        token=token,
                        raw=token,
                        proto=proto,
                        port=port,
                        port_to=port_to,
                        scope=scope,
                    )
                )
    return rules


async def _enrich_service_ports(rules: list[dict]) -> None:
    """把 firewalld 的 service 规则翻译成它实际开放的端口。

    `services: ssh` 才是这类机器上 22 端口的主要放行方式，自锁判定若不认识它，"删除 ssh
    service"看上去就毫无风险。查不到端口定义时标 ports_known=False——宁可少保护一条，也不
    能把所有删除都挡死（前端会把它单独标出来）。
    """
    for r in rules[:12]:
        if r.get("kind") != "service":
            continue
        info = await run("firewall-cmd", [f"--info-service={r['token']}"], timeout=20)
        vals: list[str] = []
        for line in (info["out"] or "").splitlines():
            s = line.strip()
            if s.startswith("port") and ":" in s:
                vals = s.split(":", 1)[1].split()
                break
        if not vals:
            r["ports_known"] = False
            continue
        specs = [_parse_port_spec(v) for v in vals]
        r["port_specs"] = specs
        r["port"], r["port_to"], r["proto"] = specs[0]
        r["ports_known"] = True


def covers(rule: dict, port: int) -> bool:
    """这条规则是否让 `port` 从外部进得来（自锁判定用它，不看端口是否"重要"）。

    limit 也算放行：ufw 的 limit 是"带限速的 accept"，删掉它 SSH 就真的不通了。
    """
    if rule.get("action") not in ("allow", "limit") or rule.get("direction") != "in":
        return False
    if str(rule.get("source", "")).lower() not in ("anywhere", "any", ""):
        return False
    if rule.get("ports_known") is False:
        return False  # 查不到端口定义的 service，不能假装它覆盖了全部端口
    specs = rule.get("port_specs") or [(rule.get("port"), rule.get("port_to"), rule.get("proto", "both"))]
    return any(
        (lo is None or lo <= port <= (hi or lo)) and pr in ("both", "tcp") for lo, hi, pr in specs
    )


def hits_port(rule: dict, port: int) -> bool:
    """待写入的规则（还没有 source/proto 归一化前的校验结果也行）是否会挡下 `port`。"""
    if str(rule.get("source", "")).lower() not in ("", "anywhere", "any"):
        return False
    if rule.get("proto", "both") not in ("both", "tcp"):
        return False
    lo, hi = rule.get("port"), rule.get("port_to")
    return lo is None or lo <= port <= (hi or lo)


# --------------------------------------------------------------------- 校验
def validate_rule(body: dict) -> dict:
    """把请求体收成一个可执行的规则；不合规一律 BadInput，不做任何猜测。"""
    action = str(body.get("action") or "allow").lower()
    if action not in ("allow", "deny"):
        raise BadInput("action 只能是 allow 或 deny")
    proto = str(body.get("proto") or "tcp").lower()
    if proto not in ("tcp", "udp", "both"):
        raise BadInput("proto 只能是 tcp、udp 或 both")
    rate = bool(body.get("rate"))
    if rate and action != "allow":
        raise BadInput("防暴力破解（限速）只能配合放行使用")

    def as_int(value, label):
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and PORT_RE.match(value))):
            raise BadInput(f"{label}必须是整数")
        p = int(value)
        if not 1 <= p <= 65535:
            raise BadInput(f"{label}范围是 1-65535")
        return p

    p = pi = None
    if body.get("port") not in (None, ""):
        p = as_int(body["port"], "端口")
    if body.get("port_to") not in (None, ""):
        pi = as_int(body["port_to"], "结束端口")
        if p is None:
            raise BadInput("只填结束端口无法确定区间，请同时填开始端口")
        if pi < p:
            raise BadInput("端口区间不合法：结束端口要不小于开始端口")
        if pi - p > MAX_RANGE_SPAN:
            raise BadInput(f"端口区间最多跨 {MAX_RANGE_SPAN} 个端口，避免一条规则打开过大缺口")

    source = ""
    raw_src = str(body.get("source") or "").strip()
    if raw_src and raw_src.lower() not in ("anywhere", "any", "::/0"):
        try:
            net = ipaddress.ip_network(raw_src, strict=False)
        except ValueError:
            raise BadInput("来源只能是 IP 或 CIDR（如 10.0.0.0/8），不接受域名") from None
        if net.prefixlen < 8:
            raise BadInput(f"来源 {net} 覆盖过广（前缀小于 /8），请写具体地址段")
        source = str(net)
    elif raw_src.lower() in ("anywhere", "any", "::/0"):
        source = ""

    comment = str(body.get("comment") or "").strip()
    if comment:
        if len(comment) > MAX_COMMENT:
            raise BadInput(f"备注最多 {MAX_COMMENT} 个字符")
        if comment.startswith("-"):
            raise BadInput("备注不能以 - 开头（会被防火墙当成命令选项）")
        if not COMMENT_RE.match(comment):
            raise BadInput("备注只能使用中英文、数字与 . : @ / # + _ - 空格")
    if p is None:
        raise BadInput("请填写要放行的端口；面板不做「放行全部端口」这种一条敞口的规则")
    return {
        "action": action,
        "proto": proto,
        "rate": rate,
        "port": p,
        "port_to": pi,
        "source": source,
        "comment": comment,
    }


# --------------------------------------------------------------------- 自锁判定
def lockout_hit(rules: list[dict], protected: set[int], target: dict) -> set[int]:
    """删掉 target 之后就不再有任何规则放行的受保护端口。

    只看"这条规则覆盖了受保护端口"会过度拦截：ssh service 与 22/tcp 同时存在时，删掉其中一条
    另一条还开着，面板不该拒绝。同族才算数——ufw 的 `22/tcp` 与 `22/tcp (v6)` 是两条规则，
    删掉 v4 那条之后 v6 仍然"覆盖 22"，但管理员大概率走的是 IPv4，所以孪生规则不能顶岗。
    """
    family = target.get("family") or "ipv4"
    rest = [r for r in rules if r is not target and (r.get("family") or "ipv4") == family]
    return {p for p in protected if covers(target, p) and not any(covers(r, p) for r in rest)}


def lockout_reasons(rules: list[dict], protected: set[int], *, intent: str, target: dict | None = None) -> list[str]:
    """执行前替管理员想清楚的那一步：这条操作会不会把他自己关在门外。

    intent 取 'add' | 'remove' | 'enable' | 'reload'。返回拒绝理由列表，空列表表示放行。
    这里没有"我已确认，继续执行"的参数——能被绕过的保护等于没有保护，而 SSH 一旦锁死
    只能靠云控制台恢复，面板手里没有那个权限。
    """
    if intent == "enable":
        missing = sorted(p for p in protected if not any(covers(r, p) for r in rules))
        if missing:
            return [
                "启用防火墙会立刻生效，而这些端口现在没有任何放行规则："
                + ", ".join(str(p) for p in missing)
                + "。请先添加放行规则（面板可以直接加）再启用。"
            ]
        return []
    if intent == "reload":
        # firewalld 的 --reload 会丢掉只存在于运行期、没写进 permanent 的规则
        missing = sorted(p for p in protected if not any(covers(r, p) for r in rules))
        if missing:
            return [
                "重载会清空运行期未持久化的规则，而这些端口目前只在运行期放行："
                + ", ".join(str(p) for p in missing)
                + "。请先把它们写入 permanent 配置。"
            ]
        return []
    if intent == "remove" and target is not None:
        hit = sorted(lockout_hit(rules, protected, target))
        if hit:
            return [
                f"规则 {target.get('raw') or target.get('token')} 是 "
                + ", ".join(str(p) for p in hit)
                + " 目前唯一的放行来源，删除后这些端口对本机不再开放。"
            ]
        return []
    if intent == "add" and target is not None and target.get("action") == "deny":
        hit = sorted(p for p in protected if hits_port(target, p))
        if hit:
            return ["这条规则会拒绝 " + ", ".join(str(p) for p in hit) + "，其中包含 SSH 或面板正在使用的端口。"]
        return []
    return []


def _reason_words(request, ports: set[int]) -> str:
    _, reasons = protected_ports(request)
    return "、".join(sorted({reasons[p] for p in ports if p in reasons}))


# --------------------------------------------------------------------- 读取
async def read(tool: str | None = None, request=None) -> dict:
    """结构化 + 原文的防火墙状态（保留 `output` 原文：设置页与既有测试都靠它）。"""
    tool = tool or await detect()
    base: dict = {
        "tool": tool or "none",
        "active": False,
        "rules": [],
        "protected": [],
        "protected_reasons": {},
        "output": "",
        "default_incoming": "",
        "logging": "",
    }
    if tool == UFW:
        st = await run("ufw", ["status", "verbose"], timeout=20)
        num = await run("ufw", ["status", "numbered"], timeout=20)
        text = st["out"] or num["out"] or ""
        base["output"] = "\n".join((num["out"] or "").splitlines()[:120])
        base["active"] = bool(re.search(r"Status:\s*active", text))
        m = re.search(r"Default:\s*(\w+)\s*\(incoming\)", text)
        base["default_incoming"] = m.group(1) if m else ""
        lm = re.search(r"Logging:\s*(.+)", text)
        base["logging"] = lm.group(1).strip() if lm else ""
        base["rules"] = parse_ufw(num["out"])
    elif tool == FIREWALLD:
        st = await run("firewall-cmd", ["--state"], timeout=20)
        base["active"] = (st["out"] or "").strip() == "running"
        base["output"] = (st["out"] or "").strip()
        zone = await run("firewall-cmd", ["--get-default-zone"], timeout=20)
        base["zone"] = (zone["out"] or "").strip()
        listed = await run("firewall-cmd", ["--list-all"], timeout=20)
        perm = await run("firewall-cmd", ["--permanent", "--list-all"], timeout=20)
        perm_rules = parse_firewalld(perm["out"], "permanent")
        perm_sigs = {r["sig"] for r in perm_rules}
        rules = parse_firewalld(listed["out"], "runtime")
        await _enrich_service_ports(rules)
        for r in rules:
            r["persisted"] = r["sig"] in perm_sigs
        base["rules"] = rules
    else:
        base["output"] = "未安装 ufw 或 firewalld"
    ports, reasons = protected_ports(request)
    base["protected"] = sorted(ports)
    base["protected_reasons"] = {str(k): v for k, v in reasons.items()}
    for r in base["rules"]:
        r["protected"] = bool(lockout_hit(base["rules"], ports, r))
    return base


def _require_tool(tool: str | None) -> str:
    if tool not in (UFW, FIREWALLD):
        raise ExecFailed("这台机器上没有可管理的防火墙（ufw 与 firewall-cmd 都不存在）")
    return tool


def _tool_message(raw: str, tool: str) -> str:
    """把工具原文压成一句人话。

    ufw/firewall-cmd 的报错会带链名、表名和文件路径，原样透给浏览器等于公开服务器网络结构；
    这里按关键字映射成本地化说法，完整原文只进日志（err_json 之外再由 run() 记录）。
    """
    text = (raw or "").strip()
    table = (
        ("could not be found", "规则不存在或编号已变化，请刷新后重试"),
        ("existing rule", "同样的规则已经存在，无需重复添加"),
        ("already set", "同样的规则已经存在，无需重复添加"),
        ("Invalid", "参数不被防火墙接受，请检查端口与来源写法"),
        ("not running", "firewalld 未在运行，先启用它"),
        ("iptables-restore", "底层 iptables 报错，请用 SSH 检查 iptables 状态"),
    )
    low = text.lower()
    for key, msg in table:
        if key.lower() in low:
            return msg
    first = text.splitlines()[0][:120] if text else "操作失败"
    return f"{tool} 拒绝了这次操作：{first}"


# --------------------------------------------------------------------- 写操作
async def add_rule(body: dict, tool: str, request=None) -> dict:
    _require_tool(tool)
    rule = validate_rule(body)
    ports, _ = protected_ports(request)
    current = await read(tool, request)
    if rule["action"] == "allow" and not rule["source"]:
        dup = [
            r
            for r in current["rules"]
            if r["action"] == "allow"
            and r.get("port") == rule["port"]
            and r.get("port_to") == rule["port_to"]
            and r.get("proto") == rule["proto"]
            and str(r.get("source", "")).lower() in ("anywhere", "any")
        ]
        if dup:
            raise BadInput("同样的放行规则已经存在，无需重复添加")
    why = lockout_reasons(current["rules"], ports, intent="add", target=rule)
    if why:
        hit = sorted(p for p in ports if hits_port(rule, p))
        words = _reason_words(request, set(hit))
        raise Forbidden(f"{why[0]}（用途：{words}）" if hit and words else why[0])

    if tool == UFW:
        verb = "limit" if rule["rate"] else rule["action"]
        spec = f"{rule['port']}:{rule['port_to']}" if rule["port_to"] is not None else str(rule["port"])
        argv: list[str] = [verb]
        if rule["source"]:
            argv += ["from", rule["source"], "to", "any", "port", spec]
            if rule["proto"] != "both":
                argv += ["proto", rule["proto"]]
        else:
            argv.append(spec if rule["proto"] == "both" else f"{spec}/{rule['proto']}")
        if rule["comment"]:
            argv += ["comment", rule["comment"]]
        r = await run("ufw", argv, timeout=60)
    else:
        proto = "tcp" if rule["proto"] == "both" else rule["proto"]
        spec = (
            f"{rule['port']}-{rule['port_to']}"
            if rule["port_to"] is not None
            else (str(rule["port"]) if rule["port"] is not None else None)
        )
        if spec is None:
            raise BadInput("firewalld 的规则需要指定端口")
        if rule["source"]:
            src = rule["source"]
            family = "ipv6" if ":" in src else "ipv4"
            act = "accept" if rule["action"] == "allow" else "drop"
            rich = f'rule family="{family}" source address="{src}" port port="{spec}" protocol="{proto}" {act}'
            argv = ["--permanent", f"--add-rich-rule={rich}"]
        else:
            argv = ["--permanent", f"--add-port={spec}/{proto}"]
        r = await run("firewall-cmd", argv, timeout=60)
        if r["code"] == 0:
            rl = await run("firewall-cmd", ["--reload"], timeout=60)
            r = {"code": rl["code"], "out": (r.get("out") or "") + (rl.get("out") or "")}
    if r["code"] != 0:
        raise ExecFailed(_tool_message(r.get("out") or "", tool))
    return {"ok": True, "detail": (r.get("out") or "").strip()[:200]}


async def remove_rule(num: int, expect: str, tool: str, request=None) -> dict:
    """按编号删除，并要求编号仍指向客户端看到的那条规则。

    ufw 的编号既是显示也是删除的寻址方式，而它会在两次请求之间移动（删一条，后面的全部前移）。
    所以删除前重新列一次、比对签名，对不上就拒绝——这一次比对把"点错了行"的竞态变成"刷新重试"。
    """
    _require_tool(tool)
    if not isinstance(num, int) or not 1 <= num <= 9999:
        raise BadInput("规则编号不合法")
    current = await read(tool, request)
    target = next((r for r in current["rules"] if r["num"] == num), None)
    if target is None:
        raise BadInput("规则表已经变化（该编号不存在），请刷新后重试")
    if expect and expect.lower() != target["sig"]:
        raise BadInput("规则表已经变化（该编号现在对应别的规则），请刷新后重试")
    ports, _ = protected_ports(request)
    why = lockout_reasons(current["rules"], ports, intent="remove", target=target)
    if why:
        hit = sorted(lockout_hit(current["rules"], ports, target))
        raise Forbidden(f"{why[0]}（用途：{_reason_words(request, set(hit))}）" if hit else why[0])

    if tool == UFW:
        # --force 不是省事而是必需：`ufw delete 3` 会追问 "Proceed with operation (y|n)?"，
        # 没有 stdin 时它打印 Aborted 并且**退出码依然是 0**（线上实测），于是每一次删除都
        # "成功"而什么都没删掉。所以既要用 --force 免掉追问，也要删完再列一次确认——
        # "Aborted" 和 "Rule deleted" 在返回码上根本区分不开。
        r = await run("ufw", ["--force", "delete", str(num)], timeout=60)
    else:
        kind = target.get("kind")
        flag = {"rich": "--remove-rich-rule=", "service": "--remove-service=", "port": "--remove-port="}[kind]
        token = target["raw"] if kind == "rich" else target["token"]
        r = await run("firewall-cmd", ["--permanent", f"{flag}{token}"], timeout=60)
        if r["code"] == 0:
            perm_rules = parse_firewalld(
                (await run("firewall-cmd", ["--permanent", "--list-all"], timeout=20))["out"], "permanent"
            )
            await _enrich_service_ports(perm_rules)
            why2 = lockout_reasons(perm_rules, ports, intent="reload")
            if why2:
                # permanent 已经改完，运行期还留着旧规则；此时 reload 才是危险的。
                # 不 reload 的话改动要等重启生效，两害相权：告知用户，交由他决定何时重载。
                return {
                    "ok": True,
                    "removed": target["raw"],
                    "reloaded": False,
                    "detail": why2[0],
                }
            rl = await run("firewall-cmd", ["--reload"], timeout=60)
            r = {"code": rl["code"], "out": (r.get("out") or "") + (rl.get("out") or "")}
    if r["code"] != 0:
        raise ExecFailed(_tool_message(r.get("out") or "", tool))
    still_open: list[str] = []
    if tool == UFW:
        left = await read(tool, request)
        key = (target["raw"], target["action"], target["family"], target["source"])
        if [x for x in left["rules"] if (x["raw"], x["action"], x["family"], x["source"]) == key]:
            raise ExecFailed(f"规则 {target['raw']} 未被删除（仍在规则表里），请用 SSH 检查 ufw 状态")
        # ufw 的 `allow` 不带来源时一次写两条（v4 + v6），而 `delete N` 只删一条编号。
        # 删完只剩孪生那条还在放行同一个端口——报告"已删除"却不提这件事，用户会以为端口关了。
        # 孪生必须按端口身份认：状态表解析出的规则根本没有 token 字段（那是 firewalld 才有的），
        # 按 token 比会让同机所有同动作规则互相误认——线上实测：删掉 3399 的 v6 那条，
        # 22/80/443 全被报成"仍在放行同一端口"。raw 又不带 `(v6)`，所以标签要自己补上，
        # 否则用户看到两条一模一样的 "8000/tcp" 更糊涂。
        still_open = [
            x["raw"] if x.get("family") != "ipv6" else f"{x['raw']}（IPv6）"
            for x in left["rules"]
            if target.get("port") is not None
            and x.get("port") == target.get("port")
            and x.get("port_to") == target.get("port_to")
            and x.get("proto") == target.get("proto")
            and x.get("source") == target.get("source")
            and x.get("action") == target.get("action")
            and x.get("direction") == target.get("direction")
            and x.get("family") != target.get("family")
        ]
    out = {"ok": True, "removed": target["raw"], "detail": (r.get("out") or "").strip()[:200]}
    if still_open:
        out["still_open"] = still_open
    return out


async def set_enabled(active: bool, tool: str, request=None) -> dict:
    _require_tool(tool)
    ports, _ = protected_ports(request)
    if tool == UFW:
        if active:
            current = await read(tool, request)
            why = lockout_reasons(current["rules"], ports, intent="enable")
            if why:
                raise Forbidden(why[0])
            r = await run("ufw", ["--force", "enable"], timeout=90)
        else:
            r = await run("ufw", ["--force", "disable"], timeout=90)
    else:
        if active:
            perm_rules = parse_firewalld(
                (await run("firewall-cmd", ["--permanent", "--list-all"], timeout=20))["out"], "permanent"
            )
            await _enrich_service_ports(perm_rules)
            why = lockout_reasons(perm_rules, ports, intent="enable")
            if why:
                raise Forbidden(why[0])
            r = await run("systemctl", ["enable", "--now", "firewalld"], timeout=90)
        else:
            r = await run("systemctl", ["disable", "--now", "firewalld"], timeout=90)
    if r["code"] != 0:
        raise ExecFailed(_tool_message(r.get("out") or "", tool))
    return {"ok": True, "active": active, "detail": (r.get("out") or "").strip()[:200]}
