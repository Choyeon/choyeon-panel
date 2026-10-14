"""批次㉗：写死的像素宽度在 375px 手机档的三种死法，全部收口并钉住。

登录态 375px 同源离屏实测（2026-10-14，批次㉖ 产物）确认的缺陷面：

1. `.json-tip { max-width: 420px }`——naive 的 tooltip 浮层不跟着视口缩，
   420px 的预格式化块在手机上顶出屏幕右侧约 45px，而浮层里没法横滚，
   JSON 开头几列字符直接看不着。
2. 概览进程表 `.proc-row` 是 `56px minmax(0,1fr) 72px 64px 72px` 的五列硬 grid，
   375px 下固定四列 + 4×8 gap 吃掉 296px，进程名只剩 ~20px——整列白搭。
3. 应用详情环境变量 KEY 框写死 `width: 220px`，同排两枚圆形按钮 ~64px，
   `flex: 1` 的值栏实测仅剩 ~30px，环境变量在手机上根本没法编辑。
   （这条不是溢出，是被兄弟挤死，同样算缺陷。）
4. Redis 统计格 `cols="2 s:4"` 最小档还是两列，~150px 的小卡里
   「standalone / 面板密码」这类组合值折行折得没法读。
5. 全站 52 处工具条/表单控件写 `style="width: NNNpx"` 而不带 `max-width: 100%`——
   单件如今都 <343 没顶穿，但 Apps.vue 早已确立 `width+max-width:100%` 的范式，
   漏网的等着下一次容器收窄来引爆。统一补齐，并把「凡固定 px 内联宽度必须封顶」
   升成守卫。

KEY 框的修法与封顶不同：它要的是随容器缩放，`clamp(110px, 38%, 220px)`；
守卫单独认这一处，防止有人把它改回 220px 字面量。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT.parent / "web" / "src"
STYLE = (WEB / "style.css").read_text(encoding="utf-8")
VIEWS = sorted((WEB / "views").glob("*.vue"))
COMPS = sorted((WEB / "components").glob("*.vue"))


def src_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def view(name: str) -> str:
    return src_of(WEB / "views" / f"{name}.vue")


class TestInlinePxWidthCapped(unittest.TestCase):
    def test_every_fixed_px_inline_width_carries_a_cap(self):
        """凡 `style="width: NNNpx"` 起手且以引号收尾的内联宽度，必须同块带 max-width——
        工具条范式是 100%，浮层范式是 94vw，两者都算封顶。"""
        pat = re.compile(r'style="width: \d+px[^"]*"')
        for path in (*VIEWS, *COMPS):
            for block in pat.findall(src_of(path)):
                with self.subTest(file=path.name, block=block[:70]):
                    self.assertIn(
                        "max-width",
                        block,
                        "固定像素宽度不封顶，容器一收窄就顶穿视口（Apps.vue 的范式）",
                    )

    def test_vue_files_do_not_start_new_uncapped_pattern(self):
        """`:style` 动态绑定不在守卫范围（值是运行期拼的），但静态新写法必须走范式。"""
        for path in (*VIEWS, *COMPS):
            for block in re.findall(r':style="\{[^"]*\}"', src_of(path)):
                with self.subTest(file=path.name, block=block[:70]):
                    if re.search(r"width: ?\d+px", block):
                        self.assertIn("maxWidth", block,
                                      "动态样式里写死像素宽度同样要封顶")


class TestJsonTipCapped(unittest.TestCase):
    def test_rule_uses_viewport_cap(self):
        m = re.search(r"\.json-tip\s*\{([^}]*)\}", STYLE, flags=re.S)
        self.assertIsNotNone(m, ".json-tip 规则没了")
        body = m.group(1)
        self.assertIn(
            "min(420px, calc(100vw - 24px))",
            body,
            "420px 的硬顶会超出 375px 视口；必须 min() 到视口减安全边距",
        )


class TestProcRowNarrowTier(unittest.TestCase):
    def test_media_query_hides_user_column(self):
        text = view("Dashboard")
        self.assertIn('class="proc-user"', text, "进程表「用户」列没有可隐藏的挂点")
        # 文件里同级媒体查询不止一处（nic-select），只认带 .proc-row 的那一档
        tiers = [
            m.group(1)
            for m in re.finditer(r"@media \(max-width: 640px\)\s*\{(.*?)\}\s*(?=@media|</style>)", text, flags=re.S)
            if ".proc-row" in m.group(1)
        ]
        self.assertEqual(len(tiers), 1, "≤640px 的进程表降档必须恰好一处")
        tier = re.sub(r"\s+", " ", tiers[0])
        self.assertIn(".proc-user { display: none", tier, "窄屏档没藏掉用户列")
        self.assertIn("grid-template-columns: 56px minmax(0, 1fr) 60px 64px", tier,
                      "窄屏档的列数值不对")

    def test_wide_grid_shape_unchanged(self):
        self.assertIn("grid-template-columns: 56px minmax(0, 1fr) 72px 64px 72px",
                      view("Dashboard"), "桌面档五列被误伤")


class TestEnvKeyInputClamped(unittest.TestCase):
    def test_key_box_uses_clamp_not_literal(self):
        text = view("AppDetail")
        self.assertIn("width: clamp(110px, 38%, 220px)", text,
                      "KEY 框写死 220px 会把 flex:1 的值栏挤到 ~30px")
        self.assertNotIn('style="width: 220px', text, "KEY 框退回像素字面量")


class TestRedisStatsSingleColumnTier(unittest.TestCase):
    def test_grid_defaults_to_one_column(self):
        text = view("Databases")
        self.assertIn('cols="1 s:2 m:4"', text, "Redis 统计格最小档必须是一列")
        self.assertNotIn('cols="2 s:4"', text, "旧的两列最小档还住着")

    def test_all_items_span_one_at_every_break(self):
        text = view("Databases")
        grid_at = text.index('cols="1 s:2 m:4"')
        for m in re.finditer(r'<NGridItem span="([^"]+)">', text):
            if abs(m.start() - grid_at) < 4000:
                with self.subTest(span=m.group(1)):
                    # 每一档都得是 1 格；写了 2 就在单列网格里占两列宽
                    self.assertRegex(m.group(1), r"^(1|[1s]:1)( m:1)?( s:1)?",
                                     '统计卡在某档会占多列')


class TestShippedBundleShape(unittest.TestCase):
    def test_built_js_carries_new_shapes_and_drops_old(self):
        dist = WEB.parent / "dist" / "assets"
        if not dist.exists():
            self.skipTest("本机没有构建产物（web/dist），线上复验另行执行")
        js = "".join(src_of(p) for p in sorted(dist.glob("*.js"))).replace(" ", "")
        self.assertIn("cols:\"1s:2m:4\"", js, "新的一列档没进产物")
        self.assertNotIn('cols:"2s:4"', js, "Redis 统计格旧的两列档还住在产物里")
        css = "".join(src_of(p) for p in sorted(dist.glob("*.css"))).replace(" ", "")
        self.assertIn("max-width:min(420px,calc(100vw-24px))", css,
                      "json-tip 的视口封顶没进产物")


if __name__ == "__main__":
    unittest.main()
