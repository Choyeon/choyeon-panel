"""批次㉕：被裁掉的全文不能只住在 hover 里，而声明换行的那一行必须先打赢 nowrap。

两条都是 375px 登录态实测出来的（同源离屏 iframe，2026-10-14）：

1. 告警内容那一列写的是 `ellipsis: { tooltip: true }`。最长的一条被裁掉 **155px**
   （「应用 p5fprobe 无响应（127.0.0.1:8124，ConnectionRefused…）」在 192px 的列里
   只剩前几个字），而 `.n-ellipsis` 这个节点**连 title 都没有**——桌面靠 hover 还能看全，
   手机上没有 hover，那半句就再也没有第二个入口。同一形状分布在告警、审计、网站、
   用户、Redis 数据浏览器五处。
2. `.cp-break` 与 `.mono-dim`/`.cell-sub` 并用的格子**一个字都没换行**：两条规则同权重、
   靠先后顺序决胜，而 `.cp-break` 写在它们前面，于是 `white-space: nowrap` 赢。
   受影响的是 cron 的命令行与表达式、日志页的 unit 名、设置页的单元名——注释里都写着
   「这里必须换行」。告警页的来源码还多一种死法：内联 `wordBreak: 'break-all'` 只赢
   word-break 这一条，nowrap 仍由类提供。

改法分三档，档位由「这张表能不能变行高」决定：
- 普通表格里的整句话 → `.cp-prose`（三行封顶，折行本身看得见，title 退成桌面端补充）。
- 开了 `virtual-scroll` 的表（审计、文件、日志、Redis 数据）→ 全文入口从 hover 换成点按
  （`tapEllipsis`），格子不放三行封顶的句子；单个长 token（unit 名、哈希）仍可 `.cp-break`。
- 数据浏览的 PG 行 → 点整行就打开行详情，那里本来就有全文，不再加一层浮层。

七件事由用例锁住：打赢 nowrap 的写法只有一个出处、`.cp-break` 不再靠顺序、
`.cp-prose` 只定义一次且旧副本清空、hover-only 的截断不许是唯一入口、
`tapEllipsis` 只有一个定义且不许就地重抄、virtual-scroll 的表里不许出现三行封顶的格子、
内联的 break-all 不再冒充换行声明。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT.parent / "web" / "src"
STYLE = (WEB / "style.css").read_text(encoding="utf-8")
VIEWS = sorted((WEB / "views").glob("*.vue"))
COMPS = sorted((WEB / "components").glob("*.vue"))
UI_TS = (WEB / "ui.ts").read_text(encoding="utf-8")


def src_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def all_vue_text() -> str:
    return "".join(src_of(p) for p in (*VIEWS, *COMPS))


def rule_of(selector: str) -> str:
    """取样式块里某条规则的声明体。"""
    m = re.search(rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", STYLE, flags=re.M)
    assert m, f"style.css 里没有 {selector} 这条规则"
    return m.group(1)


class TestBreakWinsNowrap(unittest.TestCase):
    """`.cp-break` 现在必须真的换行，而且不靠样式块的先后顺序。"""

    def test_selector_is_doubled(self):
        """双类名（0-2-0）才与顺序无关；单类名那条已经死了五处。"""
        body = rule_of(".cp-break.cp-break")
        self.assertIn("word-break: break-all", body)
        self.assertIn("white-space: pre-wrap", body)

    def test_order_dependent_copy_is_gone(self):
        """不许再留一条单类名的 `.cp-break`：它会跟 `.mono-dim` 抢顺序，抢输就静默失效。"""
        self.assertIsNone(re.search(r"(?:^|\})\s*\.cp-break\s*\{", STYLE, flags=re.M))

    def test_pre_wrap_not_normal(self):
        """cron 表达式与命令行里的空格有意义，`normal` 会把它们合并掉。"""
        self.assertIn("pre-wrap", rule_of(".cp-break.cp-break"))

    def test_no_inline_word_break_pretending_to_wrap(self):
        """内联 `wordBreak: 'break-all'` 是最容易写错的一种：它只赢这一条属性。"""
        for path in (*VIEWS, *COMPS):
            with self.subTest(file=path.name):
                self.assertNotIn("wordBreak: 'break-all'", src_of(path), f"{path.name} 又用内联声明换行")

    def test_cron_and_log_cells_still_ask_for_the_class(self):
        """反证用的锚点：这些格子必须还在用 .cp-break，否则上面两条守卫会跟着变绿但没人换行。"""
        cron = src_of(WEB / "views" / "CronTasks.vue")
        log = src_of(WEB / "views" / "LogCenter.vue")
        self.assertGreaterEqual(cron.count("cp-break"), 3, "cron 的表达式/命令/Timer 该走 .cp-break")
        self.assertIn("mono-dim cp-break", log, "日志页的 unit 名该走 .cp-break")


class TestProseCellsWrap(unittest.TestCase):
    """.cp-prose 是「整句话的格子」唯一的折行出处。"""

    def test_defined_once_with_three_line_cap(self):
        body = rule_of(".cp-prose.cp-prose")
        self.assertIn("-webkit-line-clamp: 3", body)
        self.assertIn("white-space: normal", body)
        self.assertEqual(STYLE.count(".cp-prose.cp-prose {"), 1)

    def test_no_single_class_copy(self):
        self.assertIsNone(re.search(r"(?:^|\})\s*\.cp-prose\s*\{", STYLE, flags=re.M))

    def test_old_doctor_copy_is_consolidated(self):
        """批次㉔ 那份 `.cell-sub.wrap` 规则并到 .cp-prose：折行只留一个出处。

        查的是「这条规则还在不在」（带选择器体），不是字符串——注释里提它是有意留的线索，
        但一条真的会生效的副本必须清掉。
        """
        self.assertIsNone(re.search(r"\.cell-sub\.wrap\s*\{", STYLE), "style.css 里还有一份 .cell-sub.wrap 规则")
        self.assertNotIn("cell-sub wrap", all_vue_text())
        self.assertIn("cell-sub cp-prose", src_of(WEB / "views" / "Doctor.vue"))

    def test_prose_columns_use_the_class_not_a_hover_tooltip(self):
        for name, needle in (
            ("Alerts.vue", "class: 'cp-prose', title: r.message"),
            ("Sites.vue", "mono-dim cp-prose"),
            ("Users.vue", "class: 'cp-prose'"),
            ("Apps.vue", "cell-sub cp-prose"),
            ("BackupFiles.vue", "cell-sub cp-prose"),
            ("Services.vue", "cell-sub cp-prose"),
        ):
            with self.subTest(file=name):
                self.assertIn(needle, src_of(WEB / "views" / name))

    def test_title_without_a_wrap_class_is_gone_from_plain_cells(self):
        """带 title 的 `.cell-sub`/`.mono-dim` 就是「作者承认会被裁」；那必须同时给折行。

        例外各有第二条通道，写在这里而不是悄悄跳过：
        - Audit / Files / LogCenter 开着 `virtual-scroll`，整句话的格子不许折成三段，
          全文走点按浮层（`tapEllipsis`）；
        - RedisKeyPanel 的数据格同理（`tapEllipsis`），值再长也不该把行高撑成三段；
        - Firewall 的「受保护」：title 里那句理由在页面上方逐条列明了（受保护端口那张表），
          格子里可见的只有两个字，本来就没被裁。
        """
        exempt = {"Audit.vue", "Files.vue", "LogCenter.vue", "RedisKeyPanel.vue", "Firewall.vue"}
        offenders = []
        for path in (*VIEWS, *COMPS):
            if path.name in exempt:
                continue
            for m in re.finditer(r"class: '(cell-sub|mono-dim)([^']*)', title:", src_of(path)):
                if "cp-prose" not in m.group(2) and "cp-break" not in m.group(2):
                    offenders.append(f"{path.name}: {m.group(0)[:44]}")
        self.assertEqual([], offenders, "这些格子只有 hover 才看得全：" + "；".join(offenders))


class TestNoHoverOnlyEllipsis(unittest.TestCase):
    """hover-only 的截断不许当唯一入口：要么改成点按浮层，要么这一页得有整行点击通道。"""

    # 只有这一处允许 `tooltip: true`：数据浏览的两张表，格子被裁的全文由「点整行」交付
    # （行详情 / 键详情，里面是完整值 + 可编辑）。允许的前提是那条通道真的在，
    # 所以下面那个用例把它和 tooltip 绑在一起断言，而不是写死一份豁免名单。
    TAP_CHANNEL_FILES = frozenset({"Databases.vue"})

    def test_hover_only_form_never_stands_alone(self):
        for path in (*VIEWS, *COMPS):
            text = src_of(path)
            if "tooltip: true" not in text:
                continue
            with self.subTest(file=path.name):
                self.assertIn(path.name, self.TAP_CHANNEL_FILES, f"{path.name} 的截断只有 hover 一个入口")
                self.assertIn(":row-props=", text, f"{path.name} 的整行点击通道没了，tooltip 就又变成 hover 专属")

    def test_tap_ellipsis_is_defined_once(self):
        self.assertEqual(UI_TS.count("export const tapEllipsis"), 1)
        self.assertIn("trigger: 'click'", UI_TS)

    def test_no_local_reimplementation(self):
        """触发方式只准写在 ui.ts 那一处，别处再抄一份就会漂移。"""
        for path in (*VIEWS, *COMPS):
            with self.subTest(file=path.name):
                self.assertNotIn("trigger: 'click'", src_of(path))

    def test_consumers_import_the_shared_const(self):
        for rel in (WEB / "views" / "Audit.vue", WEB / "components" / "RedisKeyPanel.vue"):
            with self.subTest(file=rel.name):
                text = src_of(rel)
                self.assertIn("ellipsis: tapEllipsis", text)
                self.assertRegex(text, r"import \{[^}]*tapEllipsis[^}]*\} from '\.\./ui'")

    def test_databases_keeps_the_row_tap_as_the_channel(self):
        """PG 数据浏览的截断不改成点按浮层：点整行本来就打开行详情（全文在那里）。"""
        text = src_of(WEB / "views" / "Databases.vue")
        self.assertIn(":row-props=\"rowProps\"", text)
        self.assertIn(":row-props=\"keyRowProps\"", text)


class TestVirtualTablesStayOneLine(unittest.TestCase):
    """virtual-scroll 的表禁 `.cp-prose`（三行封顶的整句话），但留 `.cp-break`（单个长 token）。

    差别在行高的**数量级**，不是在「能不能折行」。naive-ui 的
    `data-table/src/TableParts/Body.mjs` 传的是 `itemResizable: !virtualScrollX`，
    纵向虚拟列表会实测每一行的真实高度，所以偶发折一行不会裁字或算错范围；
    但一列里同时有 1 行和 3 行的句子（`.cp-prose` 的 clamp），行高就成倍抖动，
    滚动条位置跟着估不准。长 unit 名（`docker-<64hex>.scope`）必须看得全，
    所以日志页的来源仍走 `.cp-break`。
    """

    VIRTUAL = ("Audit.vue", "Files.vue", "LogCenter.vue")

    def test_no_clamped_cell_inside_a_virtual_table(self):
        for name in self.VIRTUAL:
            text = src_of(WEB / "views" / name)
            with self.subTest(file=name):
                if "virtual-scroll" not in text:
                    self.skipTest(f"{name} 已不再用 virtual-scroll")
                self.assertNotIn("cp-prose", text, f"{name} 开着 virtual-scroll，不许放三行封顶的格子")

    def test_audit_detail_routes_through_the_shared_const(self):
        text = src_of(WEB / "views" / "Audit.vue")
        self.assertIn("ellipsis: tapEllipsis", text)
        self.assertIn("virtual-scroll", text)


class TestShippedCssShape(unittest.TestCase):
    """构建产物里这两条规则得长成同一个形状（scoped/data-v 的坑已经踩过一次）。"""

    @staticmethod
    def css_chunks():
        dist = WEB.parent / "dist" / "assets"
        return sorted(dist.glob("*.css")) if dist.exists() else []

    def test_built_css_keeps_both_doubled_selectors(self):
        chunks = self.css_chunks()
        if not chunks:
            self.skipTest("本机没有构建产物（web/dist），线上复验另行执行")
        joined = "".join(src_of(p) for p in chunks)
        for sel in (".cp-break.cp-break", ".cp-prose.cp-prose"):
            with self.subTest(selector=sel):
                self.assertIn(sel, joined.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
