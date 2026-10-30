"""系统补丁可见性：只读探测"这台机器还差哪些更新、其中哪些是安全更新"。

为什么值得做：面板已经管住了防火墙、证书、备份这些"自己会坏"的东西，但没管过
"别人的洞顺着这台机器跳过来"这条路——未打的安全更新正是最常见的横向跳板。宝塔与
1Panel 都把补丁管理放进主机页，本面板此前连"有几个待装更新"都答不出来。

这一层严格只读：不 `apt update`（会写 /var/lib/apt/lists 并和 dpkg 抢锁）、不装任何包。
升级命令只准备好、由管理员自己执行。理由有三条：装包会顺带重启别的服务、可能换内核、
失败要人盯——这些都不该由"顺手刷新一下页面"触发。

结果按 TTL 缓存：`apt list --upgradable` 在包多的机器上要跑几秒，而概览页、自检页与 CLI
都会读它；`refresh=True` 强制回源。判不出安全更新子集的发行版如实报 None，不填 0。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from .. import probe
from ..util import PROBE_TIMEOUT, path_state, run
from . import pending_service

CACHE_TTL = 1800.0
MAX_PKGS = 300
MAX_ADVISORIES = 60
APT_TIMEOUT = 120
DNF_TIMEOUT = 180

REBOOT_FLAG = Path("/run/reboot-required")
OS_RELEASE = Path("/etc/os-release")
APT_LISTS = Path("/var/lib/apt/lists")

PKG_CMDS = ("apt", "dnf", "yum", "zypper", "pacman", "apk")

# 自动安全更新的单元名：Debian 系是 unattended-upgrades.service，RHEL 系是 dnf-automatic 的
# timer，SUSE 系是 zypp-update。命中第一个 enabled 的就算开启，都不在才算未开启。
AUTO_UNITS = (
    "unattended-upgrades.service",
    "dnf-automatic-install.timer",
    "dnf-automatic.timer",
    "yum-cron.service",
    "zypp-update.timer",
)

DEBIAN = "debian"
RHEL = "rhel"
ARCH = "arch"
SUSE = "suse"
ALPINE = "alpine"
UNKNOWN = "unknown"

# 按约定「有待装也这样退出」的族：`dnf -q check-update` 与 `zypper lp` 在有更新时回 100。
# 其余族的非零退出码都当成探测失败——但真正的判据见 `_probe_error`。
CODES_WITH_PENDING = (RHEL, SUSE)

# 本地索引超过这个小时数，「没有待装安全更新」就不再能当结论用（见 `_clean_verdict`）。
STALE_TRUST_HOURS = 168.0


def _probe_error(family: str, code: int, rows: list) -> str | None:
    """包管理器这一次到底读到了没有。读不到就不能报「0 个待装」。

    这里守的是一条很具体的假绿：`apt list --upgradable` 因为索引锁、超时（124）、
    命令不存在（127）而失败时，输出是空的，旧代码照旧 `security_known = True`，于是
    自检项输出「没有待装安全更新」——把探测失败粉饰成一台干净的机器，正好是
    「未打的安全更新当跳板」这条路最需要的伪装。

    规则只看一件事：**非零退出码 + 一行都没解析出来** = 读不到。非零但解析出了行，
    数量就是真的（此时不可能假绿），退出码留在 notes 里说明。
    """
    allowed = (0, 100) if family in CODES_WITH_PENDING else (0,)
    if code in allowed or rows:
        return None
    return f"退出码 {code}"

# 自动安全更新的单元名按族给：Debian 系是 unattended-upgrades.service，RHEL 系是
# dnf-automatic 的 timer，SUSE 系是 zypp-update，命中第一个 enabled 的就算开启。
# 只在这三族上探——Arch/Alpine 根本没有这类单元，翻一圈 systemctl 再报「未开启」，
# 等于把「这台机器不这么打补丁」说成「这台机器没人管补丁」。
AUTO_UNITS_BY_FAMILY = {
    DEBIAN: ("unattended-upgrades.service",),
    RHEL: ("dnf-automatic-install.timer", "dnf-automatic.timer", "yum-cron.service"),
    SUSE: ("zypp-update.timer",),
}

# `systemctl is-enabled` 打印的三种能定论的状态。masked 与 disabled 归成同一个结论
# （都是"自动更新没有开着"），其余一律判不出：static/alias/generated 说的是单元类型，
# 不是开没开，把它们折成 disabled 就是替管理员下结论。
_ENABLED_STATES = {"enabled": "enabled", "disabled": "disabled", "masked": "disabled"}

_FAMILY_TOKENS = (
    (DEBIAN, ("debian", "ubuntu")),
    (RHEL, ("rhel", "fedora", "centos")),
    (ARCH, ("arch",)),
    (SUSE, ("suse", "opensuse", "sles")),
    (ALPINE, ("alpine",)),
)
_FAMILY_CMD = {DEBIAN: ("apt",), RHEL: ("dnf", "yum"), ARCH: ("pacman",), SUSE: ("zypper",), ALPINE: ("apk",)}

_LOCK = asyncio.Lock()
_cache: dict = probe.new_cache()


def parse_os_release(text: str) -> dict:
    """只取族判定要用的键，不引入完整 ini 解析。"""
    out: dict = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        out[key.strip()] = value.strip().strip('"')
    return out


def family_of(meta: dict, available: set[str]) -> str:
    """先按 ID / ID_LIKE 判族，判不出来再按包管理器命令在不在。

    ID_LIKE 不能省：Ubuntu 的 ID=ubuntu，只有 `ID_LIKE=debian` 才认得出它走 apt，
    Rocky/Alma 同理。两条都落空的发行版如实报 unknown，不猜。
    """
    ids = [str(meta.get("ID") or "").lower()]
    ids += [x.strip().lower() for x in str(meta.get("ID_LIKE") or "").split()]
    for fam, tokens in _FAMILY_TOKENS:
        if any(tok in ids for tok in tokens):
            return fam
    for fam, cmds in _FAMILY_CMD.items():
        if any(c in available for c in cmds):
            return fam
    return UNKNOWN


def parse_apt_list(text: str) -> list[dict]:
    r"""`apt list --upgradable` → 结构化待装清单。

    真实行形如：
        libssl3/stable-security 3.0.18-1~deb13u2 amd64 [upgradable from: 3.0.18-1~deb13u1]
    多架构包会在行尾并列多个架构与多个旧版本，所以只认"首段必含 `/`、行尾必含
    `[upgradable from:`"，不假设列数。安全更新的判据是 suite 里出现 security：Debian 是
    `stable-security`，Ubuntu 是 `noble-security`（常与 `noble-updates` 逗号并列）。
    """
    pkgs: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Listing") or "[upgradable from:" not in line:
            continue
        head, _, tail = line.partition(" [upgradable from:")
        fields = head.split()
        if not fields or "/" not in fields[0]:
            continue
        name, _, suites = fields[0].partition("/")
        suite_list = [s.strip() for s in suites.split(",") if s.strip()]
        olds = tail.rstrip("]").replace(":", " ").split()
        pkgs.append(
            {
                "name": name,
                "suite": ",".join(suite_list),
                "candidate": fields[1] if len(fields) > 1 else "",
                "current": olds[0] if olds else "",
                "security": any("security" in s.lower() for s in suite_list),
            }
        )
    return pkgs


_CHECK_UPDATE_RE = re.compile(r"^(\S+\.\S+)\s+(\S+)\s+(\S+)\s*$")


def parse_dnf_check_update(text: str) -> list[dict]:
    """`dnf -q check-update`：三列 `包名.架构  版本  仓库`；表头与空行天然匹配不上。

    仓库列在部分发行版叫 `security`/`*-security`，认不出来时不硬判——安全子集改由
    updateinfo 的通告数给出，两边都给不出就报未知。
    """
    pkgs: list[dict] = []
    for line in text.splitlines():
        m = _CHECK_UPDATE_RE.match(line.strip())
        if not m:
            continue
        name, version, repo = m.group(1), m.group(2), m.group(3)
        if name.lower().startswith("last") or version.startswith("meta-"):
            continue
        pkgs.append(
            {
                "name": name,
                "suite": repo,
                "candidate": version,
                "current": "",
                "security": "security" in repo.lower(),
            }
        )
    return pkgs


# Fedora 的通告号尾段是十六进制串（FEDORA-2025-1a2b3c4d），只写 \d+ 会整条漏掉。
_ADVISORY_RE = re.compile(r"\b((?:RH|AL|EL)SA-\d{4}:\d+|FEDORA-\d{4}-[0-9A-Za-z]+|USN-\d+-\d+)")


def parse_dnf_updateinfo(text: str) -> list[dict]:
    """`dnf -q updateinfo list security` → 通告号。

    这套命令在不同 dnf 版本里列序并不一致，所以只抽通告号、不解析列：抽不到就是
    "判不出来"，宁可让上层报未知，也不给一个假的 0。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = _ADVISORY_RE.search(line)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        out.append({"id": m.group(1), "line": line.strip()[:160]})
    return out


