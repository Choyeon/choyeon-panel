"""批次㉟：折行这件事必须只有一个出处，日志盒与颜色变量跟着一起收口。

线上（2026-10-15，批次㉞产物）逐页查到的形态缺陷，四条同源问题：

1. **组件里再写一遍 nowrap，会把全局的换行类按回去。**
   构建产物里 style.css 排在各组件 scoped 样式之前，特异度又相同（`.cp-prose.cp-prose`
   是 0-2-0，`.info-v[data-v-x]` 也是 0-2-0），后写的组件规则必然赢。于是
   InfoList 的取值、概览的告警判词与进程名、服务页的生效值/指纹、应用详情的标题
   这十一处挂着 `.cp-prose`/`.cp-break` 的格子，其实一个字都没换行——
   手机上没有 hover，`title` 那条退路等于不存在，被裁掉的就是结论本身。
2. **`.cr-log` 是 `.log-view` 的两份逐字副本**（备份页、定时任务页各一份，
   连 14px/16px 的紧凑行高一起抄），六块日志区里只有这两块不是同一个形态。
3. **`var(--cp-warn-soft, rgba(…))` 里那个变量全站无人定义**，日志检索命中的高亮
   永远走内联兜底色；反过来 `--cp-brand-grad-from` 定义了三次却一次都没被用。
4. **16 处浮层封顶都是 94vw，只有 ConfirmActionDialog 写成 92vw**——守卫只查
   「有没有 maxWidth」，不查值，所以它一直活着。

本批把这些都改成一处定义，并加守卫：挂全局换行类的规则体里不许再出现
white-space / text-overflow；`--cp-*` 的使用与定义必须双向对齐；vw 封顶只认 94。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT.parent / "web" / "src"
STYLE = (WEB / "style.css").read_text(encoding="utf-8")
SRC_FILES = sorted([p for p in WEB.rglob("*") if p.suffix in {".vue", ".ts", ".css"}])


def src(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def rule_body(text: str, selector: str) -> str:
    """取裸选择器对应的规则体（只认第一条命中）。找不到就失败——守卫不能空转。"""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text, flags=re.S)
    assert m, f"选择器 {selector!r} 已经不在源码里"
    return m.group(1)


# (文件, 选择器, 该格子挂的全局换行类)
WRAP_CELLS = [
    ("components/InfoList.vue", ".info-v", "cp-prose"),
    ("views/Dashboard.vue", ".alert-msg", "cp-prose"),
    ("views/Dashboard.vue", ".proc-name", "cp-break"),
    ("views/Dashboard.vue", ".ov-note", "cp-prose"),
    ("views/Dashboard.vue", ".app-name", "cp-prose"),
    ("views/AppDetail.vue", ".ad-title", "cp-prose"),
    ("views/AppDetail.vue", ".ngx-name", "cp-prose"),
    ("views/AppDetail.vue", ".ngx-dom", "cp-prose"),
    ("views/AppDetail.vue", ".dep-item :deep(.n-space > *:first-child)", "cp-break"),
    ("views/Services.vue", ".cp-tbl :deep(.ls-value)", "cp-break"),
    ("views/Services.vue", ".cp-tbl :deep(.ls-fp)", "cp-break"),
]


class TestWrapHasOneOwner(unittest.TestCase):
    """挂全局换行类的格子，本地规则只许写布局，不许再写折行方式。"""

    def test_local_rule_does_not_redeclare_line_breaking(self):
        for rel, sel, _cls in WRAP_CELLS:
            with self.subTest(file=rel, selector=sel):
                body = rule_body(src(rel), sel)
                for prop in ("white-space", "text-overflow"):
                    self.assertNotIn(
                        prop,
                        body,
                        f"{rel} 的 {sel} 又写了一遍 {prop}；组件 scoped 排在 style.css 之后、"
                        "同特异度时会把全局换行类按回去（这就是那十一处『声明了换行却没换』的成因）",
                    )

    def test_cells_actually_carry_the_global_class(self):
        """删掉 nowrap 却不挂换行类 = 长串直接把容器撑破，所以挂点也要钉住。"""
        needles = {
            "components/InfoList.vue": ['class="info-v cp-prose"'],
            "views/Dashboard.vue": [
                'class="alert-msg cp-prose"',
                'class="proc-name cp-break"',
                'class="ov-note cp-prose"',
                'class="app-name cp-prose"',
            ],
            "views/AppDetail.vue": [
                'class="ad-title cp-prose"',
                'class="ngx-name cp-prose"',
                'class="ngx-dom cp-prose"',
                'class="cp-break"',
            ],
            "views/Services.vue": ["'mono cp-break ls-value'", "'mono cp-break ls-fp'"],
        }
        for rel, want in needles.items():
            text = src(rel)
            for needle in want:
                with self.subTest(file=rel, needle=needle):
                    self.assertIn(needle, text, f"{rel} 里没有 {needle} 这个挂点")

    def test_global_classes_are_still_double_class(self):
        """0-2-0 是这套打法的全部依据：改成单类名就又被组件样式压回去。"""
        for cls in ("cp-break", "cp-prose"):
            with self.subTest(cls=cls):
                self.assertIn(f".{cls}.{cls} {{", STYLE, f".{cls} 必须是双类名选择器")

    def test_narrow_screen_no_longer_repeats_the_wrap_override(self):
        """窄屏档里那条 `.ls-fp { white-space: normal … }` 是给 nowrap 打的补丁，
        出处收口之后它成了第二份定义——删掉，避免下一次改换行又要改三个地方。"""
        text = src("views/Services.vue")
        for block in re.findall(r"@media \(max-width: 767px\) \{(.*?)\n\}", text, flags=re.S):
            self.assertNotIn(".ls-fp", block, "窄屏档不该再单独管 ls-fp 的折行")


class TestLogBoxSingleSource(unittest.TestCase):
    def test_cr_log_copy_is_gone(self):
        for path in SRC_FILES:
            with self.subTest(file=path.name):
                self.assertNotIn("cr-log", path.read_text(encoding="utf-8"),
                                 ".cr-log 是 .log-view 的逐字副本，已经并回共享类")

    def test_both_drawers_use_the_shared_box(self):
        for rel in ("views/Backups.vue", "views/CronTasks.vue"):
            with self.subTest(file=rel):
                self.assertIn('class="log-view log-drawer"', src(rel))

    def test_shared_box_owns_the_appearance(self):
        body = rule_body(STYLE, ".log-view")
        for prop in ("background", "color", "padding", "border", "overflow", "white-space", "word-break"):
            self.assertIn(prop, body, f"日志盒的外观应当只在 .log-view 一处定义，缺 {prop}")

    def test_drawer_rule_only_sets_the_height(self):
        body = rule_body(STYLE, ".log-drawer")
        self.assertEqual(
            sorted(re.findall(r"([a-z-]+):", body)),
            ["height", "margin"],
            "抽屉里只补高度与 <pre> 边距，多余的外观声明就是第二份定义",
        )
        self.assertIn("max(220px, calc(var(--viewport-h) - 420px))", body,
                      "裸 calc() 在 420px 高的窗口（手机横屏）会算出负数、日志盒塌成 0；"
                      "视口高度也只能走 --viewport-h（批次64 起全站一个出处，@supports 里换成 100dvh）")


class TestTokenContract(unittest.TestCase):
    """自定义属性必须双向对齐：引用要有定义，定义要有人引用。

    `(?<![\\w-])` 是必需的——naive 的 BEM 类名（`.n-menu-item-content--selected::before`）
    长得就像一条属性声明，不排掉会把类名当成"定义了没人用"的变量。
    """

    DEF = re.compile(r"(?<![\w-])(--[a-z0-9-]+)\s*:")
    USE = re.compile(r"var\(\s*(--[a-z0-9-]+)")

    def _pairs(self):
        used, defined = set(), set()
        for path in SRC_FILES:
            text = path.read_text(encoding="utf-8")
            used |= set(self.USE.findall(text))
            defined |= set(self.DEF.findall(text))
        return used, defined

    def test_every_used_property_is_defined(self):
        used, defined = self._pairs()
        self.assertFalse(
            used - defined,
            f"引用了没人定义的变量，var() 会静默走兜底值：{sorted(used - defined)}",
        )

    def test_no_dead_property_is_left_defined(self):
        used, defined = self._pairs()
        self.assertFalse(
            defined - used,
            f"定义了却没人 var() 的变量（改这里不会生效，纯粹误导）：{sorted(defined - used)}",
        )

    def test_legacy_token_set_does_not_come_back(self):
        """这批清掉的假契约：主题色五枚 + 层级标度七枚 + 侧栏两枚 + 菜单高度 + 三处 shadow-card，
        还有 --fs-2xl 和重复声明的 --fs-3xl。它们都只有定义没有使用。"""
        for dead in ("--brand:", "--brand-soft:", "--ok:", "--warn:", "--err:",
                     "--z-modal:", "--z-drawer:", "--sidebar-w:", "--menu-item-h:",
                     "--shadow-card:", "--fs-2xl:", "--cp-brand-grad-from:"):
            with self.subTest(token=dead):
                self.assertNotIn(dead, STYLE, f"{dead} 是没人 var() 的死变量")
        self.assertEqual(STYLE.count("--fs-3xl:"), 1, "--fs-3xl 声明过两次（同值）")

    def test_log_highlight_token_exists_in_both_themes(self):
        """日志检索命中高亮原先写 var(--cp-warn-soft, rgba(…))，那个变量全站没定义。"""
        self.assertNotIn("--cp-warn-soft", STYLE)
        self.assertIn("var(--cp-code-hl)", src("views/LogCenter.vue"))
        self.assertEqual(STYLE.count("--cp-code-hl:"), 2, "命中高亮必须有明暗两档")


class TestOverlayWidthCap(unittest.TestCase):
    def test_all_caps_are_94vw(self):
        vals = set()
        for path in SRC_FILES:
            text = path.read_text(encoding="utf-8")
            vals |= set(re.findall(r"max-width:\s*(\d+)vw", text))
            vals |= set(re.findall(r"maxWidth:\s*'(\d+)vw'", text))
        self.assertEqual(vals, {"94"}, "浮层封顶全站只有一个值，ConfirmActionDialog 的 92vw 是漏网的")

    def test_pagination_item_meets_touch_target(self):
        body = rule_body(STYLE, ".n-pagination-item")
        self.assertIn("min-width: 44px", body, "翻页按钮停在 naive 默认的 38px，够不到 44 的拇指目标")

    def test_dead_mask_classes_are_gone(self):
        for cls in ("cp-masked", "cp-mask-prompt"):
            for path in SRC_FILES:
                with self.subTest(cls=cls, file=path.name):
                    self.assertNotIn(cls, path.read_text(encoding="utf-8"),
                                     "前置条件遮蔽用的是 EmptyBox，这两条规则全站没人挂")
