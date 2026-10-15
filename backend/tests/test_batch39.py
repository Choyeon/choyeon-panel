"""批次㊴：表格横向滚动的口径守卫。

`scroll-x` 写死像素时，naive-ui 把 `formatLength(scrollX)` 当成 `.n-data-table-base-table`
的 `min-width`（`use-scroll.mjs` 的 `styleScrollXRef`），于是表格容器本身被撑到那个宽度，
它内部那层真正该滚的节点（`.n-data-table-base-table-header` / `.n-scrollbar-container`）
的 clientWidth 跟着变成同一个数，scrollWidth - clientWidth 归零，横溢被推到页面级
`.cp-scroll` 上。线上 375×667 实测（写 1040px 与写 'auto' 两份同一 DOM）：

    /apps      页面多滚 714px → 0px；表内滚动 0 → 440px；右固定操作列 不可达 → 可见
    /users     页面多滚 698px → 0px；表内滚动 0 → 537px
    /services  页面多滚 714px → 0px；表内滚动 0 → 530px
    /audit     页面多滚 706px → 0px；表内滚动 0 → 424px

宽度从此由每列的 `minWidth` 决定，不再由一张表抄一个桌面时代凑出来的总数。
代价写在 `test_scroll_x_auto_的已知代价` 里，是有意接受的取舍，不是没看见。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"

SCROLL_X_RE = re.compile(r':scroll-x="([^"]*)"')


def _scroll_x_values() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted(WEB_SRC.rglob("*.vue")):
        for value in SCROLL_X_RE.findall(path.read_text(encoding="utf-8")):
            out.append((str(path.relative_to(WEB_SRC)), value))
    return out


def _inside_media(src: str, needle: str) -> str:
    """返回 needle 所在的那段媒体查询文本；needle 不在任何 `@media` 里时抛错。"""
    idx = src.index(needle)
    start = src.rindex("@media", 0, idx)
    window = src[start:idx]
    if window.count("{") - window.count("}") < 1:
        raise AssertionError(f"「{needle}」不在 @media 块内")
    return window


class ScrollXContractTests(unittest.TestCase):
    def test_全站表格不再有写死的横向滚动宽度(self):
        pairs = _scroll_x_values()
        # 数量兜底：断"全对"时一条用例都没扫到也是全对（历史上这张表有 45 处）。
        self.assertGreaterEqual(len(pairs), 40, f"只扫到 {len(pairs)} 处 scroll-x，扫描本身失效了")
        bad = [(f, v) for f, v in pairs if v != "'auto'"]
        self.assertEqual(bad, [], f"scroll-x 必须是 'auto'，写死像素会把横溢推到页面上：{bad}")

    def test_反证_写死像素的形态会被拦下(self):
        # 这一条盯的是上一条的判据本身：把真实文件里的 'auto' 换成 1040 必须被认出来。
        real = [v for _, v in _scroll_x_values()]
        self.assertTrue(all(v == "'auto'" for v in real))
        fake = SCROLL_X_RE.findall('<NDataTable :scroll-x="1040" :data="rows">')
        self.assertEqual(fake, ["1040"], "正则没抓到数字形态，上一条就是空断言")
        self.assertEqual([v for v in fake if v != "'auto'"], ["1040"])

    def test_scroll_x_auto_的已知代价(self):
        # naive-ui 的 deriveActiveRightFixedColumn 用 Number(props.scrollX) 算右固定列的
        # 激活边界（use-scroll.mjs:112）。'auto' 进去是 NaN，任何 x < NaN 都假，
        # 所以右固定列的投影（.n-data-table--scroll-x-fixed-right 那条阴影）不再出现。
        # 这里锁住"我们是被知情地换掉了它"：sticky 定位本身是 CSS 出的，不受影响，
        # 而列永远落在视口内（上一条实测 frVisible 由 false 变 true），投影要表达的
        # "右边还有内容被压住"这个信息，窄屏下由表内滚动条自己说。
        head = (WEB_SRC / "views" / "Apps.vue").read_text(encoding="utf-8")
        self.assertIn(':scroll-x="\'auto\'"', head)
        self.assertNotIn("rightFixedShadow", head, "别指望在这里补回 naive 的右投影")


class MobileLogBoxTests(unittest.TestCase):
    """两处 420px 的日志盒最小高：在 667px 的屏上吃掉 63% 竖向空间。"""

    def test_应用详情的部署日志在窄屏改矮(self):
        src = (WEB_SRC / "views" / "AppDetail.vue").read_text(encoding="utf-8")
        self.assertIn(".dep-log { flex: 1; min-width: 0; min-height: 420px;", src, "桌面档的 420px 不该被顺手删")
        needle = ".dep-log { min-height: 200px; max-height: 50vh; }"
        self.assertIn(needle, src, "窄屏没有覆盖部署日志的高度")
        self.assertIn("@media", _inside_media(src, needle))

    def test_服务页的单元日志盒在窄屏改矮(self):
        src = (WEB_SRC / "views" / "Services.vue").read_text(encoding="utf-8")
        self.assertIn("min-height: 420px", src, "桌面档的 420px 不该被顺手删")
        needle = ".svc-log :deep(.log-view) { min-height: 160px; height: auto !important; }"
        self.assertIn(needle, src, "窄屏没有覆盖单元日志盒的高度")
        window = _inside_media(src, needle)
        self.assertIn("640px", window, f"覆盖写在错误的断点里：{window[:60]}")
        # :deep 的可达性沿用桌面档那只选择器，两者必须一字不差同形。
        self.assertIn(".svc-log :deep(.log-view)", src.split(needle)[0], "窄屏选择器和桌面档不是同一只")

    def test_快捷入口按钮够触屏(self):
        src = (WEB_SRC / "views" / "LogCenter.vue").read_text(encoding="utf-8")
        self.assertNotIn('size="tiny"', src, "tiny 档在 375px 上只有 24px 高，手指按不准")
        self.assertRegex(
            src,
            r'v-for="p in PRESETS"[^>]*size="small"',
            "日志检索的快捷入口按钮没升到 small（线上实测 44px 高才够触屏）",
        )


if __name__ == "__main__":
    unittest.main()