def parse_pacman_qu(text: str) -> list[dict]:
    """`pacman -Qu`：`包名 旧版 -> 新版`。Arch 不给安全分类，全按普通更新计。"""
    pkgs: list[dict] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 or "->" not in fields:
            continue
        arrow = fields.index("->")
        name, _, oldver = fields[0].rpartition("/")
        pkgs.append(
            {
                "name": name or fields[0],
                "suite": "",
                "candidate": fields[arrow + 1] if arrow + 1 < len(fields) else "",
                "current": fields[1] if arrow == 2 else oldver,
                "security": False,
            }
        )
    return pkgs


def parse_zypper_lp(text: str) -> list[dict]:
    """`zypper -q lp`（list-patches）：竖线分隔，列序随版本变化，所以按"任一格写着
    security 就是安全补丁"判，不数第几列。
    """
    pkgs: list[dict] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) < 2:
            continue
        if parts[0].lower() in ("name", "category") or set(parts[0]) <= {"-", "="}:
            continue
        pkgs.append(
            {
                "name": parts[0],
                "suite": parts[-1],
                "candidate": "",
                "current": "",
                "security": any(p.lower() == "security" for p in parts),
            }
        )
    return pkgs


_APK_RE = re.compile(r"^(\S+)\s+(\S+)\s*<\s*(\S+)")


