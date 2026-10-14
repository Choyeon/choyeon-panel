"""批次㉙：写操作按钮的第二次点击，必须在请求发出去之前就被本地挡掉。

这一批不动后端，收的是三类同源的前端缺陷：

1. 连点两次 = 两条同名写请求。第一条成功，第二条撞上「已存在 / 已删除 0 个 / 正在部署」，
   用户看到的是一句和刚才那一下无关的报错，而真实结果（成功）正好被它盖住。
2. 行操作与整轮操作互相踩。Alerts 的单条确认与整轮巡检、Databases 的批量删 key 与扫描，
   动的是同一份服务端状态，前端却各转各的圈，谁都不知道对方在写。
3. 后到的旧响应盖掉当前画面。翻页/切库时两个请求同时在飞，先发的后返回，把上一页、
   上一个目录的数据写回 data（loadTables / searchJournal 的 seq 闸就是为这个）。

于是钉住的不变量是：每个「点了会发写请求」的入口，在第一个 await 之前就有同步早退，
并且对应控件带 :disabled。早退管的是不吃 disabled 的路径（回车走 form submit、程序化调用），
disabled 管的是「点不动」这件事必须看得见。naive-ui 的 :loading 是否阻断点击在这里无法验证，
所以两者都留，不拿其中一个当另一个的替身。

合法豁免只有三类，各自写明了依据（见 TestConfirmDialogChoke / TestRefreshDedupeExemption
与 LOCAL_ONLY_EXEMPT）：
- 请求在 ConfirmActionDialog 的 run() 之后才发，那里是全 panel 破坏性操作共用的一道闸。
- 只读刷新按钮走 useCachedFetch.refresh()，同一 key 的并发 GET 由 e.inflight 合并成一次往返。
- 六个同构写入口共用一个本地包装（RedisKeyPanel 的 act、Apps 的按行 act），闸做在包装里。
跨行并发则压根不是缺陷：Apps 的两行同时部署是合法用法，那里只挡同一行的第二次点击。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT.parent / "web" / "src"
VIEWS = sorted((WEB / "views").glob("*.vue"))
COMPS = sorted((WEB / "components").glob("*.vue"))
API_SRC = (WEB / "api.ts").read_text(encoding="utf-8")

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# 假值右边界不能用 \b：`''` 后面接的是分号，两个非词字符之间没有边界，于是 `x.value = ''`
# 会被当成"置成忙"，清位反过来又检不出来。
FALSY = re.compile(r"""(?:''|""|null|false|0)(?![\w])""")

# 每个 api.ts 条目形如 `  name: (a, b) => req(...)`，可能折行。取「本条目起点到下一条目起点」
# 这一片，比按行拼更稳（多行条目在 api.ts 里确实存在）。
_ENTRY = re.compile(r"^\s+([A-Za-z][A-Za-z0-9_]*)\s*:\s*\(", re.M)


def _api_write_names() -> set:
    hits = list(_ENTRY.finditer(API_SRC))
    out = set()
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(API_SRC)
        chunk = API_SRC[m.start():end]
        if "req(" not in chunk and "request(" not in chunk:
            continue
        meths = re.findall(r"method:\s*'([A-Z]+)'", chunk)
        # req() 的默认方法：带 body 就是 POST，只看显式 method 会漏掉一半写接口。
        is_write = any(x in WRITE_METHODS for x in meths) or (
            not meths and re.search(r"\bbody:", chunk) is not None
        )
        if is_write:
            out.add(m.group(1))
    return out


API_WRITE = _api_write_names()

# 函数定义：function 与 const 箭头两种写法都在用；参数表里带括号（`() => string`）和
# 返回值注解（`): Promise<boolean>`）都得容得下，所以参数表用配对扫描而不是 `[^)]*`。
_DEF = re.compile(r"(?:async\s+)?function\s+([A-Za-z][A-Za-z0-9_]*)\s*\(|"
                  r"^\s*(?:const|let)\s+([A-Za-z][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(")
# 模板里的 @click="fn(...)"、@update:value="fn"；渲染函数与下拉项里的 onClick:/run:；
# 以及原生 DOM 回调赋值（inp.onchange = () => uploadFiles(...)，Files 的上传就走这里）。
# 三个出处都要认，否则整页的写入口都不在守卫范围内——Services 的行内动作就藏在 run: 里。
_BIND_TEMPLATE = re.compile(r'@[A-Za-z0-9.:\-]+\s*=\s*"([^"]*)"')
_BIND_SCRIPT = re.compile(r"(?:on[A-Z][A-Za-z0-9]*|run|handler)\s*:\s*([^,\n]+)")
_BIND_DOM = re.compile(r"\.on[a-z]+\s*=\s*([^;\n]+)")


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pair(text: str, i: int, open_ch: str, close_ch: str):
    depth = 0
    for j in range(i, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j
    return None


def _func_slices(text: str) -> dict:
    """函数名 -> 函数体。右边界取「下一个顶层定义」与「第一行顶格的 }」里更早的那个：
    只按下一个顶层定义切，会把中间夹着的 const 箭头函数算进上一个函数，
    那些写请求就被算到只读的 load() 头上了。
    """
    hits = list(_DEF.finditer(text))
    out = {}
    for i, m in enumerate(hits):
        close = _pair(text, m.end() - 1, "(", ")")
        if close is None:
            continue
        brace = text.find("{", close)
        # 参数表之后到 { 之间只允许返回值注解，否则这不是个函数定义（比如三元表达式）
        if brace == -1 or len(text[close:brace].strip()) > 60:
            continue
        nxt = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        end_brace = re.compile(r"^\}\s*$", re.M).search(text, brace)
        out[m.group(1) or m.group(2)] = text[brace + 1: min(nxt, end_brace.start() if end_brace else len(text))]
    return out


def _wired_names(text: str) -> set:
    names = set()
    for m in _BIND_TEMPLATE.finditer(text):
        expr = m.group(1)
        names.update(re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*\(", expr))
        names.update(re.findall(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*$", expr))
    for m in _BIND_SCRIPT.finditer(text):
        names.update(re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*\(", m.group(1)))
    for m in _BIND_DOM.finditer(text):
        names.update(re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*\(", m.group(1)))
    return names


def _has_write(body: str) -> bool:
    if re.search(rf"method:\s*'(?:{'|'.join(WRITE_METHODS)})'", body):
        return True
    # 直接 req('...', { body: ... })：默认方法就是 POST。
    if re.search(r"\breq\(\s*[`'\"][^`'\"]*[`'\"]\s*,\s*\{[^}]*\bbody:", body, re.S):
        return True
    return any(re.search(rf"\.\s*{name}\s*\(", body) for name in API_WRITE)


# Firewall 和 Sites 在页面里自成一个 api 小对象（`const api = { add: () => req(...) }`），
# 写请求藏在包装里，处理函数本身只看得到 `api.add(body)`；只看函数体就会整页漏掉。
_LOCAL_API = re.compile(r"const api = \{([\s\S]*?)^\};", re.M)
_LOCAL_KEY = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*\(", re.M)


def _local_write_names(text: str, funcs: dict) -> set:
    names = {name for name, body in funcs.items() if _has_write(body)}
    for m in _LOCAL_API.finditer(text):
        block = m.group(1)
        keys = list(_LOCAL_KEY.finditer(block))
        for i, km in enumerate(keys):
            end = keys[i + 1].start() if i + 1 < len(keys) else len(block)
            chunk = block[km.start():end]
            if not ("req(" in chunk or "request(" in chunk):
                continue
            meths = re.findall(r"method:\s*'([A-Z]+)'", chunk)
            if any(x in WRITE_METHODS for x in meths) or (not meths and re.search(r"\bbody:", chunk)):
                names.add(km.group(1))
    return names


def _calls_write(body: str, local_names: set, self_name: str) -> bool:
    """本函数自己发写请求，或者调了一个会发写请求的本地包装（自己不算自己）。"""
    if _has_write(body):
        return True
    return any(
        re.search(rf"\b{re.escape(n)}\s*\(", body)
        for n in local_names
        if n != self_name
    )


def _raised_flags(body: str) -> set:
    """这个函数自己置成"忙"的 ref：赋了一个非假值。

    `=` 后面排除 `=` 和 `>`：否则 `if (type.value === 'hash')` 这种比较会被当成赋值。
    右边是对象/数组字面量的通常不算——那是表单和选中集在重置（dbForm、checked），
    跟"在途标志"没有关系，把它们当标志会让参数校验冒充早退。
    例外是名字本身就带忙语义的（`busy.value = { unit, verb }`）：带载荷的在途标记靠赋值
    区分不开，只能靠名字认。
    """
    out = set()
    for m in re.finditer(r"([A-Za-z][A-Za-z0-9_]*)\.value\s*=(?!=|>)(.*)", body):
        name, rhs = m.group(1), m.group(2).strip()
        if FALSY.match(rhs):
            continue
        if rhs.startswith(("{", "[")) and not BUSY_WORDS.search(name):
            continue
        out.add(name)
    return out


def _code_prefix(body: str) -> str:
    """第一个 await 之前的代码（去空行与注释）。早退只有在这里才算数——第一个请求已经发出，
    第二次点击就已经在服务端排队了。"""
    kept = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        kept.append(s)
        if "await " in s:
            break
    return "\n".join(kept)


def _if_returns(prefix: str):
    """前缀里所有 `if (…) return` 的条件文本。"""
    out = []
    idx = prefix.find("if (")
    while idx != -1:
        close = _pair(prefix, idx + 3, "(", ")")
        if close is not None:
            tail = prefix[close + 1: close + 60].lstrip()
            if tail.startswith("return") or (tail.startswith("{") and "return" in tail):
                out.append(prefix[idx + 4: close])
        idx = prefix.find("if (", idx + 1)
    return out


# 在途标志的命名范式：早退条件里的"忙碌判定"函数（rowBusy、opBusy）和集合（alertInflight）
# 都靠它认。不合范式的条件不当作早退——`if (isNaN(v)) return`、`if (!name) return` 这种参数
# 校验挡不住第二次点击。将来若真用了别的名字，这条会红而不是默默放过，改名清单照着补即可。
BUSY_WORDS = re.compile(
    r"busy|loading|saving|running|acking|acting|creating|uploading|restoring|deleting"
    r"|submitting|probing|auditing|scanning|sampling|renewing|reprofil|fixing|checking"
    r"|testing|editing|toggling|inflight|queued|pending",
    re.I,
)


def _guard(prefix: str, raised: set):
    """挑一条真正的在途早退：条件要么引用了本函数置过忙的 ref，要么名字就在在途范式里。

    只看"置过忙"是不够的：`busy.value = { unit, verb }` 这种带载荷的标记右边是个对象，
    和数据重置（dbForm.value = { name: '' }）长得一样，靠赋值区分不开，得靠名字。
    """
    for cond in _if_returns(prefix):
        refs = re.findall(r"([A-Za-z][A-Za-z0-9_]*)\.value", cond)
        calls = re.findall(r"([A-Za-z][A-Za-z0-9_.]*)\s*\(", cond)
        named = [r for r in refs if BUSY_WORDS.search(r)]
        if set(refs) & raised or named or (not refs and any(BUSY_WORDS.search(c) for c in calls)):
            return {"cond": cond, "refs": refs, "guards": refs + calls, "via": None}
    return None


def _analyze(path: Path):
    """name -> {"body","guard"}，只收录「点了会发写请求」的入口。两遍扫描：先找出自带早退的
    本地包装（RedisKeyPanel 的 act），再判各入口——第一个 await 就交给这类包装的写法，
    闸做在包装里，标志也跟着包装走。"""
    text = _src(path)
    funcs = _func_slices(text)
    wired = _wired_names(text)
    local_writes = _local_write_names(text, funcs)
    raised = {name: _raised_flags(body) for name, body in funcs.items()}
    guards = {}
    for name, body in funcs.items():
        guards[name] = _guard(_code_prefix(body), raised[name])

    out = {}
    for name, body in funcs.items():
        if name not in wired or not _calls_write(body, local_writes, name):
            continue
        prefix = _code_prefix(body)
        guard = _guard(prefix, raised[name])
        if guard is None:
            m = re.search(r"await\s+([A-Za-z][A-Za-z0-9_]*)\s*\(", prefix)
            delegate = m and m.group(1) in guards and guards[m.group(1)]
            if delegate:
                guard = {
                    "cond": f"经由 {m.group(1)}() 的早退",
                    "refs": delegate["refs"],
                    "guards": delegate["guards"],
                    "via": m.group(1),
                }
                raised[name] = raised[m.group(1)]
        if guard is None and "acceptParams(" in prefix:
            # 请求在 ConfirmActionDialog 的 run() 之后才发，那里已经自带闸
            guard = {"cond": "经由 ConfirmActionDialog", "refs": [], "guards": ["acceptParams"], "via": "dialog"}
        out[name] = {"body": body, "guard": guard, "raised": raised[name]}
    return text, out


# 逐条说明理由，而不是笼统豁免。
LOCAL_ONLY_EXEMPT = {
    # 终端里的命令是给 PTY 的输入，没有「在途请求」这个概念，服务端也没有对应的幂等闸。
    ("Terminal.vue", "submitCmd"),
}


class TestWriteHandlersGuarded(unittest.TestCase):
    def test_every_wired_mutation_early_returns_before_first_await(self):
        for path in (*VIEWS, *COMPS):
            _text, handlers = _analyze(path)
            for name, info in handlers.items():
                if (path.name, name) in LOCAL_ONLY_EXEMPT:
                    continue
                with self.subTest(file=path.name, handler=name):
                    self.assertIsNotNone(
                        info["guard"],
                        "第一个 await 之前必须对在途标志早退：disabled 挡不住回车提交和"
                        "程序化调用，连点的第二次请求照样发得出去",
                    )

    def test_api_write_names_are_actually_discovered(self):
        """解析器一坏，上面那条就退化成永远为真——先钉住它确实认出了几个写接口。"""
        for name in ("deleteUser", "updateBackup", "deleteApp", "saveUnit"):
            self.assertIn(name, API_WRITE, "api.ts 的写接口没被识别，守卫形同虚设")
        self.assertNotIn("apps", API_WRITE, "读接口被误判成写接口，守卫会开始误报")

    def test_parser_recognises_the_known_wrappers(self):
        """委托豁免的依据：闸做在包装里。包装自己不在模板里出现，所以认的是"入口指向它"。"""
        _text, handlers = _analyze(WEB / "components" / "RedisKeyPanel.vue")
        for name in ("saveString", "addMember", "delMember"):
            self.assertEqual(
                handlers[name]["guard"]["via"],
                "act",
                f"{name} 没有把互斥交给 act()，豁免就指向了空气",
            )


class TestGuardedHandlersHaveDisabledBinding(unittest.TestCase):
    def test_control_is_disabled_while_flag_is_set(self):
        for path in (*VIEWS, *COMPS):
            text, handlers = _analyze(path)
            for name, info in handlers.items():
                if (path.name, name) in LOCAL_ONLY_EXEMPT:
                    continue
                if not info["guard"] or info["guard"]["via"] == "dialog":
                    continue
                if info["guard"]["via"] not in (None, "dialog") and not info["guard"]["refs"]:
                    # 委托给"按 key 排队保存"的包装（Settings 阈值）：那种入口的控件本来就该
                    # 继续能用，最后一次的值得赢，置灰反而是错的。
                    continue
                tokens = set(info["guard"]["refs"]) | set(info["guard"]["guards"])
                with self.subTest(file=path.name, handler=name):
                    self.assertTrue(
                        self._bound(text, name, tokens),
                        "早退有了，按钮却还亮着：用户看不见「现在不能点」，只会把它当成卡住",
                    )

    @staticmethod
    def _bound(text: str, name: str, tokens: set) -> bool:
        # 模板入口：@click="name(...)" 所在标签里有 :disabled。
        for m in _BIND_TEMPLATE.finditer(text):
            if not re.search(rf"\b{re.escape(name)}\s*\(?", m.group(1)):
                continue
            lo = text.rfind("<", 0, m.start())
            hi = text.find(">", m.end())
            tag = text[lo: hi if hi != -1 else len(text)]
            if ":disabled" in tag:
                return True
        # 渲染函数入口（h(NButton, { onClick: () => act(...) })）：同一段里带 disabled:。
        for m in _BIND_SCRIPT.finditer(text):
            if not re.search(rf"\b{re.escape(name)}\s*\(", m.group(1)):
                continue
            window = text[max(0, m.start() - 500): m.end() + 500]
            if re.search(r"disabled\s*:", window):
                return True
        # 标志本身出现在某个 disabled 表达式里，也算绑定到位。
        return any(re.search(rf"disabled[^\n]*\b{re.escape(t)}\b", text) for t in tokens)


class TestBusyFlagIsReset(unittest.TestCase):
    """早退把第二次点击挡在本地，前提是第一次请求结束时标志一定被清。

    漏了 finally 的写法会让按钮永久置灰——比重复提交更难发现，因为不刷新就恢复不了。
    """

    def test_raised_flags_get_a_falsy_reset(self):
        for path in (*VIEWS, *COMPS):
            text, handlers = _analyze(path)
            for name, info in handlers.items():
                if not info["guard"]:
                    continue  # 没有早退的入口由上一条测试管，这里不重复报
                # 只问在途标志：早退查它、这个函数又把它置成忙，那就必须有地方清掉。
                # 其余 `.value = …` 是数据赋值（sqlOut、err），清不清是另一回事。
                for flag in sorted(set(info["raised"]) & set(info["guard"]["refs"])):
                    with self.subTest(file=path.name, flag=flag, handler=name):
                        self.assertRegex(
                            text,
                            rf"{re.escape(flag)}\.value\s*=\s*(?:''|\"\"|null|false|0)(?![\w])",
                            "在途标志没有置空的地方，请求失败后按钮会一直灰着",
                        )


class TestSeqGuardedLoaders(unittest.TestCase):
    def test_table_listing_carries_seq_guard(self):
        text = _src(WEB / "views" / "Databases.vue")
        self.assertIn("let tablesSeq = 0;", text)
        self.assertIn("const seq = ++tablesSeq;", text)
        self.assertGreaterEqual(
            text.count("if (seq !== tablesSeq) return;"),
            2,
            "切库时旧响应会后到并把上一个库的表名盖上来，两处 await 之后都要判 seq",
        )

    def test_journal_search_carries_seq_guard(self):
        text = _src(WEB / "views" / "LogCenter.vue")
        self.assertIn("let journalSeq = 0;", text)
        self.assertIn("const seq = ++journalSeq;", text)
        self.assertIn("if (seq !== journalSeq) return;", text)

    def test_row_page_loaders_share_the_seq_pattern(self):
        """loadTables 是照着 loadTableData 的既有范式补的，别把范式本身弄丢。"""
        text = _src(WEB / "views" / "Databases.vue")
        self.assertIn("let tableSeq = 0;", text)
        self.assertIn("const seq = ++tableSeq;", text)


class TestConfirmDialogChoke(unittest.TestCase):
    def test_shared_dialog_guards_and_disables(self):
        text = _src(WEB / "components" / "ConfirmActionDialog.vue")
        self.assertRegex(
            text,
            r"async function run\(\)\s*\{\s*\n\s*if \(!p\.value \|\| loading\.value\) return;",
            "所有破坏性操作都走这个弹窗，它的 run() 是最后一道连点闸",
        )
        self.assertIn(':loading="loading"', text)
        self.assertIn(':disabled="loading"', text)


class TestRefreshDedupeExemption(unittest.TestCase):
    def test_cached_fetch_merges_concurrent_reads(self):
        """只读刷新按钮没有早退是有依据的：并发 GET 在 composable 里合并成一次往返。
        这条依据一旦失效（inflight 被删），豁免就不成立了。"""
        text = _src(WEB / "composables" / "useCachedFetch.ts")
        self.assertIn("inflight", text)


if __name__ == "__main__":
    unittest.main()
