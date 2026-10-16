"""SSH 爆破来源的一键封禁：给「过去 24 小时 N 次失败认证」那句判词接上一个能按的按钮。

这一层不重新发明防火墙：规则仍然由 `firewall_service` 写，删除仍然按编号 + 签名，删除前仍然过
自锁判定。它只回答三件事——封谁（单个公网地址）、这条是谁封的（备注 / 命名集合里带 `choyeon-ban`
标记）、封完到底生效没有（写完重新列一次，不拿命令的退出码当证据：ufw 打印 Aborted 却返回 0
是这台机器上实测过的行为）。

为什么面板里已经有防火墙页了还要这一层：防火墙页要管理员自己判断"这个地址能不能封"，而它面对
的是 34 个地址。判词说得出有多少次失败，却没有任何一个地方能把门关上——那等于只报了个信。
装 fail2ban 仍是更好的长期答案，那条建议照旧留着；这条路径管的是"此刻还没装"的那段时间。
"""

from __future__ import annotations

import ipaddress

from ..errors import BadInput, ExecFailed, Forbidden
from ..util import run
from . import firewall_service as fw
from . import login_audit_service as login_audit

# ufw 写一条规则要重建用户侧脚本，20 秒的探测超时不够，与 firewall_service 的写操作同一档。
CMD_TIMEOUT = 60

# 封禁的"署名"。ufw 的规则可以带备注，所以备注前缀就是唯一认领依据；
# firewalld 的 rich rule 没有备注字段，改用命名集合，两者都不靠"看起来像"来认。
MARKER = "choyeon-ban"
IPSET = "choyeon-ban"
IPSET_RICH = f'rule source ipset="{IPSET}" drop'
MAX_BANS = 50
# 备注里的失败次数只是给人看的那一栏，但它会进 ufw 的 comment 字段，所以照样封顶：
# 写歪的数字把备注顶出 MAX_COMMENT，ufw 会连规则一起拒收，"封禁失败"看起来就像防火墙坏了。
MAX_HITS = 10_000_000


def _fail(out: str, tool: str) -> ExecFailed:
    """把工具的原文收成一句人话。原文可能带路径与命令细节，只取前 200 字符。"""
    text = " ".join((out or "").split())[:200]
    return ExecFailed(f"{tool} 拒绝执行：{text}" if text else f"{tool} 执行失败")


def _is_addr(raw: str) -> bool:
    try:
        ipaddress.ip_address((raw or "").strip())
    except ValueError:
        return False
    return True


def _public_addr(raw: str) -> str:
    """只接受单个公网可路由地址：不接受 CIDR 段、域名、端口，也不接受内网地址。

    不接受 CIDR 是刻意的：一条 `deny from 10.0.0.0/8` 在这种机器上等于自断后路，
    而"批量封一段"本来就该由防火墙页那条带自锁判定的路去做。
    """
    text = (raw or "").strip()
    if not text:
        raise BadInput("要封禁的地址不能为空")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise BadInput("只能封单个 IP 地址（不接受 CIDR 段、域名或带端口的写法）") from None
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    ):
        raise Forbidden(f"{ip} 是内网/本机保留地址，封掉它更可能把自己关在外面")
    return str(ip)


def _is_mine(comment: str) -> bool:
    """这条备注是不是面板自己写的。

    只按"以标记开头"认领会把 `choyeon-banana` 这种撞前缀的人工备注也算进来，
    于是列表里多出一条不是面板封的封禁，`rm` 还会替人把它删掉。
    """
    text = (comment or "").strip()
    return text == MARKER or text.startswith(MARKER + " ")


def _comment(hits: int) -> str:
    """备注必须过 `firewall_service.COMMENT_RE`：只放中英文、数字、空格与 . : @ / # + _ -。

    数字夹进 `MAX_HITS` 才算：`hits` 是客户端列表里读回来的显示值，服务端替它决定
    "是不是爆破"是不行的，但也不能让它把备注写得连规则都落不下去。
    """
    try:
        n = max(0, min(int(hits), MAX_HITS))
    except (TypeError, ValueError):
        n = 0
    return f"{MARKER} ssh 爆破 {n} 次/24h" if n else f"{MARKER} ssh 爆破"