def parse_apk_version(text: str) -> list[dict]:
    """`apk version -l '<'`：`已装包 旧版 < 新版`；Alpine 不给分类，安全子集报未知。"""
    pkgs: list[dict] = []
    for line in text.splitlines():
        m = _APK_RE.match(line.strip())
        if not m:
            continue
        pkgs.append(
            {
                "name": m.group(1),
                "suite": "",
                "candidate": m.group(3),
                "current": m.group(2),
                "security": False,
            }
        )
    return pkgs


def commands_for(family: str) -> dict:
    """给管理员复制的那几行命令。面板自己不执行它们，理由见模块 docstring。"""
    table = {
        DEBIAN: {
            "apply_all": "apt update && apt upgrade -y",
            "apply_security": "apt update && unattended-upgrade -d",
            "refresh_index": "apt update",
        },
        RHEL: {"apply_all": "dnf upgrade -y", "apply_security": "dnf upgrade --security -y",
               "refresh_index": "dnf makecache"},
        ARCH: {"apply_all": "pacman -Syu", "apply_security": "pacman -Syu",
               "refresh_index": "pacman -Syy"},
        SUSE: {
            "apply_all": "zypper update -y",
            "apply_security": "zypper patch --category security -y",
            "refresh_index": "zypper refresh",
        },
        ALPINE: {"apply_all": "apk update && apk upgrade", "apply_security": "apk update && apk upgrade",
                 "refresh_index": "apk update"},
    }
    return dict(table.get(family) or {"apply_all": "", "apply_security": "", "refresh_index": ""})


