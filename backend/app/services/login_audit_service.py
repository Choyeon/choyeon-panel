"""登录安全对账：谁能进这台机器、用什么方式进、过去一天有没有人在试着进。

为什么值得做：面板已经摆出了 ufw 规则和监听端口，但 SSH 才是多数机器真正的入口。
"22 对外开着"本身不危险，危险的是它后面写着"允许 root + 允许口令"，而门外一天有
几百次撞库。这些事实本来就躺在 sshd -T、/etc/passwd、~/.ssh/authorized_keys 和
journal 里，只是没有一个页面把它们拼成一句能照着做的事。

严格只读，而且比端口那一层更要紧：SSH 配置写错就是把人——包括正在看这个页面的
这个人——锁在门外。面板不写 sshd_config、不 reload sshd、不碰 authorized_keys，
只给可复制的命令。

如实承认的局限写进 notes，不装全知：
① 非 root 时读不到 /etc/shadow，也读不到别的用户的 authorized_keys；
② 拿不到 `sshd -T` 时退化成 sshd_config 原文，Include 与 Match 块都没展开过，
   列出来的"值"可能是文件里的值而不是生效值——这一条会显式标出来。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import shutil
import stat
from datetime import UTC, datetime

from .. import config, probe, security
from .. import database as dbm
from ..severity import SEV_ORDER
from ..util import name_list, run, tallied
from . import exposure_service, systemd_ops

CACHE_TTL = 300.0
SSHD_TIMEOUT = 10.0
JOURNAL_TIMEOUT = 25.0
MAX_KEYS = 60  # 单个 authorized_keys 最多逐条列这么多行，再多只报总数
MAX_FILES = 64
JOURNAL_LINES = 50000
# 认证日志这一段覆盖多长，说法只有一份：`_attempts` 给出 window_label / window_note，
# doctor 判词、CLI 与登录安全页签都从这里读。`-n` 限的是行数不是时间——窗口里日志超过
# 上限时，读到的只是 24h 的最后一段，把次数写成「过去 24 小时 N 次」就是拿面板自己的
# 读取上限冒充一次测量（批次85）。
WINDOW_FULL = "过去 24 小时"
# journalctl -o short-iso 的行首是 2026-10-30T07:57:09+0800；取到分钟够人核对日期。
_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})")
# 失败来源与成功来源各列前几名。两处用同一个数：只列失败的 Top5 而看不到"这个人其实
# 登录成功过"，封禁那条路上的提醒就无从谈起。
TOP_IPS = 5
# 面板自己那扇门 24h 内的失败登录到达这个数，就把"没人开两步验证"升成要紧：
# 有人反复试口令时，第二因子是唯一还能挡住的层；没人在试则只是加固建议。
PANEL_BRUTE_RISK = 50

NON_LOGIN_SHELLS = (
    "/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false",
    "/bin/sync", "/sbin/shutdown", "/sbin/halt",
)
SUDO_GROUPS = ("sudo", "wheel")
GUARD_UNITS = ("fail2ban", "sshguard")
ALGO_PREFIX = ("ssh-", "ecdsa-", "sk-", "x509-")

_FAILED_RE = re.compile(
    r"Failed (?:password|publickey|none)|Invalid user"
    r"|Connection (?:closed|reset) by (?:authenticating|invalid) user"
)
_ACCEPTED_RE = re.compile(r"Accepted (?:password|publickey|keyboard-interactive(?:/published)?|none)")
# 爆破行有两种写法：`... from <ip> port`，和 preauth 就断掉的
# `Connection closed by authenticating user root <ip> port`（本机这种占了失败行的四成）。
# 只认 from 会让这批来源从 IP 排行里整体消失，判词就成了"没几个人在打"。
_IP_RE = re.compile(
    r"\b(?:from|by (?:authenticating|invalid) user \S+) "
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]{3,45})"
)

_LOCK = asyncio.Lock()
_cache: dict = probe.new_cache()


def _read_text(path: str, limit: int = 200000) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None


# 账号表按行数封顶读。LDAP/FreeIPA 镜像出来的 /etc/passwd 与 /etc/shadow 能到几万行，
# 一次全读进内存不现实；但"读满就停"必须说出来——空口令账号或多余 uid=0 若排在
# 上限之后，原来这一轮会报成「确认没有」，而那正是最不该冒充核对过的一句话。
ACCOUNTS_MAX_LINES = 4000
# 行数上限的字符兜底：正常 shadow 一行约 60 字符，先触到的会是行数。但账号表里也可以有
# 一条很长的组（成员几百个），所以字符这道线真的可能先到——`_read_lines` 把两种触顶都算
# "没读到底"，不假设行数一定赢。
ACCOUNTS_MAX_CHARS = 2_000_000


def _read_lines(path: str, limit: int = ACCOUNTS_MAX_LINES) -> tuple[list[str] | None, bool]:
    """(行, 这一轮有没有读到文件底)。读不动是 `(None, False)`——那是"没读到"，不是"没有"。

    盘只开一次：字符上限仍走 `_read_text` 那一个读者（权限判定与测试替身都只认它），
    这里只补上"没读到底"这一层它没说的账。两道线都算：`read(limit)` 停在字符上时
    行数还没触顶，只看 `len(lines) > limit` 会把这一种截断漏成"扫全了"，
    而截断正好落在某一条账号的中间。
    """
    text = _read_text(path, ACCOUNTS_MAX_CHARS)
    if text is None:
        return None, False
    lines = text.splitlines()
    return lines[:limit], len(lines) > limit or len(text) >= ACCOUNTS_MAX_CHARS


def _parse_effective(text: str) -> dict[str, list[str]]:
    """`sshd -T` 每行 `key value`；listenaddress 会重复出现，值本身可能带空格。"""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        # 注释行和"只有键没有值"的行都不算读到一个设置，否则 "# X11Forwarding no"
        # 会变成名为 "#" 的配置，让人以为核对过它。
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(" ")
        if not sep or not value:
            continue
        out.setdefault(key.strip().lower(), []).append(value.strip())
    return out


def _parse_config(text: str) -> dict[str, list[str]]:
    """退路：直接读 sshd_config。Match 块是给特定条件用的，这里不评估也不采信。"""
    vals: dict[str, list[str]] = {}
    in_match = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("match "):
            in_match = True
            continue
        if in_match:
            continue
        key, sep, value = line.partition(" ")
        if not sep:
            continue
        vals.setdefault(key.strip().lower(), []).append(value.strip())
    return vals


async def _sshd_settings() -> dict:
    """生效配置优先：`sshd -T` 由 sshd 自己算，Include 进来的和内置默认都在里面。"""
    if not shutil.which("sshd"):
        return {
            "source": "missing", "effective": False, "values": {},
            "notes": ["这台机器上没有 sshd（或不在 PATH），SSH 设置无从判起"],
        }
    r = await run("sshd", ["-T"], timeout=SSHD_TIMEOUT)
    if r["code"] == 0 and r["out"]:
        return {"source": "sshd -T", "effective": True, "values": _parse_effective(r["out"]), "notes": []}
    text = _read_text("/etc/ssh/sshd_config")
    if text is None:
        err = (r["out"] or "")[:120] or "读不到 /etc/ssh/sshd_config"
        return {"source": "unreadable", "effective": False, "values": {},
                "notes": [f"sshd -T 没成功且配置文件读不到：{err}（非 root 常见）"]}
    return {
        "source": "sshd_config", "effective": False, "values": _parse_config(text),
        "notes": ["列的是 sshd_config 原文而不是 sshd -T：Include 进来的文件和 Match 条件块都没展开，"
                  "这里的值是文件里的值，不一定是生效值（sshd -T 需要 root）。"],
    }


def _val(values: dict, key: str) -> str:
    got = values.get(key)
    return got[-1] if got else ""


def _yes(values: dict, key: str) -> bool:
    return _val(values, key) == "yes"


def _setting_rows(values: dict, reachable: bool, guard_status: str) -> list[dict]:
    """把关心的几项排成表：判词在这一层算完，前端只负责显示。"""
    pw = _yes(values, "passwordauthentication")
    root_yes = _val(values, "permitrootlogin") == "yes"
    tries = _val(values, "maxauthtries")
    tries_bad = tries.isdigit() and int(tries) > 6
    # 三件事叠在一起才是最坏组合：口令能试、外面碰得到、没人管爆破。
    # 「没人管」只认核对到的那两种状态（没在跑 / 压根没装）：防护正在起停或 systemctl 没回话时
    # 升级成要紧，等于把一句没验证的话写进红字里。
    no_guard = guard_status in ("unguarded", "absent")
    pw_exposed = pw and reachable
    root_flag = "risk" if pw_exposed and root_yes else "note" if root_yes else ""
    pw_flag = "risk" if pw_exposed and no_guard else "warn" if pw_exposed else ""
    kbd_flag = "warn" if pw and _yes(values, "kbdinteractiveauthentication") else ""
    empty_pw_yes = _yes(values, "permitemptypasswords")
    x11_yes = _yes(values, "x11forwarding")
    gateway_yes = _yes(values, "gatewayports")
    pam_off = bool(values.get("usepam")) and not _yes(values, "usepam")
    SPEC = [
        ("port", "监听端口", "", ""),
        ("listenaddress", "监听地址", "", ""),
        ("permitrootlogin", "root 直接登录", root_flag,
         "把 PermitRootLogin 设成 prohibit-password，root 只允许钥匙登录"),
        ("passwordauthentication", "口令认证", pw_flag,
         "改用 SSH 钥匙后设 PasswordAuthentication no（先确认自己的钥匙能登进来）"),
        ("kbdinteractiveauthentication", "键盘交互认证", kbd_flag,
         "它走 PAM 问的是同一套口令，设 KbdInteractiveAuthentication no"),
        ("pubkeyauthentication", "公钥认证", "", ""),
        ("permitemptypasswords", "允许空口令", "risk" if empty_pw_yes else "",
         "设 PermitEmptyPasswords no"),
        ("maxauthtries", "单次连接允许尝试次数", "note" if tries_bad else "",
         "设 MaxAuthTries 3~6，让每次撞库连接更贵"),
        ("authenticationmethods", "认证方式", "", ""),
        ("usepam", "PAM", "note" if pam_off else "",
         "关掉 PAM 后 faillock 之类策略不再生效，一般应保持 yes"),
        ("x11forwarding", "X11 转发", "note" if x11_yes else "",
         "不需要图形转发就设 X11Forwarding no"),
        ("gatewayports", "远程转发是否对全网开放", "warn" if gateway_yes else "",
         "设 GatewayPorts no，否则一条远程端口转发就能把端口开给整张网"),
        ("clientaliveinterval", "空闲断连间隔", "", ""),
        ("logingracetime", "登录宽限", "", ""),
    ]
    rows: list[dict] = []
    for key, label, flag, fix in SPEC:
        raw = values.get(key)
        if not raw:
            continue
        # 建议只在真的有问题时才给：值本来就合规还附一句"设 X no"，读起来像没检查过。
        rows.append({"key": key, "label": label, "value": "、".join(raw),
                     "flag": flag, "fix": fix if flag else ""})
    # 白名单：AllowUsers/AllowGroups 都没设，意味着任何有效账号都是一个可试的用户名
    if not values.get("allowusers") and not values.get("allowgroups"):
        rows.append({"key": "allowusers", "label": "允许登录的用户白名单", "value": "未设置", "flag": "note",
                     "fix": "AllowUsers <你的账号>，把可试的用户名从整台机器缩到几个人"})
    else:
        got = "、".join((values.get("allowusers") or []) + (values.get("allowgroups") or []))
        rows.append({"key": "allowusers", "label": "允许登录的用户白名单", "value": got, "flag": "", "fix": ""})
    return rows


def _is_algo(token: str) -> bool:
    return token.startswith(ALGO_PREFIX)


def _blob_meta(algo: str, b64: str) -> tuple[str | None, int | None, bool]:
    """钥匙指纹（SHA256:…）、位数、以及是否强度过低：纯解析，不 fork ssh-keygen。"""
    try:
        blob = base64.b64decode(b64 + "=" * (-len(b64) % 4))
    except (binascii.Error, ValueError):
        return None, None, False
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    parts: list[bytes] = []
    i = 0
    while i + 4 <= len(blob) and len(parts) < 4:
        n = int.from_bytes(blob[i:i + 4], "big")
        i += 4
        if n <= 0 or i + n > len(blob):
            break
        parts.append(blob[i:i + n])
        i += n
    bits: int | None = None
    if algo.startswith("ssh-rsa") or algo.startswith("rsa-sha2-"):
        if len(parts) >= 3:
            modulus = parts[2]
            if modulus and modulus[0] == 0:
                modulus = modulus[1:]
            bits = len(modulus) * 8
    elif algo.startswith("ecdsa-sha2-"):
        curve = parts[1].decode(errors="replace") if len(parts) >= 2 else ""
        bits = {"nistp256": 256, "nistp384": 384, "nistp521": 521}.get(curve)
    elif algo.startswith("ssh-ed25519") or algo.startswith("sk-ssh-ed25519"):
        bits = 256
    elif algo == "ssh-dss":
        bits = 1024
    weak = algo == "ssh-dss" or (algo.startswith(("ssh-rsa", "rsa-sha2-")) and bits is not None and bits < 2048)
    return fp, bits, weak


def _passwd_users() -> tuple[list[dict], bool]:
    """(解析出来的账号行, 这份表有没有读到底)。

    那个标志不是装饰：调用方要用它决定"这台机器没有多余 uid=0"这类否定句能不能说出口。
    """
    lines, truncated = _read_lines("/etc/passwd")
    if lines is None:
        return [], False
    out = []
    for line in lines:
        f = line.split(":")
        if len(f) < 7:
            continue
        shell = f[6]
        out.append({
            "name": f[0],
            "uid": int(f[2]) if f[2].isdigit() else -1,
            "home": f[5],
            "shell": shell,
            "login": bool(shell) and shell not in NON_LOGIN_SHELLS,
        })
    return out, truncated


def _authorized_keys(users: list[dict], as_root: bool) -> dict:
    """逐个用户目录读 authorized_keys：只列指纹不列公钥本体，且不跟随符号链接。"""
    name_by_uid = {u["uid"]: u["name"] for u in users}
    files: list[dict] = []
    scanned = 0
    me = os.getuid()
    for u in users:
        home = u["home"]
        if not home or not os.path.isdir(home):
            continue
        # 非 root 只可能读到自己（和 root 跑面板时的全部）
        if not as_root and u["uid"] != me:
            continue
        path = os.path.join(home, ".ssh", "authorized_keys")
        try:
            st = os.lstat(path)
        except OSError:
            continue
        scanned += 1
        entry = {
            "user": u["name"], "path": path,
            "mode": stat.filemode(st.st_mode)[-9:],
            "owner": name_by_uid.get(st.st_uid, str(st.st_uid)),
            "modified_at": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
            "symlink": stat.S_ISLNK(st.st_mode),
            "count": 0, "keys": [], "flags": [],
        }
        if entry["symlink"]:
            entry["flags"].append("是指向别处的符号链接，面板没有读它指向的内容")
            files.append(entry)
            continue
        if st.st_mode & 0o022:
            entry["flags"].append("同组或其他人可写：任何本地用户都能往里加钥匙")
        if st.st_uid not in (0, u["uid"]):
            entry["flags"].append(f"属主是 {name_by_uid.get(st.st_uid, st.st_uid)}，不是这个用户")
        text = _read_text(path, 80000) or ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entry["count"] += 1
            if line.startswith("@"):
                entry["flags"].append("含 @cert-authority / @revoked 指令行，面板不解析其约束")
                continue
            parts = line.split()
            opts, comment = "", ""
            if len(parts) >= 2 and _is_algo(parts[0]):
                algo, b64, comment = parts[0], parts[1], " ".join(parts[2:])
            elif len(parts) >= 3 and _is_algo(parts[1]):
                opts, algo, b64, comment = parts[0], parts[1], parts[2], " ".join(parts[3:])
            else:
                if len(entry["keys"]) < MAX_KEYS:
                    entry["keys"].append({"algo": "?", "bits": None, "comment": line[:48], "fp": None,
                                          "weak": False, "options": "", "unparsed": True})
                continue
            fp, bits, weak = _blob_meta(algo, b64)
            if len(entry["keys"]) < MAX_KEYS:
                entry["keys"].append({"algo": algo, "bits": bits, "comment": comment, "fp": fp,
                                      "weak": weak, "options": opts, "unparsed": not bool(fp)})
        files.append(entry)
        if len(files) >= MAX_FILES:
            break
    return {
        "files": files,
        "scanned": scanned,
        "total_keys": sum(f["count"] for f in files),
        "truncated": any(f["count"] > len(f["keys"]) for f in files),
    }


def _accounts(users: list[dict], passwd_partial: bool = False) -> dict:
    """空口令、多余 uid=0、有登录 shell 的账号、sudo/wheel 成员。

    `complete` 说的是"这三份表都从头看到尾了"：任何一份读满上限或被截，
    下面的否定结论（没有空口令、没有多余 uid=0）就只能对已扫过的那一段负责。
    """
    login_users = [u["name"] for u in users if u["login"]]
    uid0_extra = [u["name"] for u in users if u["uid"] == 0 and u["name"] != "root"]
    empty: list[str] = []
    notes: list[str] = []
    shadow_lines, shadow_partial = _read_lines("/etc/shadow")
    if shadow_lines is None:
        notes.append("读不到 /etc/shadow（需要 root），空口令账号这一项没核对成")
    else:
        for line in shadow_lines:
            f = line.split(":")
            if len(f) >= 2 and f[1] == "":
                empty.append(f[0])
    group_lines, group_partial = _read_lines("/etc/group")
    groups: dict[str, list[str]] = {}
    for line in group_lines or []:
        f = line.split(":")
        if len(f) >= 4 and f[0] in SUDO_GROUPS:
            groups[f[0]] = [x for x in f[3].split(",") if x]
    partial = [n for n, hit in (("/etc/passwd", passwd_partial), ("/etc/shadow", shadow_partial),
                                ("/etc/group", group_partial)) if hit]
    if partial:
        notes.append(f"账号表这一轮只读到前 {ACCOUNTS_MAX_LINES} 行（或 {ACCOUNTS_MAX_CHARS} 字符），"
                     f"{'、'.join(partial)} 比这更长：后面的账号本轮没有核对，"
                     "「没有空口令账号 / 没有多余 uid=0」只对已扫过的这一段成立")
    return {
        "login_shells": login_users,
        "login_count": len(login_users),
        "uid0_extra": uid0_extra,
        "empty_password": empty,
        "shadow_unreadable": shadow_lines is None,
        "sudo_groups": groups,
        "scan_partial": partial,
        "complete": not partial,
        "notes": notes,
    }


async def _guard_status() -> dict:
    """fail2ban / sshguard 这类"撞库有没有人管"的东西，只看服务此刻在不在跑。

    判词从 systemd_ops.unit_states 取（批次㉜定下的唯一出处），换掉原来的
    `systemctl is-active a b` + 按位置切词，有两个原因：
    ① is-active 只会回"0 / 非 0"，正在启动、单元不存在、dbus 查失败三种情况在退出码上长一样，
       而这一页对三种情况要说的话不同（前者别急着说没防护，中者是这台机器压根没装，
       后者是这一项没核对成，不能写成"都没在跑"）；
    ② "装没装"原来用 `shutil.which(unit 名)` 判断，而 fail2ban 装的可执行文件叫
       fail2ban-client / fail2ban-server，包名根本不是命令名——装了也报"没装"。
       systemd 的 LoadState=not-found 才是"这台机器上没有这个单元"的权威说法。
    """
    if not systemd_ops.has_systemd():
        payload = {"units": [], "status": "no-systemd",
                   "note": "本机没有可管理的 systemd，「爆破防护在不在跑」这一项没有核对成"}
        payload["phrase"] = _guard_phrase(payload)
        return payload
    states = await systemd_ops.unit_states(list(GUARD_UNITS))
    units = []
    for name in GUARD_UNITS:
        st = states.get(name) or dict(systemd_ops.UNKNOWN_STATE)
        units.append({
            "name": name,
            "state": st["state"],
            "active": st["active"],
            "busy": st["busy"],
            "missing": st["missing"],
            "unknown": st["state"] == "unknown",
            "desc": _unit_desc(st),
        })
    status = _guard_verdict(units)
    note = ""
    if status == "unknown":
        note = (f"systemctl 这次没回话，{' / '.join(GUARD_UNITS)} 是否在跑没有核对成——"
                "这一项按「没查到」处理，不算成「没有防护」。")
    elif status == "pending":
        note = "防护单元正在启动或停止中，这一轮没有把它算成「正在跑的防护」。"
    return {"units": units, "status": status, "note": note}


def _guard_verdict(units: list) -> str:
    """guarded / pending / unguarded / absent / unknown：判词要说的是哪一种，只在这一处定。

    只有"每一个候选都没核对到状态"才算 unknown。反过来，只要有一个单元确认没在跑、
    且没有任何一个在跑，"没有防护在跑"这句话就是核对过的，不必因为另一个单元查失败而闭嘴。
    """
    if any(u["active"] for u in units):
        return "guarded"
    if any(u["busy"] for u in units):  # activating / deactivating
        return "pending"
    known = [u for u in units if not u["unknown"]]
    if not known:
        return "unknown"
    return "absent" if all(u["missing"] for u in known) else "unguarded"


def _unit_desc(st: dict) -> str:
    """一个防护单元的说人话状态。判词在这里成型，CLI 与前端只负责摆出来。

    先问"查没查到"再问"在不在跑"：systemctl 没回话时 ActiveState 是空串，
    照状态表翻译就成了"没在跑"——那是把一次探测失败写成了一句安全结论。
    """
    if st["state"] == "unknown":
        return "没查到"
    if st["missing"]:
        return "没有这个单元"
    return {
        "active": "在跑", "reloading": "在跑（重载中）", "activating": "正在启动",
        "deactivating": "正在停止", "failed": "上次启动失败", "inactive": "没在跑",
    }.get(st["state"], st["state"])


def _guard_phrase(guard: dict) -> str:
    """判词里那句「爆破防护现在怎么样」。只说真正核对到的那一部分。"""
    units = [u for u in guard["units"] if not u["unknown"]]
    status = guard["status"]
    on = [u["name"] for u in units if u["active"]]
    if status == "no-systemd":
        return "本机没有可管理的 systemd"
    if on:
        return "、".join(on) + " 正在跑"
    if status == "pending":
        return "、".join(f"{u['name']}（{u['desc']}）" for u in units if u["busy"]) + "，正在起停"
    if status == "absent":
        return "systemd 里没有 " + " / ".join(u["name"] for u in units) + " 单元（这台机器没装）"
    if status == "unguarded":
        tail = " 都没在跑" if len(units) > 1 else " 没在跑"
        return "、".join(u["name"] for u in units) + tail
    return "这一项没核对成"


def _no_attempts(note: str) -> dict:
    """读不到日志时如实返回"这一项没核对成"，不给出 0 次这种看着像好消息的数。"""
    return {"ok": False, "failed": 0, "distinct_ips": 0, "top": [], "accepted": [],
            "accepted_ips": [], "note": note, "truncated": False,
            "window_label": WINDOW_FULL, "window_note": ""}


async def attempts() -> dict:
    """过去 24h 的 sshd 认证尝试汇总（公开入口）。

    封禁那一层需要知道某个地址有没有成功登录过，而它不该去调下划线函数：
    这份数据的外形（ok / top / accepted_ips）就是这一层对外的契约。
    """
    return await _attempts()


def _first_seen(lines: list[str]) -> str:
    """这一轮读到的第一行是什么时候（`journalctl -o short-iso` 的时间前缀）。

    只为把「最后 5 万行」翻成人能核对的时间跨度。行首不是 ISO 时间（老格式日志、
    测试里喂的裸日志行）就回空串：判词照旧说「这一轮只读到最后 N 行」，不编日期。
    """
    for line in lines:
        m = _STAMP_RE.match(line.strip())
        if m:
            return f"{m.group(1)} {m.group(2)}"
    return ""


def _cap(attempts: dict) -> int:
    """这一轮实际读到的行数上限，出自它自己的载荷。

    `window_label` 会在别的时刻被调用（doctor、CLI、封禁提醒），那时模块常量可能
    已经不是当初读日志用的那个数（测试、以后调参都算）——说法必须跟着那次读取走。
    """
    try:
        return int(attempts.get("line_limit") or JOURNAL_LINES)
    except (TypeError, ValueError):
        return JOURNAL_LINES


def window_label(attempts: dict) -> str:
    """「这些次数覆盖多久」的唯一说法。未截断时就是过去 24 小时。"""
    if not attempts.get("ok") or not attempts.get("truncated"):
        return WINDOW_FULL
    since = attempts.get("covered_from") or ""
    return f"这一轮读到的最后 {_cap(attempts)} 行日志里" + (f"（自 {since} 起）" if since else "")


def window_note(attempts: dict) -> str:
    """截断时的一句完整说明；未截断时为空，读的人不必自己拼。"""
    if not attempts.get("ok") or not attempts.get("truncated"):
        return ""
    cap = _cap(attempts)
    since = attempts.get("covered_from") or ""
    span = f"（从 {since} 起，覆盖不足 24 小时）" if since else ""
    return (f"24 小时窗口的 sshd 日志超过这一轮读取上限（{cap} 行），"
            f"只数到最后 {cap} 行{span}：失败次数、来源数与此处排行都是这一段的下限，"
            "不是整窗口的数。全量请自己跑 journalctl --since '24 hours ago' 数一遍。")


async def _attempts() -> dict:
    """过去 24h 的 sshd 认证日志：失败次数、来源 IP 排行、成功登录各用了什么方式。"""
    if not shutil.which("journalctl"):
        return _no_attempts("没有 journalctl，统计不到登录尝试")
    r = await run(
        "journalctl",
        ["-u", "ssh", "-u", "sshd", "-u", "ssh.service",
         "--since", "24 hours ago", "-n", str(JOURNAL_LINES), "--no-pager", "-q",
         "-o", "short-iso"],
        timeout=JOURNAL_TIMEOUT,
    )
    out = r["out"] or ""
    if r["code"] not in (0, 1):
        return _no_attempts(f"journalctl 退出码 {r['code']}，这段窗口没读到")
    if r["code"] == 1 and not out:
        return _no_attempts("这台机器上没有 ssh 相关的 journal 单元（sshd 日志可能写在别处）")
    lines = out.splitlines()
    # 读满上限就是窗口被切过：`-n` 取的是 24h 里的最后 N 行，不是"24h 里只有这些"。
    cut = len(lines) >= JOURNAL_LINES
    seen = _first_seen(lines) if cut else ""
    failed, ips, accepted, ok_ips = 0, {}, {}, {}
    for line in lines:
        if _FAILED_RE.search(line):
            failed += 1
            m = _IP_RE.search(line)
            if m:
                ips[m.group("ip")] = ips.get(m.group("ip"), 0) + 1
        else:
            m = _ACCEPTED_RE.search(line)
            if m:
                method = m.group(0).split()[1].split("/")[0]
                accepted[method] = accepted.get(method, 0) + 1
                im = _IP_RE.search(line)
                if im:
                    ok_ips[im.group("ip")] = ok_ips.get(im.group("ip"), 0) + 1
    top = sorted(ips.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_IPS]
    tops_ok = sorted(ok_ips.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_IPS]
    payload = {
        "ok": True, "failed": failed, "distinct_ips": len(ips),
        "top": [{"ip": ip, "count": c} for ip, c in top],
        "accepted": [{"method": k, "count": v} for k, v in sorted(accepted.items(), key=lambda kv: (-kv[1], kv[0]))],
        "accepted_ips": [{"ip": ip, "count": c} for ip, c in tops_ok],
        "note": "", "truncated": cut, "covered_from": seen, "line_limit": JOURNAL_LINES,
    }
    payload["window_label"] = window_label(payload)
    payload["window_note"] = window_note(payload)
    return payload


def _panel_2fa() -> dict:
    """面板自己那扇门的第二因子覆盖面。

    为什么算在这一层：sshd 那侧再紧，面板账号也是同一个 root 的入口——网页终端发的是
    root shell，口令被猜到就等于这台机器的 root。这里只统计"有几个账号、几个开了"，
    外加过去 24h 有多少次失败的面板登录（撞门的证据），密钥与口令哈希一律不出库。
    """
    try:
        rows = dbm.query("SELECT username,totp_secret<>'' AS totp FROM users")
        got = dbm.query_one(
            "SELECT COUNT(*) AS c FROM audit WHERE action IN ({})"
            " AND created_at >= datetime('now','-1 day')".format(",".join("?" * len(security.LOGIN_FAILED_ACTIONS))),
            security.LOGIN_FAILED_ACTIONS,
        )
    except Exception as e:  # noqa: BLE001 - 账号表读不到不该把整个 SSH 对账拖没
        return {"ok": False, "accounts": 0, "with_totp": 0, "recent_failed": 0,
                "note": f"读不到面板账号/审计表，两步验证覆盖面这一项没核对成：{e}"}
    return {"ok": True, "accounts": len(rows), "with_totp": sum(1 for r in rows if r["totp"]),
            "recent_failed": int((got or {}).get("c") or 0), "note": ""}


def _ssh_exposure(values: dict, ports_data: dict | None) -> dict:
    """和端口暴露面那一层对个账：sshd 在听的这个端口，外面到底进得来吗。"""
    port = _val(values, "port")
    port_n = int(port) if port.isdigit() else 22
    row = next((p for p in (ports_data or {}).get("ports", []) if p["port"] == port_n), None)
    return {
        "port": port_n,
        "reachable": bool(row["reachable"]) if row else None,
        "verdict": row["verdict"] if row else "",
        "reason": row["reason"] if row else "端口表里没有这个端口（可能 sshd 没在跑）",
    }


def _findings(ssh: dict, keys: dict, accounts: dict, attempts: dict, guard: dict, exposure: dict,
              panel: dict | None = None) -> list[dict]:
    out: list[dict] = []
    vals = ssh["values"]
    pw = _yes(vals, "passwordauthentication")
    # reachable 为 None（端口表里没找到）时不能当成"安全"，按可达判。
    reachable = exposure["reachable"] is not False
    guarded = guard["status"] == "guarded"
    # 「没有防护」这句话只在真核对到时才说；正在起停或 systemctl 没回话时不产出这条判断，
    # 由 notes 如实说明这一项没核对成。
    no_guard = guard["status"] in ("unguarded", "absent")
    failed = attempts["failed"] if attempts["ok"] else 0
    # 「这些次数覆盖多久」只有 window_label 一个出处：读满行数上限时它就不是 24 小时，
    # 判词照旧写"过去 24 小时"等于拿面板自己的读取上限冒充一次测量。
    wl = window_label(attempts)
    wn = window_note(attempts)

    if accounts["empty_password"]:
        out.append({
            "id": "empty-password", "severity": "risk",
            "title": f"{len(accounts['empty_password'])} 个账号是空口令",
            "detail": name_list(accounts["empty_password"], 10) + "。这些账号只要能连上服务就能直接登进去。",
            "fix": "passwd <账号> 立刻设口令，不该存在的就锁掉：usermod -L <账号>",
        })
    if accounts["uid0_extra"]:
        out.append({
            "id": "uid0", "severity": "risk",
            "title": "除 root 之外还有 uid=0 的账号：" + "、".join(accounts["uid0_extra"]),
            "detail": "uid 0 就等于 root，这类账号通常不是装系统时装出来的，应当按入侵痕迹处理。",
            "fix": "确认来源后删掉：userdel -r <账号>；顺带查它的家目录和 authorized_keys",
        })
    if pw and _val(vals, "permitrootlogin") == "yes" and reachable:
        out.append({
            "id": "root-password", "severity": "risk",
            "title": "sshd 同时允许 root 登录和口令认证，且端口对外可达",
            "detail": "这是撞库最省事的一组组合：用户名固定是 root，只需要猜口令。"
                      + (f"{wl}已有 {failed} 次失败认证。" if attempts["ok"] else ""),
            "fix": "先确认自己的钥匙能登进来，再把 PermitRootLogin 改成 prohibit-password",
        })
    elif pw and reachable:
        out.append({
            "id": "password-auth", "severity": "warn",
            "title": "sshd 允许口令认证且端口对外可达",
            "detail": "root 直登已经关了，但普通账号的口令仍是可猜目标。"
                      + (f"{wl} {failed} 次失败认证。" if attempts["ok"] else ""),
            "fix": "改用 SSH 钥匙后设 PasswordAuthentication no（关之前拿另一台机器验证钥匙能登进来）",
        })
    if pw and no_guard:
        phrase = _guard_phrase(guard)
        out.append({
            "id": "no-guard",
            "severity": "risk" if failed >= 100 else "warn",
            "title": "口令认证开着，却没有正在运行的爆破防护",
            "detail": (f"{phrase}，{wl} {failed} 次失败认证、来自 "
                       f"{attempts['distinct_ips']} 个地址，全靠 sshd 自己扛。")
                      if attempts["ok"] else f"{phrase}，而这段窗口又读不到 sshd 日志。",
            "fix": "二选一：装上并启用 fail2ban 的 sshd jail，或者干脆关掉口令认证（后者更彻底）",
        })
    if _yes(vals, "kbdinteractiveauthentication") and pw:
        out.append({
            "id": "kbdinteractive", "severity": "warn",
            "title": "键盘交互认证（KbdInteractiveAuthentication）开着",
            "detail": "它走 PAM 问的还是同一套口令；只关 PasswordAuthentication 而留着它，等于关了一扇门又留一扇窗。",
            "fix": "在 sshd_config 里设 KbdInteractiveAuthentication no",
        })
    if _yes(vals, "permitemptypasswords"):
        out.append({
            "id": "empty-pw-config", "severity": "risk",
            "title": "sshd 配置里 PermitEmptyPasswords 是 yes",
            "detail": "只要有任何账号口令为空，就能免口令登进去。",
            "fix": "设 PermitEmptyPasswords no 并 reload sshd（先确认没有空口令账号）",
        })
    if ssh["source"] != "sshd -T":
        out.append({
            "id": "config-source", "severity": "note",
            "title": "这一轮读的不是生效配置",
            "detail": "；".join(ssh["notes"]) or "Include 与 Match 块未展开。",
            "fix": "用 root 执行 sshd -T 看真正的生效值",
        })
    weak = [f"{f['user']}：{k['comment'] or k['fp'] or '?'}" for f in keys["files"] for k in f["keys"] if k["weak"]]
    if weak:
        out.append({
            "id": "weak-key", "severity": "warn",
            "title": f"{len(weak)} 把已授权钥匙强度过低",
            "detail": name_list(weak, 6, unit="把") + "。弱钥匙指 DSA，或 RSA 小于 2048 位。",
            "fix": "换 ed25519 钥匙，再从对应 authorized_keys 里删掉旧行",
        })
    bad_files = [f for f in keys["files"] if f["flags"]]
    if bad_files:
        hard = any(f["symlink"] or "可写" in " ".join(f["flags"]) for f in bad_files)
        out.append({
            "id": "ak-file", "severity": "risk" if hard else "warn",
            "title": f"{len(bad_files)} 个 authorized_keys 的权限或形态有问题",
            "detail": name_list([f"{f['user']}：{'，'.join(f['flags'])}" for f in bad_files], 4, sep="；"),
            "fix": "owner-only：chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys",
        })
    silent = [
        f"{f['user']}：{k['fp'] or k['comment'][:20]}"
        for f in keys["files"] for k in f["keys"]
        if not k["comment"] and not k["unparsed"]
    ]
    if silent:
        out.append({
            "id": "no-comment", "severity": "warn",
            "title": f"{len(silent)} 把已授权钥匙没有备注",
            "detail": "备注是唯一记录「这把钥匙是谁的」的地方；没有它只能逐把去问。指纹："
                      + name_list(silent, 5, unit="把"),
            "fix": "逐把核对指纹，认不出来源的就删；自己的钥匙重新加上注释",
        })
    unparsed = sum(1 for f in keys["files"] for k in f["keys"] if k["unparsed"])
    if unparsed:
        out.append({
            "id": "ak-unparsed", "severity": "note",
            "title": f"{unparsed} 行 authorized_keys 面板解析不了",
            "detail": "解析不了不等于没有效：sshd 可能照样接受它。这一行需要人工看一眼。",
            "fix": "cat 对应文件人工核对（面板不代替人判断未知格式）",
        })
    if failed >= 500 and not guarded:
        out.append({
            "id": "brute-volume", "severity": "risk",
            "title": f"{wl} {failed} 次失败认证，来自 {attempts['distinct_ips']} 个地址",
            "detail": "排行靠前：" + "、".join(f"{t['ip']}（{t['count']} 次）" for t in attempts["top"][:3])
                      + (f"。{wn}" if wn else ""),
            "fix": "在网关侧封来源，或让 sshd 只监听跳板/内网；量持续大时配合 fail2ban",
        })
    accepted_password = next((a["count"] for a in attempts.get("accepted", []) if a["method"] == "password"), 0)
    if accepted_password:
        out.append({
            "id": "accepted-password", "severity": "warn" if not guarded else "note",
            "title": f"{wl}有 {accepted_password} 次口令方式登录成功",
            "detail": "即使配置允许，口令登录成功本身也值得逐条核对是不是本人、是不是已知来源。"
                      + (f" {wn}" if wn else ""),
            "fix": "choyeonctl ssh-audit --json 看 accepted 明细，并核对该 IP 与时间",
        })
    # 面板账号的两步验证覆盖面。panel 为 None 表示调用方没给这一层数据（比如单测只关心
    # sshd 那几条），此时不判——"没数据"在这里永远不等于"没问题"。
    if panel and panel.get("ok") and panel["accounts"]:
        pfailed = panel["recent_failed"]
        if not panel["with_totp"]:
            out.append({
                "id": "panel-2fa",
                "severity": "risk" if pfailed >= PANEL_BRUTE_RISK else "warn" if pfailed else "note",
                "title": f"面板 {panel['accounts']} 个账号里没有一个开两步验证",
                "detail": "面板的网页终端发出去的就是这台机器的 root shell，现在拦在猜口令的人前面的只有口令本身；"
                          f"限速按 IP 计数（每 {config.LOGIN_WINDOW_SEC} 秒 {config.LOGIN_LIMIT} 次），"
                          "换来源 IP 的分布式撞库不受它影响。"
                          + (f"过去 24 小时面板登录已失败 {pfailed} 次。" if pfailed else "过去 24 小时没有失败记录。"),
                "fix": "设置 → 两步验证，用验证器 App 扫码开启；choyeonctl user list 看每个账号的覆盖情况",
            })
        elif panel["with_totp"] < panel["accounts"]:
            out.append({
                "id": "panel-2fa-partial", "severity": "note",
                "title": f"面板账号只有 {panel['with_totp']}/{panel['accounts']} 个开了两步验证",
                "detail": "第二因子按账号生效，没开的那个账号就是整条链上唯一没有加固的一环——"
                          "而它往往正是给别人建的那类账号。",
                "fix": "让未开启的账号本人登录后在设置页开启；只有 root 能用 choyeonctl user 2fa-disable 关闭",
            })
    # 排序与标签同一出处（app.severity）：这一页最轻那档叫 note，四个对账页叫 info，
    # 两者在 SEV_ORDER 里同为末档，新页签不必再猜哪个词排在最后。
    out.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["id"]))
    return out


def _summary(findings: list[dict]) -> dict:
    out = {"risk": 0, "warn": 0, "note": 0}
    for f in findings:
        out[f["severity"]] += 1
    return out


async def audit(refresh: bool = False) -> dict:
    """带 TTL 的只读对账入口；并发与连点合并成一轮 `sshd -T` + journal 读取（见 app/probe.py）。"""
    return await probe.read(_cache, _LOCK, _collect, refresh=refresh, ttl=CACHE_TTL)


async def _collect() -> dict:
    as_root = os.geteuid() == 0
    ssh, passwd_scan, guard, attempts, panel = await asyncio.gather(
        _sshd_settings(), asyncio.to_thread(_passwd_users), _guard_status(), _attempts(),
        asyncio.to_thread(_panel_2fa),
    )
    users, passwd_partial = passwd_scan
    try:
        ports_data = await exposure_service.audit()
    except Exception:  # noqa: BLE001 - 端口那一层失败不该把这一页一起拖没
        ports_data = None
    keys = await asyncio.to_thread(_authorized_keys, users, as_root)
    accounts = _accounts(users, passwd_partial)
    exposure = _ssh_exposure(ssh["values"], ports_data)
    findings = _findings(ssh, keys, accounts, attempts, guard, exposure, panel)

    notes = list(ssh["notes"]) + list(accounts["notes"])
    for extra in (guard.get("note"), attempts.get("note"), panel.get("note")):
        if extra:
            notes.append(extra)
    if not as_root:
        notes.append("本次不是 root 运行：/etc/shadow 和其它用户的 authorized_keys 读不到，这两项没有核对。")
    if keys["truncated"]:
        notes.append(f"单个 authorized_keys 最多逐条列 {MAX_KEYS} 行，超出的只计数没列出。")
    notes.append(
        "这一层严格只读：不写 sshd_config、不 reload sshd、不改 authorized_keys——"
        "SSH 是这台机器的主入口，猜错就是把人锁在门外。"
    )

    return {
        "ok": True,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "as_root": as_root,
        "sshd": {"source": ssh["source"], "effective": ssh["effective"],
                 "port": exposure["port"], "reachable": exposure["reachable"], "verdict": exposure["verdict"],
                 "reason": exposure["reason"]},
        "settings": _setting_rows(ssh["values"], exposure["reachable"] is not False, guard["status"]),
        "guard": {"status": guard["status"], "phrase": _guard_phrase(guard), "units": guard["units"]},
        "keys": keys,
        "accounts": accounts,
        "attempts": attempts,
        # 面板账号的两步验证覆盖情况：只有计数，没有任何账号的密钥或哈希。
        "panel": panel,
        "counts": _summary(findings),
        "findings": findings,
        "notes": notes,
    }


def doctor_item(data: dict) -> dict:
    """自检里的「登录安全」一行。

    fail 只留给入侵迹象（空口令、多余 uid=0）——那是"这台机器可能已经被人进来了"；
    加固建议一律 warn：它们是真问题，但不代表面板装坏了，硬凑成 fail 只会让人以为
    部署出了问题，反而去看错地方。
    """
    if data["sshd"]["source"] in ("missing", "unreadable"):
        first = data["notes"][0] if data["notes"] else ""
        return {"status": "warn", "detail": f"读不到 sshd 配置，登录方式无从核对：{first}",
                "fix": "choyeonctl ssh-audit", "fixable": False}
    counts = data["counts"]
    if data["accounts"]["empty_password"] or data["accounts"]["uid0_extra"]:
        listed = data["accounts"]["empty_password"] + data["accounts"]["uid0_extra"]
        return {"status": "fail",
                "detail": f"存在不该有的账号（空口令或额外 uid=0）：{name_list(listed, 5)}",
                "fix": "choyeonctl ssh-audit 里看完整判词，先按入侵痕迹处理", "fixable": False}
    if counts["risk"]:
        risk_titles = [f["title"] for f in data["findings"] if f["severity"] == "risk"]
        # 没有值得处理的那一档就别写"0 项"：那是一句关于不存在之事的陈述
        mid = f"、{counts['warn']} 项值得处理" if counts["warn"] else ""
        return {"status": "warn",
                "detail": tallied(risk_titles, " 项要紧", "项", counts["risk"],
                                  lead="登录方式有 ", mid=mid),
                "fix": "choyeonctl ssh-audit", "fixable": False}
    if counts["warn"]:
        return {"status": "warn", "detail": f"没有要紧项，另有 {counts['warn']} 项加固建议",
                "fix": "choyeonctl ssh-audit", "fixable": False}
    if data["guard"]["status"] in ("unknown", "no-systemd"):
        # 没核对成不等于没问题：绿灯不能说"这台机器没人撞库也不怕"，
        # 而爆破防护恰恰是这句话里唯一还没验证的那一半。
        return {"status": "warn",
                "detail": f"登录方式没有要紧项，但爆破防护是否在跑没核对成（{data['guard']['phrase']}）",
                "fix": "choyeonctl ssh-audit", "fixable": False}
    if not data["attempts"]["ok"]:
        # 同一个道理，落在句子里的另一半：读不到认证日志时 failed 是 _no_attempts 的占位 0，
        # 照原样念出来就成了"这 24 小时一个人都没试进来"——恰恰是这台机器最需要知道、
        # 而这一次没查到的那件事。日志写去别处（syslog、远程）的机器上，绿灯最容易出现。
        return {"status": "warn",
                "detail": "登录方式没有要紧项，但这 24 小时的登录尝试读不到"
                          f"（{data['attempts'].get('note') or '没有可读的 sshd 日志'}），"
                          "有没有人在撞库无从核对",
                "fix": "choyeonctl ssh-audit", "fixable": False}
    scanned = data["accounts"]
    if not scanned.get("complete", True):
        # 和上面两条例外同一条纪律：没扫完的那一段不能算核对过。排在 4000 行之后
        # 的空口令账号是看不见的，而绿灯说的恰恰是"这台机器没有那种账号"。
        files = "、".join(scanned.get("scan_partial") or [])
        return {"status": "warn",
                "detail": f"登录方式没有要紧项，但账号表这一轮没读到底（上限 {ACCOUNTS_MAX_LINES} 行，"
                          f"{files} 更长），空口令与多余 uid=0 没有核对完整",
                "fix": "choyeonctl ssh-audit", "fixable": False}
    att = data["attempts"]
    # 绿灯这一行也要把窗口说对：日志读满上限时"24h 失败认证 N 次"里的 N 只是最后
    # 一段的下限，而这正是 doctor 最容易被当成"全部核对过"的那句话。
    detail = (f"sshd 端口 {data['sshd']['port']} 的登录方式没有要紧项；"
              f"{window_label(att)}失败认证 {att['failed']} 次"
              + ("（日志超过这一轮读取上限，这是下限）" if att.get("truncated") else ""))
    if not data["as_root"]:
        detail += "（非 root，空口令与他人的 authorized_keys 未核对）"
    return {"status": "pass", "detail": detail, "fix": "", "fixable": False}
