"""批次 63：只读探针的「浏览器等多久」必须 ≥「服务端那一轮最多跑多久」。

七个对账页签点了「重新探测」之后，前端原先一律只等 15 秒（api.ts 的默认值），而后端那一轮
的子进程预算是按「这台机器可能很慢」写的：`apt list` 一支 120 秒、dnf 那一支两条各 180 秒、
`ss` 加防火墙三条各 20 秒。浏览器先撒手会发生的事很具体：

- 服务端那一轮**仍然在跑**，它持有那一页的锁，跑完把结果写进缓存；
- 画面上只剩「请求超时，请稍后重试」，用户于是再点一次——排到同一把锁后面；
- 而那一轮刚落地的缓存，本来正是这一次点击要的答案。

也就是说 15 秒不是「保守」，是把一次会成功的探测读成失败，再让那把锁为这次误判多跑一轮。
「前端超时 ≥ 服务端子进程上限」这条口径本来就写在 api.ts，但只落在写操作那几档
（doctorFix 960 秒一类），只读探针全留在默认值上。

这一批把七档按各自最坏一支算齐，并把算法本身做成门：常量和**条数**都从后端源码解析，
所以「把 DNF_TIMEOUT 调到 600 秒却忘了前端」「给 firewalld 多加一条探测命令」都会把门打红，
而不是等到线上出现「点了没反应」。

同一批收掉文件页的第二份字节上限：列表接口本来就回传 `maxUploadBytes`（服务端
`CP_MAX_UPLOAD_MB`，默认 8MB），而 open() 里另写死 1MB——3MB 的日志/SQL 后端肯开、肯存，
前端先拒，而且两句话术不一样，用户会以为存在两道不同的限制。

一处如实说明：这台生产机上七个探针实测最慢 4.61 秒（2026-10-20），所以这一批不是治「正在
发生的卡顿」，是把上限与预算对齐，并让将来调大预算的人被迫同时改前端那一档。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
APP = BACKEND / "app"
SERVICES = APP / "services"
API_TS = (REPO / "web" / "src" / "api.ts").read_text(encoding="utf-8")
FILES_VUE = (REPO / "web" / "src" / "views" / "Files.vue").read_text(encoding="utf-8")


def _read(rel: Path) -> str:
    return rel.read_text(encoding="utf-8")


UTIL = _read(APP / "util.py")
UPDATES = _read(SERVICES / "updates_service.py")
EXPOSURE = _read(SERVICES / "exposure_service.py")
FIREWALL = _read(SERVICES / "firewall_service.py")
LOGIN = _read(SERVICES / "login_audit_service.py")
SYSTEMD_OPS = _read(SERVICES / "systemd_ops.py")
PENDING = _read(SERVICES / "pending_service.py")
STORAGE = _read(SERVICES / "storage_service.py")
MEMORY = _read(SERVICES / "memory_service.py")
LOAD = _read(SERVICES / "load_service.py")
CONFIG = _read(APP / "config.py")
FILES_SERVICE = _read(SERVICES / "files_service.py")

# pending 的内核包比对每装一个旧内核就多跑一次 `dpkg --compare-versions`，代码里没有上限
# （`dpkg -S` 那一层有 MAX_PKG_LOOKUPS，这一层没有）。这里按 8 个旧内核算：这个命令不碰 dpkg
# 锁，正常是几十毫秒，8 个已经是「管理员从不删旧内核」的机器。真超过 8 个的话，该收口的是
# pending_service 里那个循环，而不是在这一行再乘一档。
OLD_KERNELS_ASSUMED = 8

# 余量是给 TLS/nginx/序列化留的，不是第二份预算。
MARGIN_MIN_MS = 15_000
MARGIN_RATIO = 0.10


def const(text: str, name: str, where: str) -> float:
    m = re.search(rf"^{name}\s*=\s*([0-9][0-9.]*)\s*(?:#.*)?$", text, re.M)
    if not m:
        raise AssertionError(f"{where} 里读不到常量 {name}：这一档算不出来，门不许当它是 0")
    return float(m.group(1))


def branch(text: str, start: str, where: str) -> str:
    """取 `start` 这一支的正文，到自己下一个同级 elif/else 或下一个顶层 def 为止。

    同级才算：分支正文里还有自己的 if/elif，把那些当成「这一支结束了」会漏数。
    锚点必须全文件唯一——`if family == DEBIAN:` 在 `_reboot_verdict` 里也有一句，
    取第一处就会把 `_collect` 的那一支读成空，于是门绿着、预算却是错的。
    """
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if start in ln]
    if len(hits) != 1:
        raise AssertionError(f"{where} 里「{start}」出现 {len(hits)} 次，锚点不够唯一，数出来的条数不可信")
    hit = hits[0]
    base = len(lines[hit]) - len(lines[hit].lstrip())
    out = [lines[hit]]
    for ln in lines[hit + 1:]:
        s = ln.strip()
        indent = len(ln) - len(ln.lstrip())
        if indent <= base and (s.startswith("elif ") or s.startswith("else:")):
            break
        if ln and not ln[0].isspace() and (s.startswith(("def ", "async def ", "@", "class "))):
            break
        out.append(ln)
    return "\n".join(out)


def count(hay: str, needle: str) -> int:
    return len(re.findall(re.escape(needle), hay))


def max_tuple_arity(text: str, name: str, where: str) -> int:
    """登记表里最长的那一行：`_auto_updates` 是按候选单元逐个试到出结论为止。"""
    idx = text.find(f"{name} = {{")
    if idx < 0:
        raise AssertionError(f"{where} 里读不到 {name}")
    best = 0
    for line in text[idx:].splitlines()[1:]:
        s = line.strip()
        if s == "}":
            break
        m = re.search(r"\(([^()]*)\)", s)
        if not m:
            continue
        best = max(best, len([x for x in m.group(1).split(",") if x.strip()]))
    if not best:
        raise AssertionError(f"{name} 里没有解析出任何候选单元")
    return best


def default_timeout_ms(api_text: str = API_TS) -> int:
    m = re.search(r"timeoutMs\s*=\s*(\d+)", api_text)
    if not m:
        raise AssertionError("api.ts 里读不到 request() 的默认超时")
    return int(m.group(1))


def client_timeout_ms(path: str, api_text: str = API_TS) -> int:
    """api.ts 里 `req(`/xxx`` 那一行的 timeout；没显式写就是吃默认值。"""
    line = next((ln for ln in api_text.splitlines() if f"req(`{path}" in ln), None)
    if line is None:
        raise AssertionError(f"api.ts 里找不到 {path} 这一档")
    m = re.search(r"timeout:\s*(\d+)", line)
    return int(m.group(1)) if m else default_timeout_ms(api_text)


def doc_total_s(key: str, api_text: str = API_TS) -> float:
    """api.ts 注释里这一档写明的合计秒数（区块里最后一个 `= Ns`）。"""
    lines = api_text.splitlines()
    pat = re.compile(rf"^ *// +{re.escape(key)} +")
    start = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
    if start is None:
        raise AssertionError(f"api.ts 的预算表里没有 {key} 这一行——上限没有出处")
    chunk = []
    for ln in lines[start:]:
        s = ln.strip()
        if chunk and (not s.startswith("//") or re.match(r"^// +[a-z][a-z0-9-]* +", s)):
            break
        chunk.append(s)
    hits = re.findall(r"=\s*([0-9]+(?:\.[0-9]+)?)s(?![0-9.])", "\n".join(chunk))
    if not hits:
        raise AssertionError(f"{key} 那一行没写合计秒数（结尾应有 `= Ns`），没法与重算结果对账")
    return float(hits[-1])


# ------------------------------------------------------------------ 各档预算（全部由源码算）

def budget_updates(updates: str = UPDATES) -> float:
    apt = count(branch(updates, 'await run("apt", ["list", "--upgradable"]', "updates_service"),
                      "timeout=APT_TIMEOUT")
    dnf = count(branch(updates, "elif family == RHEL:", "updates_service"), "timeout=DNF_TIMEOUT")
    units = max_tuple_arity(updates, "AUTO_UNITS_BY_FAMILY", "updates_service")
    per_unit = count(branch(updates, "async def _auto_updates", "updates_service"), "timeout=PROBE_TIMEOUT")
    pkg = max(apt * const(updates, "APT_TIMEOUT", "updates_service"),
              dnf * const(updates, "DNF_TIMEOUT", "updates_service"))
    return pkg + units * per_unit * const(UTIL, "PROBE_TIMEOUT", "util")


def budget_ports(exposure: str = EXPOSURE, firewall: str = FIREWALL) -> float:
    ss = count(exposure, "timeout=CTRL_TIMEOUT")
    fw = count(branch(firewall, "tool == FIREWALLD:", "firewall_service"), "timeout=CTRL_TIMEOUT")
    return (ss + fw) * const(UTIL, "CTRL_TIMEOUT", "util")


def budget_ssh_audit(login: str = LOGIN, ports: float | None = None) -> float:
    """这一页并发读三样，取最坏一支，然后再串一次端口对账。"""
    gather = max(const(login, "SSHD_TIMEOUT", "login_audit_service"),
                 const(login, "JOURNAL_TIMEOUT", "login_audit_service"),
                 const(UTIL, "PROBE_TIMEOUT", "util"))
    if "await exposure_service.audit()" not in login:
        raise AssertionError("login_audit_service 不再串端口对账了——这一档的预算要重算")
    return gather + (budget_ports() if ports is None else ports)


def budget_pending(pending: str = PENDING) -> float:
    sub = const(pending, "SUBPROCESS_TIMEOUT", "pending_service")
    lookups = const(pending, "MAX_PKG_LOOKUPS", "pending_service")
    m = re.search(r'\["nginx", "-T"\].*?timeout=(\d+)', pending, re.S)
    if not m:
        raise AssertionError("pending_service 里 nginx -T 的超时写法与这套算法不一致，请重算")
    sites = count(pending, "subprocess.run(")
    if sites != 7:
        raise AssertionError(
            f"pending_service 的子进程位点变了（现在 {sites} 处）：内核比对、被替换映射、"
            "systemctl 两条各是几趟要重新数一遍")
    # 1 趟内核包列表 + OLD_KERNELS_ASSUMED 趟版本比对 + 每个路径 2 趟（dpkg -S、dpkg-query -W）
    # + systemctl 2 趟 + nginx -T 1 趟
    return sub * (1 + OLD_KERNELS_ASSUMED + 2 * lookups + 2) + float(m.group(1))


def default_roots(config: str = CONFIG) -> int:
    out = 0
    for key in ("CP_FILE_ROOTS", "CP_LOG_ROOTS"):
        m = re.search(rf'_paths\("{key}",\s*"([^"]*)"', config)
        if not m:
            raise AssertionError(f"config.py 里读不到 {key} 的默认值")
        out += len([p for p in m.group(1).split(",") if p.strip()])
    return out


def budget_storage(storage: str = STORAGE, config: str = CONFIG) -> float:
    return (const(UTIL, "PROBE_TIMEOUT", "util")
            + default_roots(config) * const(storage, "ROOT_BUDGET_SEC", "storage_service"))


def budget_memory(memory: str = MEMORY) -> float:
    return (const(memory, "RATE_WINDOW_SEC", "memory_service")
            + const(memory, "PROC_BUDGET_SEC", "memory_service")
            + const(memory, "JOURNAL_TIMEOUT", "memory_service"))


def budget_load(load: str = LOAD) -> float:
    return (const(load, "SCAN_BUDGET_SEC", "load_service")
            + const(load, "SAMPLE_WINDOW_SEC", "load_service"))


def required_ms(budget_s: float) -> int:
    ms = budget_s * 1000.0
    return int(ms + max(MARGIN_MIN_MS, ms * MARGIN_RATIO))


# key -> (api.ts 里那一档的路径, 预算算法)
BUDGETS: dict[str, tuple[str, object]] = {
    "updates": ("/updates", budget_updates),
    "ports": ("/ports", budget_ports),
    "ssh-audit": ("/ssh-audit", budget_ssh_audit),
    "pending": ("/pending", budget_pending),
    "storage": ("/storage", budget_storage),
    "memory": ("/memory", budget_memory),
    "load": ("/load", budget_load),
}


def budget_of(key: str) -> float:
    return float(BUDGETS[key][1]())  # type: ignore[operator]


def check_alignment(key: str, api_text: str = API_TS, budget: float | None = None) -> tuple[float, int, int]:
    """预算 → 至少要给的毫秒 → 前端实际给的毫秒；不给够就抛 AssertionError。"""
    path = BUDGETS[key][0]
    b = budget_of(key) if budget is None else budget
    need = required_ms(b)
    got = client_timeout_ms(path, api_text)
    if got < need:
        raise AssertionError(
            f"{path}：服务端最坏一支要 {b:.1f}s，加余量至少要 {need}ms，前端只给 {got}ms。"
            "浏览器会在这一轮跑完之前报「请求超时」，而后端仍持有那一页的锁往下跑，"
            "用户再点一次只是排到同一把锁后面")
    return b, need, got


def check_doc_total(key: str, api_text: str = API_TS, budget: float | None = None) -> float:
    """写给人看的那份秒数也算一遍：注释与重算结果不一致就是注释在骗人。"""
    b = budget_of(key) if budget is None else budget
    written = doc_total_s(key, api_text)
    if abs(written - b) > 0.05:
        raise AssertionError(f"{key}：api.ts 注释写的是 {written}s，按后端常量重算是 {b:.1f}s")
    return b


def code_lines_of(text: str, start: str, end: str) -> list[str]:
    """取 <script> 段里真正参与执行的那几行：整行注释与注释段落一律剥掉。

    剥注释是必需的：文件里现在留着「以前 open() 里另写死 1MB」这类说明句，
    不剥掉的话，「这里不许再有 1MB 字面量」的断言会打到自己的解释上。
    """
    i, j = text.find(start), text.find(end)
    if i < 0 or j < 0:
        raise AssertionError(f"Files.vue 里读不到 {start} … {end} 这一段")
    out = []
    for ln in text[i:j].splitlines():
        s = ln.strip()
        if not s or s.startswith(("//", "/*", "*", "/* ")):
            continue
        out.append(ln)
    return out


def local_byte_caps(vue_text: str = FILES_VUE) -> list[str]:
    return [ln.strip() for ln in code_lines_of(vue_text, "<script", "</script>")
            if re.search(r"1024\s*\*\s*1024", ln)]


def check_files_cap(vue_text: str = FILES_VUE) -> None:
    body = "\n".join(code_lines_of(vue_text, "<script", "</script>"))
    if "maxUploadBytes" not in body:
        raise AssertionError("Files.vue 不再读列表接口回传的 maxUploadBytes，等于另立了一份上限")
    if not re.search(r"const capBytes = computed[^\n]*maxUploadBytes", body):
        raise AssertionError("Files.vue 的额度没收成「一个 computed、一个出处」")
    if not re.search(r"e\.size > capBytes\.value", body):
        raise AssertionError("open() 的在线打开预检没读服务端那一份额度")
    hit = body.find("e.size > capBytes.value")
    if "fmtSize(capBytes.value)" not in body[hit:hit + 240]:
        raise AssertionError("open() 拒绝的那句话里没带服务端的数，用户在两处看到两个数")
    bad = local_byte_caps(vue_text)
    if bad:
        raise AssertionError(f"Files.vue 里还有本地写死的字节上限：{bad}")


# ==========================================================================================


class Batch63Test(unittest.TestCase):
    def test_backend_constants_are_readable(self):
        """门不许在常量改名或挪家时静默算出 0——每个名字都点得到才算数。"""
        for text, name, where in [
            (UTIL, "CTRL_TIMEOUT", "util"), (UTIL, "PROBE_TIMEOUT", "util"),
            (UPDATES, "APT_TIMEOUT", "updates_service"), (UPDATES, "DNF_TIMEOUT", "updates_service"),
            (LOGIN, "SSHD_TIMEOUT", "login_audit_service"),
            (LOGIN, "JOURNAL_TIMEOUT", "login_audit_service"),
            (PENDING, "SUBPROCESS_TIMEOUT", "pending_service"),
            (PENDING, "MAX_PKG_LOOKUPS", "pending_service"),
            (STORAGE, "ROOT_BUDGET_SEC", "storage_service"),
            (MEMORY, "RATE_WINDOW_SEC", "memory_service"), (MEMORY, "PROC_BUDGET_SEC", "memory_service"),
            (LOAD, "SCAN_BUDGET_SEC", "load_service"), (LOAD, "SAMPLE_WINDOW_SEC", "load_service"),
        ]:
            self.assertGreater(const(text, name, where), 0, f"{where}.{name} 必须是正数")
        # PROBE_TIMEOUT / CTRL_TIMEOUT 只有一个住处（util），用到它的页签不许自己抄一份
        for src, where in ((UPDATES, "updates_service"), (EXPOSURE, "exposure_service"),
                           (STORAGE, "storage_service"), (FIREWALL, "firewall_service")):
            used = [n for n in ("PROBE_TIMEOUT", "CTRL_TIMEOUT") if f"timeout={n}" in src]
            self.assertTrue(used, f"{where} 这一档不再引用 util 的超时，预算表要重看")
            imp = re.search(r"^from \.\.util import (.+)$", src, re.M)
            self.assertIsNotNone(imp, f"{where} 没有从 ..util 导入超时")
            for n in used:
                self.assertIn(n, imp.group(1), f"{where} 用了 {n} 却没从 util 导入")
                self.assertNotIn(f"{n} = ", src, f"{where} 里自己定义了一份 {n}")

    def test_budget_multiplicities_match_the_source(self):
        """条数也是从源码数的：某一档多加一条探测命令，这张表必须先跟着变。"""
        self.assertEqual(count(branch(UPDATES, "elif family == RHEL:", "u"), "timeout=DNF_TIMEOUT"), 2)
        self.assertEqual(count(branch(UPDATES, 'await run("apt", ["list", "--upgradable"]', "u"),
                               "timeout=APT_TIMEOUT"), 1)
        self.assertEqual(max_tuple_arity(UPDATES, "AUTO_UNITS_BY_FAMILY", "u"), 3)
        self.assertEqual(count(branch(UPDATES, "async def _auto_updates", "u"), "timeout=PROBE_TIMEOUT"), 1)
        self.assertEqual(count(EXPOSURE, "timeout=CTRL_TIMEOUT"), 1)
        self.assertEqual(count(branch(FIREWALL, "tool == FIREWALLD:", "f"), "timeout=CTRL_TIMEOUT"), 3)
        self.assertEqual(count(branch(FIREWALL, 'st = await run("ufw", ["status", "verbose"]', "f"),
                                "timeout=CTRL_TIMEOUT"), 2)
        self.assertEqual(default_roots(), 4)
        self.assertIn("timeout=PROBE_TIMEOUT", branch(SYSTEMD_OPS, "async def unit_states", "systemd_ops"))

    def test_seven_probes_all_override_the_default(self):
        """七档一律显式给值：留在默认上等于「没人核对过这一档要多久」。"""
        self.assertEqual(default_timeout_ms(), 15000)
        for key, (path, _fn) in BUDGETS.items():
            line = next(ln for ln in API_TS.splitlines() if f"req(`{path}" in ln)
            self.assertIn("timeout:", line, f"{key} 这一档还在吃默认超时")

    def test_client_timeout_covers_server_budget(self):
        for key in BUDGETS:
            with self.subTest(probe=key):
                _budget, need, got = check_alignment(key)
                self.assertGreaterEqual(got, need)

    def test_api_comment_totals_are_the_recomputed_numbers(self):
        for key in BUDGETS:
            with self.subTest(probe=key):
                self.assertGreater(check_doc_total(key), 0)

    def test_files_cap_has_only_the_server_copy(self):
        check_files_cap()

    def test_backend_publishes_the_number_it_enforces(self):
        """一个数：额度从 config 来，列表接口回传它，read_text 与上传比的也是它。"""
        self.assertRegex(FILES_SERVICE, r"MAX_FILE_BYTES\s*=\s*config\.MAX_UPLOAD_BYTES")
        self.assertIn('"maxUploadBytes": MAX_FILE_BYTES', FILES_SERVICE)
        self.assertRegex(FILES_SERVICE, r"st\.st_size > MAX_FILE_BYTES")
        self.assertRegex(CONFIG, r'MAX_UPLOAD_BYTES\s*=\s*_int\("CP_MAX_UPLOAD_MB"')


class CounterProofTest(unittest.TestCase):
    """反证：把改掉的东西喂回同一个检查器，门必须红。"""

    def test_lowering_the_updates_timeout_fails_the_gate(self):
        old = client_timeout_ms("/updates")
        broke = API_TS.replace(f"timeout: {old}", "timeout: 15000", 1)
        self.assertNotEqual(broke, API_TS, "反证没打上：updates 那一行写法变了")
        with self.assertRaises(AssertionError) as ctx:
            check_alignment("updates", broke)
        self.assertIn("/updates", str(ctx.exception))

    def test_raising_dnf_timeout_fails_the_gate(self):
        broke = UPDATES.replace("DNF_TIMEOUT = 180", "DNF_TIMEOUT = 600", 1)
        self.assertNotEqual(broke, UPDATES, "反证没打上：DNF_TIMEOUT 的写法变了")
        budget = budget_updates(broke)
        self.assertGreater(budget, 375.0)
        with self.assertRaises(AssertionError) as ctx:
            check_alignment("updates", API_TS, budget)
        self.assertIn("至少要", str(ctx.exception))

    def test_adding_a_firewall_probe_fails_the_gate(self):
        anchor = 'listed = await run("firewall-cmd", ["--list-all"], timeout=CTRL_TIMEOUT)'
        broke = FIREWALL.replace(anchor,
                                 'extra = await run("firewall-cmd", ["--list-services"], timeout=CTRL_TIMEOUT)\n'
                                 f"        {anchor}", 1)
        self.assertNotEqual(broke, FIREWALL, "反证没打上：firewalld 那几行写法变了")
        budget = budget_ports(EXPOSURE, broke)
        self.assertEqual(budget, budget_ports() + 20.0)
        with self.assertRaises(AssertionError):
            check_alignment("ports", API_TS, budget)

    def test_stale_comment_total_fails_the_gate(self):
        broke = API_TS.replace("× PROBE_TIMEOUT(5s) = 375s", "× PROBE_TIMEOUT(5s) = 360s", 1)
        self.assertNotEqual(broke, API_TS)
        with self.assertRaises(AssertionError):
            check_doc_total("updates", broke)

    def test_restoring_the_hardcoded_one_megabyte_fails_the_files_gate(self):
        anchor = ("  if (capBytes.value && e.size > capBytes.value) {\n"
                  "    return msg.warning(`文件超过 ${fmtSize(capBytes.value)}，请使用下载`);")
        broke = FILES_VUE.replace(anchor,
                                  "  if (e.size > 1024 * 1024) {\n"
                                  "    return msg.warning('文件过大，请使用下载');", 1)
        self.assertNotEqual(broke, FILES_VUE, "反证没打上：open() 那段写法变了")
        with self.assertRaises(AssertionError) as ctx:
            check_files_cap(broke)
        self.assertIn("预检没读服务端那一份额度", str(ctx.exception))

    def test_reintroducing_a_local_cap_alongside_the_server_one_fails(self):
        """最像真实回归的一种：open() 改好了，别处又留一份本地写死的额度。"""
        anchor = "const capBytes = computed<number>(() => Number(data.value?.maxUploadBytes || 0));"
        broke = FILES_VUE.replace(anchor, f"{anchor}\nconst legacyCap = 1024 * 1024;", 1)
        self.assertNotEqual(broke, FILES_VUE, "反证没打上：capBytes 那一行写法变了")
        with self.assertRaises(AssertionError) as ctx:
            check_files_cap(broke)
        self.assertIn("本地写死的字节上限", str(ctx.exception))

    def test_losing_the_server_cap_lookup_fails_the_files_gate(self):
        anchor = "const capBytes = computed<number>(() => Number(data.value?.maxUploadBytes || 0));"
        broke = FILES_VUE.replace(anchor, "const capBytes = computed<number>(() => 8 * 1024 * 1024);", 1)
        self.assertNotEqual(broke, FILES_VUE)
        with self.assertRaises(AssertionError):
            check_files_cap(broke)

    def test_branch_helper_does_not_stop_at_nested_else(self):
        """branch() 的「同级」判定本身也反证一次：截早了会少数命令，门就会假绿。"""
        fw = branch(FIREWALL, "tool == FIREWALLD:", "firewall_service")
        self.assertIn("--list-all", fw, "firewalld 那一支被截短了，三条命令只数到一部分")
        self.assertNotIn("tool == UFW", fw, "firewalld 那一支越界吃进了 ufw 的两条")


if __name__ == "__main__":
    unittest.main()
