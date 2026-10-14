"""批次㉖：浮层的宽度不能靠裸百分比和 min-width，375px 实测它们把抽屉压成 113px。

两条都是登录态 375px 同源离屏实测出来的（2026-10-14，旧构建）：

1. 五处 `<NDrawer :width="'NN%'">`。naive 把 width 原样写成行内样式，百分比就跟着
   视口缩：375px 下备份任务抽屉 188px、上传产物 150px、通知渠道 113px、两步验证
   120px——里面 230px 的下拉实测顶穿右边缘 67px，整张表单在手机上没法用。
2. 备份恢复弹窗写的是 `{ width: '40vw', minWidth: '420px', maxWidth: '94vw' }`。
   CSS 里 min-width 压过 max-width，375px 下这条弹窗宽 420px、顶出屏幕 68px。
   数死的像素同理：PG 行编辑器抽屉 `:width="560"`，比手机屏还宽 185px。

改法：抽屉宽度收进 `ui.ts` 的 `useDrawerWidth(fraction)`（唯一出处，七处抽屉全接）——≤640px 一律
全屏，宽屏取比例并夹在 [420, 860]；弹窗不设 min-width，桌面下限写进
`width: max(40vw, 420px)`，`max-width: 94vw` 才真的封顶；TOTP 二维码补 max-width。

六件事由用例锁住：useDrawerWidth 只有一份、抽屉的 width 必须是共享出处的变量名
（百分比与像素字面量都算回潮）、抽屉不许少接一处（锚点数量）、NModal 一律有 vw 封顶且
style 里没有 min-width、二维码可缩、旧形状（'40%'/'50%'/'30%'/'32%'/560 与 40vw+minWidth）
一处不剩，构建产物里也查得到。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT.parent / "web" / "src"
UI_TS = (WEB / "ui.ts").read_text(encoding="utf-8")
VIEWS = sorted((WEB / "views").glob("*.vue"))
COMPS = sorted((WEB / "components").glob("*.vue"))


def src_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def all_vue_text() -> str:
    return "".join(src_of(p) for p in (*VIEWS, *COMPS))


def drawer_tags(text: str) -> list[str]:
    """所有 <NDrawer 开标签（跨行属性归一成一行再看）。"""
    return [re.sub(r"\s+", " ", m) for m in re.findall(r"<NDrawer\b[^>]*>", text, flags=re.S)]


class TestDrawerWidthSingleSource(unittest.TestCase):
    def test_helper_defined_once(self):
        self.assertEqual(UI_TS.count("export function useDrawerWidth"), 1)
        # 规则本体也要在：手机全屏、宽屏夹在 420..860
        self.assertIn("420", UI_TS)
        self.assertIn("860", UI_TS)

    def test_no_percentage_width_anywhere(self):
        """`'NN%'` 这种写法一处都不许留；数字字面量同样不许（560px 在手机上顶出屏）。"""
        for path in (*VIEWS, *COMPS):
            for tag in drawer_tags(src_of(path)):
                with self.subTest(file=path.name, tag=tag[:60]):
                    m = re.search(r":width=\"([^\"]+)\"", tag)
                    self.assertIsNotNone(m, f"{path.name} 的抽屉没写 :width，等于回到 naive 默认 251px")
                    value = m.group(1)
                    self.assertNotIn("%", value, "裸百分比在 375px 下只剩一百多像素")
                    self.assertRegex(
                        value, r"^[A-Za-z_$][A-Za-z0-9_$]*$",
                        f"{path.name} 的抽屉 width 写死了 {value!r}，必须引用 useDrawerWidth 的结果",
                    )

    def test_drawers_reference_the_shared_helper(self):
        """每个抽屉的 width 都必须是 useDrawerWidth 的结果，且该文件从 ../ui 导入。"""
        users = [p for p in (*VIEWS, *COMPS) if re.search(r"<NDrawer\b", src_of(p))]
        self.assertGreaterEqual(len(users), 5, "锚点：抽屉有 Backups/BackupFiles/Settings/CronTasks/RowEditor 五个文件")
        for path in users:
            text = src_of(path)
            with self.subTest(file=path.name):
                self.assertIn("useDrawerWidth(", text)
                self.assertRegex(text, r"import \{[^}]*useDrawerWidth[^}]*\} from '\.\./ui'")

    def test_old_copies_stay_dead(self):
        joined = all_vue_text()
        for dead in ("\"'40%'\"", "\"'50%'\"", "\"'30%'\"", "\"'32%'\""):
            self.assertNotIn(f":width={dead}", joined, f"{dead} 又回来了")


class TestModalWidthCap(unittest.TestCase):
    """弹窗：封顶只认 max-width，min-width 会把它顶穿。"""

    def modal_tags(self) -> list[tuple[str, str]]:
        out = []
        for path in (*VIEWS, *COMPS):
            for m in re.findall(r"<NModal\b[^>]*>", src_of(path), flags=re.S):
                out.append((path.name, re.sub(r"\s+", " ", m)))
        return out

    def test_every_modal_caps_with_viewport(self):
        seen = self.modal_tags()
        self.assertGreaterEqual(len(seen), 10, "锚点：弹窗至少有十来处，守卫不是空转")
        for name, tag in seen:
            with self.subTest(file=name, tag=tag[:56]):
                self.assertTrue(
                    "max-width" in tag or "maxWidth" in tag,
                    f"{name} 的弹窗没有 vw 封顶，宽屏写法会顶穿手机屏",
                )

    def test_no_min_width_on_modals(self):
        for name, tag in self.modal_tags():
            with self.subTest(file=name, tag=tag[:56]):
                self.assertNotIn("min-width", tag)
                self.assertNotIn("minWidth", tag)

    def test_restore_modal_keeps_desktop_floor_in_width(self):
        """恢复弹窗的 420 下限还在，只是换到 width: max() 里，不再压封顶。"""
        text = src_of(WEB / "views" / "BackupFiles.vue")
        self.assertIn("width: max(40vw, 420px)", text)


class TestQrShrinkable(unittest.TestCase):
    def test_totp_qr_has_max_width(self):
        m = re.search(r"<img[^>]*两步验证绑定二维码[^>]*>", src_of(WEB / "views" / "Settings.vue"))
        self.assertIsNotNone(m, "TOTP 二维码不见了（反证锚点）")
        self.assertIn("max-width", m.group(0), "220px 的二维码在窄浮层里必须能缩")


class TestShippedBundleShape(unittest.TestCase):
    """旧构建里那个 min-width 是真实发出去的；新产物里不许再有它贴着恢复弹窗。"""

    def test_built_js_has_no_drawer_percentage_literal(self):
        js_dir = WEB.parent / "dist" / "assets"
        if not js_dir.exists():
            self.skipTest("本机没有构建产物（web/dist），线上复验另行执行")
        joined = "".join(src_of(p) for p in sorted(js_dir.glob("*.js"))).replace(" ", "")
        # 只钉我们写过的那一坨旧形状。`minWidth:"420px"` 单独出现是 naive-ui 自己的
        # preset-dialog 样式（preset="card" 走不到），不归我们管。
        self.assertNotIn('width:"40vw",minWidth:"420px"', joined, "旧恢复弹窗的 min-width 顶封顶写法还住在产物里")
        self.assertNotIn('width:"50%"', joined, "旧抽屉的裸百分比还住在产物里")


if __name__ == "__main__":
    unittest.main()