async def _auto_updates(family: str) -> dict:
    units = AUTO_UNITS_BY_FAMILY.get(family)
    if not units:
        return {"state": "unknown", "unit": None}
    for unit in units:
        # 探针预算：`systemctl is-enabled` 本机毫秒级返回，卡住就是 systemd 出问题了。
        # 判据看的是正文而不是退出码：`is-enabled` 对 disabled 单元本来就退 1，
        # 拿"非 0 即跳过"当失败处理，会把一个真的没开自动更新的机器读成"不知道"，
        # 自检那句「且自动安全更新未开启」就此消失。反过来，超时那句正文是命令自己写的，
        # 它既不是 enabled 也不是 disabled，旧代码让它落到循环外的 "disabled"——
        # 于是 systemd 卡住时面板断言"这台机器没开自动安全更新"。现在读不到就 unknown。
        r = await run("systemctl", ["is-enabled", unit], timeout=PROBE_TIMEOUT)
        state = _ENABLED_STATES.get(((r.get("out") or "").strip().splitlines() or [""])[0])
        if r["code"] == 124 or state is None:
            continue
        return {"state": state, "unit": unit}
    return {"state": "unknown", "unit": None}


def _apt_cache_age() -> float | None:
    """apt 索引距上次刷新的小时数：`apt list` 读的是本地索引，索引旧则结论旧。"""
    try:
        entries = [p for p in APT_LISTS.glob("*") if p.is_file()]
        if not entries:
            return None
        newest = max(p.stat().st_mtime for p in entries)
    except OSError:
        return None
    return round((time.time() - newest) / 3600.0, 1)


def _clean_verdict(data: dict, detail: str, auto_note: str) -> dict:
    """「看起来是干净的」这一支的统一出口：本地索引太旧时不许给 pass。

    `apt list --upgradable` 读的是 /var/lib/apt/lists 里的本地索引，面板自己不跑
    `apt update`（那会写盘并和 dpkg 抢锁）。索引是 30 天前的，这句话说的就是 30 天前
    那个世界——CVE 每天在进，绿灯却照常亮。这与 `_probe_error` 守的是同一类假绿，
    所以判据放在一起，页签、CLI 和自检项不会再各写一套阈值。
    """
    cmds = data.get("commands") or {}
    age = data.get("index_age_hours")
    if age is None:
        return {"status": "pass", "detail": detail + auto_note, "fix": ""}
    if age > STALE_TRUST_HOURS:
        return {"status": "warn",
                "detail": f"{detail}，但清单读的是 {age:.0f} 小时前的本地索引，"
                          f"超过 {STALE_TRUST_HOURS:.0f} 小时就不能当结论用" + auto_note,
                "fix": cmds.get("refresh_index") or ""}
    return {"status": "pass",
            "detail": f"{detail}（索引 {age:.0f} 小时前刷新）" + auto_note, "fix": ""}


async def _reboot_verdict(family: str) -> tuple[bool | None, str | None]:
    """「上次更新要不要重启」。Debian 上不能只认 /run/reboot-required。

    那个标记由 update-notifier-common 在 apt 事件里写，服务器镜像默认不装它。旧代码写的是
    `REBOOT_FLAG.exists()`，在这类机器上恒为 False：本页签报「不用重启」，而自检项 `pending`
    同时报「装了新内核，进程还踩着旧映射」——同一台机器两个页签互相打脸。现在两个页签读
    同一个来源（pending_service 的内核包比对），不可能再分歧。判不出就返回 None，不假称 False。
    """
    if family == DEBIAN:
        k = await asyncio.to_thread(pending_service.kernel_reboot_verdict)
        req = k.get("needs_reboot")
        if req is None:
            return None, k.get("reason") or "读不到已装内核列表，重启与否判不出"
        return bool(req), k.get("reason")
    if family == RHEL:
        # 这里不能用 REBOOT_FLAG.exists()：exists() 把 PermissionError 折成 False，
        # 而 False 在这一页的意思从头到尾是"确认不用重启"。/run 的权限被收紧过
        # （或面板降权运行）的机器上，它会稳定报"不用重启"，而真话是"看不了"。
        state, why = path_state(REBOOT_FLAG)
        if state is None:
            return None, f"确认不了 {REBOOT_FLAG}（{why}），重启与否这一轮不作数"
        return bool(state), None
    return None, "该发行版的重启判定不由面板负责"