async def _accepted_ips() -> dict[str, int]:
    """过去 24h **成功**登录过的来源 → 次数。读不到日志时返回空表，不返回"没人登录过"。"""
    data = await login_audit.attempts()
    if not data.get("ok"):
        return {}
    out: dict[str, int] = {}
    for row in data.get("accepted_ips") or []:
        ip = str(row.get("ip") or "")
        if ip:
            out[ip] = int(row.get("count") or 0)
    return out


async def _entries(scope: list[str]) -> tuple[list[str], str]:
    """读 firewalld 命名集合的成员。「集合还不存在」等于一条封禁也没有，不是读不到。"""
    r = await run("firewall-cmd", scope + [f"--ipset={IPSET}", "--get-entries"], timeout=CMD_TIMEOUT)
    text = (r["out"] or "").strip()
    if r["code"] == 0:
        return [line.strip() for line in text.splitlines() if _is_addr(line)], ""
    low = text.lower()
    if not text or "not existing" in low or "not found" in low:
        return [], ""
    return [], text[:160]


async def _ensure_ipset() -> None:
    """建集合并把"丢弃整个集合"的规则同时写进运行期与 permanent。

    两份都要写：只写运行期的话，下一次 `--reload`（别人干的，不是我们）就把封禁静默清空，
    而列表里那些地址看着还在 permanent 集合中——这正是面板最怕的"看着安全"。
    """
    for scope in (["--permanent"], []):
        r = await run("firewall-cmd", scope + [f"--new-ipset={IPSET}", "--type=hash:ip"], timeout=CMD_TIMEOUT)
        if r["code"] != 0 and "already exists" not in (r["out"] or "").lower():
            raise _fail(r["out"] or "", "firewalld")
    bound = await run("firewall-cmd", ["--query-rich-rule", IPSET_RICH], timeout=CMD_TIMEOUT)
    if bound["code"] != 0:
        for scope in (["--permanent"], []):
            r = await run("firewall-cmd", scope + [f"--add-rich-rule={IPSET_RICH}"], timeout=CMD_TIMEOUT)
            if r["code"] != 0:
                raise _fail(r["out"] or "", "firewalld")


async def _ipset_entry(op: str, ip: str) -> str:
    """对某个地址执行 add/remove，运行期与 permanent 各一次；返回没做成那部分的原因。"""
    fails: list[str] = []
    for scope in (["--permanent"], []):
        r = await run("firewall-cmd", scope + [f"--ipset={IPSET}", f"--{op}-entry={ip}"], timeout=CMD_TIMEOUT)
        if r["code"] != 0:
            fails.append(" ".join((r["out"] or "").split())[:120])
    return "；".join(fails)[:200]


async def list_bans(tool: str | None = None) -> dict:
    """面板认得的封禁。读不到规则表时 `ok=false` 并带上原因——空列表和"没读到"是两回事。"""
    tool = tool or await fw.detect()
    base: dict = {"tool": tool or "none", "ok": False, "reason": "", "marker": MARKER, "maxBans": MAX_BANS, "bans": []}
    if tool == fw.UFW:
        cur = await fw.read(tool)
        if bad := fw.rules_unread(cur):
            base["reason"] = bad
            return base
        base["ok"] = True
        for rule in cur["rules"]:
            comment = str(rule.get("comment") or "")
            src = str(rule.get("source") or "").strip()
            if rule.get("action") != "deny" or not _is_mine(comment) or not _is_addr(src):
                continue
            base["bans"].append({
                "ip": src, "num": rule.get("num") or 0, "sig": rule.get("sig") or "",
                "comment": comment, "runtime": True, "permanent": True,
            })
        return base
    if tool == fw.FIREWALLD:
        runtime, err_rt = await _entries([])
        permanent, err_pm = await _entries(["--permanent"])
        if err_rt and err_pm:
            base["reason"] = err_rt or err_pm
            return base
        base["ok"] = True
        for ip in sorted(set(runtime) | set(permanent), key=lambda s: (s.count(":"), s)):
            base["bans"].append({
                "ip": ip, "num": 0, "sig": "", "comment": f"{MARKER}（命名集合 {IPSET}）",
                "runtime": ip in runtime, "permanent": ip in permanent,
            })
        return base
    base["reason"] = "这台机器上没有 ufw / firewalld，面板封不了地址"
    return base