async def _collect() -> dict:
    osr_text = ""
    osr_unreadable = ""
    # 不能用 OS_RELEASE.exists() 当闸门：exists() 把 PermissionError 折成"不在"，
    # 于是一台 /etc 读不动的机器会被说成"这机器没有 os-release"。三态门禁同批其它处。
    state, why = path_state(OS_RELEASE)
    if state is None:
        osr_unreadable = why
    elif state:
        try:
            osr_text = OS_RELEASE.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            # _stat 过了不等于读得动（ACL / 挂载选项 / 读到一半被换掉），
            # 裸 read_text 在这里抛出去就是 GET /api/updates 直接 500
            osr_unreadable = type(e).__name__
    meta = parse_os_release(osr_text) if osr_text else {}
    available = {c for c in PKG_CMDS if shutil.which(c)}
    family = family_of(meta, available)
    notes: list[str] = []
    pkgs: list[dict] = []
    advisories: list[dict] = []
    security_known = False
    probe_error: str | None = None
    index_age: float | None = None

    if osr_unreadable:
        # 不把"读不到"说成"这机器没有 os-release"：页面至少要让人看见判词是哪一路来的
        notes.append(f"读不到 {OS_RELEASE}（{osr_unreadable}）："
                     f"发行版名与家族这轮只按包管理器命令判，页面上的发行版名可能为空")

    if family == DEBIAN:
        r = await run("apt", ["list", "--upgradable"], timeout=APT_TIMEOUT)
        pkgs = parse_apt_list(r["out"] or "")
        if r["code"] != 0:
            notes.append(f"apt list 失败（退出码 {r['code']}）：{(r['out'] or '').strip()[:160]}")
        probe_error = _probe_error(family, r["code"], pkgs)
        # 探测失败时连「安全更新有几个」都不能假称知道——旧代码在这里照旧置 True，
        # 于是 doctor 拿着 security_count=0 输出「没有待装安全更新」。
        security_known = probe_error is None
        index_age = _apt_cache_age()
        if index_age is not None and index_age > 24:
            notes.append(f"apt 索引已 {int(index_age)} 小时未刷新，结论可能偏旧（面板不代跑 apt update）")
    elif family == RHEL:
        cmd = "dnf" if "dnf" in available else "yum"
        r = await run(cmd, ["-q", "check-update"], timeout=DNF_TIMEOUT)
        # check-update 有待装更新时退出码就是 100，这是约定不是失败。
        pkgs = parse_dnf_check_update(r["out"] or "")
        if r["code"] not in (0, 100):
            notes.append(f"{cmd} check-update 失败（退出码 {r['code']}）：{(r['out'] or '').strip()[:160]}")
        probe_error = _probe_error(family, r["code"], pkgs)
        adv = await run(cmd, ["-q", "updateinfo", "list", "security"], timeout=DNF_TIMEOUT)
        advisories = parse_dnf_updateinfo(adv["out"] or "")[:MAX_ADVISORIES]
        security_known = probe_error is None and (bool(advisories) or any(p["security"] for p in pkgs))
        if probe_error is None and not security_known:
            notes.append(f"{cmd} 没给出安全更新子集，只报待装总数")
    elif family == ARCH:
        r = await run("pacman", ["-Qu"], timeout=DNF_TIMEOUT)
        pkgs = parse_pacman_qu(r["out"] or "")
        probe_error = _probe_error(family, r["code"], pkgs)
    elif family == SUSE:
        r = await run("zypper", ["-q", "lp"], timeout=DNF_TIMEOUT)
        pkgs = parse_zypper_lp(r["out"] or "")
        probe_error = _probe_error(family, r["code"], pkgs)
        security_known = any(p["security"] for p in pkgs)
    elif family == ALPINE:
        r = await run("apk", ["version", "-l", "<"], timeout=DNF_TIMEOUT)
        pkgs = parse_apk_version(r["out"] or "")
        probe_error = _probe_error(family, r["code"], pkgs)
    else:
        notes.append("未识别的发行版或没有受支持的包管理器，不猜更新数量")

    if probe_error:
        notes.append(f"{probe_error}：这一轮读不到待装清单，页面上的数量按「读不到」处理，不是 0")

    security_pkgs = [p for p in pkgs if p["security"]]
    if not security_known:
        security_count = None
    elif family == RHEL and advisories:
        security_count = len(advisories)
    else:
        security_count = len(security_pkgs)

    reboot, reboot_reason = await _reboot_verdict(family)

    return {
        "ok": True,
        "family": family,
        "distro": meta.get("PRETTY_NAME") or "",
        "supported": family != UNKNOWN,
        # 读不到时是 None 而不是 0：0 会被 CLI 与网页渲染成「待装 0 个」，那就是假绿。
        "pending_total": None if probe_error else len(pkgs),
        "probe_error": probe_error,
        # 只有 Debian 走 apt 索引，其余族给 None：自检项不会拿一个不存在的年龄去判新旧。
        "index_age_hours": index_age,
        # 阈值只在这里算一次：网页拿布尔值着色，不许自己抄一份 168。
        "index_stale": index_age is not None and index_age > STALE_TRUST_HOURS,
        "security_count": security_count,
        "security_known": security_known,
        "packages": pkgs[:MAX_PKGS],
        "advisories": advisories,
        "reboot_required": reboot,
        "reboot_reason": reboot_reason,
        "auto_updates": await _auto_updates(family),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commands": commands_for(family),
        "notes": notes,
    }


async def detect(refresh: bool = False) -> dict:
    """带 TTL 的探测入口；并发与连点合并成一轮 `apt list`（见 app/probe.py）。"""
    return await probe.read(_cache, _LOCK, _collect, refresh=refresh, ttl=CACHE_TTL)


def doctor_item(data: dict) -> dict:
    """把探测结果折成自检项。判据放服务层，CLI 与网页才不会各说一套。"""
    if not data.get("supported"):
        return {"status": "warn", "detail": "未能识别发行版/包管理器，无法判断待装更新", "fix": ""}
    cmds = data.get("commands") or {}
    err = data.get("probe_error")
    if err:
        # 探测失败绝不能走到下面的 pass：那是把「我不知道」打印成「这台机器是干净的」。
        auto = (data.get("auto_updates") or {}).get("state")
        auto_note = "，且自动安全更新未开启" if auto == "disabled" else ""
        return {"status": "warn",
                "detail": f"待装更新读不到（{err}），这一条不等于「没有安全更新」"
                          + ("；刷新索引后面板就能给出数量" if cmds.get("refresh_index") else "")
                          + auto_note,
                "fix": cmds.get("refresh_index") or ""}
    sec = data.get("security_count")
    total = int(data.get("pending_total") or 0)
    auto = (data.get("auto_updates") or {}).get("state")
    auto_note = "，且自动安全更新未开启" if auto == "disabled" else ""
    fix = cmds.get("apply_security") or ""
    if sec is None:
        if total:
            return {"status": "warn",
                    "detail": f"待装更新 {total} 个（该发行版判不出安全更新子集）" + auto_note,
                    "fix": fix}
        return _clean_verdict(data, "没有待装更新（该发行版判不出安全更新子集）", auto_note)
    if sec:
        return {"status": "warn", "detail": f"{sec} 个待装安全更新（共 {total} 个待装）" + auto_note, "fix": fix}
    detail = "没有待装安全更新"
    if total:
        detail += f"（另有 {total} 个功能更新）"
    if data.get("reboot_required"):
        return {"status": "warn", "detail": detail + "，但上次更新要求重启才生效", "fix": "systemctl reboot"}
    return _clean_verdict(data, detail, auto_note)