async def ban(ip: str, request=None, hits: int = 0) -> dict:
    """封一个地址，写完重新列一次确认它真的在里面。"""
    tool = await fw.detect()
    if tool not in (fw.UFW, fw.FIREWALLD):
        raise Forbidden("这台机器上没有 ufw / firewalld，面板封不了地址；长期方案是装上 fail2ban 的 sshd jail")
    target = _public_addr(ip)
    cur = await list_bans(tool)
    if not cur["ok"]:
        raise Forbidden(fw.unread_refusal(cur["reason"]))
    if any(b["ip"] == target for b in cur["bans"]):
        return {"ok": True, "already": True, "tool": tool, "bans": cur["bans"],
                "detail": f"{target} 已经在封禁列表里，无需重复"}
    if len(cur["bans"]) >= MAX_BANS:
        raise BadInput(f"封禁列表已有 {len(cur['bans'])} 条（上限 {MAX_BANS}），先解封不再需要的地址再加")

    # 这个地址既在爆破又成功登录过（口令被猜中过一次）时照样封，但要把话说在前面：
    # 静默踢掉一个正在工作的会话，比多打一行提醒难查得多。
    note = ""
    accepted = await _accepted_ips()
    if accepted.get(target):
        note = f"提醒：{target} 过去 24 小时还成功登录过 {accepted[target]} 次，封掉会同时断开那个会话。"

    if tool == fw.UFW:
        r = await run("ufw", ["insert", "1", "deny", "from", target, "comment", _comment(hits)], timeout=CMD_TIMEOUT)
        if r["code"] != 0:
            raise _fail(r["out"] or "", "ufw")
    else:
        await _ensure_ipset()
        if err := await _ipset_entry("add", target):
            after = await list_bans(tool)
            if not any(b["ip"] == target and b["runtime"] and b["permanent"] for b in after["bans"]):
                raise ExecFailed(f"firewalld 没有把 {target} 完整写进两份配置：{err}")

    after = await list_bans(tool)
    if not any(b["ip"] == target for b in after["bans"]):
        raise ExecFailed("命令说加好了，但重新列出来的封禁里没有这个地址——不算成功")
    return {"ok": True, "added": True, "tool": tool, "bans": after["bans"],
            "detail": (note + f"已封禁 {target}").strip()}


async def unban(ip: str, num: int = 0, expect: str = "", request=None) -> dict:
    """解封。ufw 走 `firewall_service.remove_rule`：编号会移动，所以仍要那次签名比对与自锁判定。"""
    tool = await fw.detect()
    if tool == fw.UFW:
        if not num:
            raise BadInput("ufw 按规则编号删除，请带上这条封禁的编号与签名")
        out = await fw.remove_rule(num, expect, tool, request)
        after = await list_bans(tool)
        return {"ok": True, "tool": tool, "bans": after["bans"],
                "detail": f"已解封（{out.get('removed') or '规则已删除'}）"}
    if tool == fw.FIREWALLD:
        target = _public_addr(ip)
        cur = await list_bans(tool)
        if not cur["ok"]:
            raise Forbidden(fw.unread_refusal(cur["reason"]))
        if not any(b["ip"] == target for b in cur["bans"]):
            raise BadInput(f"{target} 不在面板的封禁列表里")
        err = await _ipset_entry("remove", target)
        after = await list_bans(tool)
        if any(b["ip"] == target for b in after["bans"]):
            raise ExecFailed(f"firewalld 说删了，但集合里还有 {target}：{err or '两份配置未对齐'}")
        return {"ok": True, "tool": tool, "bans": after["bans"], "detail": f"已解封 {target}"}
    raise Forbidden("这台机器上没有 ufw / firewalld，面板没有可解封的东西")
