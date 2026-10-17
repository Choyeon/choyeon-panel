"""批次53A：应用列表里两行 nowrap 的格子不再越界。

1440 档实测：域名这一列的内容宽 186px，而 rosetta 那三个域名的自然宽是 226px。这一列原先
写死 width，格子里又是 nowrap 的一行，于是被挤扁的是排在最后的注脚「nginx 检测」——它被压到
12px 宽、竖成 6 行字符，爬进了状态列（用户截图里那一竖条）。

同一个打法在名称列的 375 档：列宽 200px，而「rosetta-frontend [前端] [同步] ↗」自然宽 289px，
nowrap 让徽章和按钮直接盖在类型列上。两处都改成整项换行：域名是给人读的，注脚也是，
名字后面的标记也是，谁都不该逐字断行，更不该爬到隔壁列去。
"""
import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
APPS = (SRC / "web" / "src" / "views" / "Apps.vue").read_text(encoding="utf-8")


class DomainColumnShapeTests(unittest.TestCase):
    def test_the_domain_column_has_room_for_two_tags_and_the_counter(self):
        m = re.search(r"key: 'domains', (?:width|minWidth): (\d+)", APPS)
        self.assertTrue(m, "这一列得声明自己的宽度，不能靠表格均分——均分过 186px，两个域名就装不下")
        self.assertGreaterEqual(
            int(m.group(1)),
            234,
            "两个域名的自然宽 191 + 计数 15 + 间距 8 + 左右内边距 24 = 234，低于这个数就要开始换行",
        )

    def test_the_domain_row_wraps_whole_items(self):
        rules = [ln for ln in APPS.splitlines() if ".dom-line" in ln]
        self.assertTrue(rules, "域名行得有它自己的规则，否则 NSpace 的 item 包装层会替我们决定怎么收缩")
        self.assertIn("flex-wrap: wrap", rules[0], "nowrap 下尾部条目会被压成 1ch 再逐字断行，换行才是这一格该有的收缩方式")

    def test_the_provenance_note_is_not_a_peer_of_the_tags(self):
        self.assertIn(
            "h('div', { class: 'dom-hint' }, 'nginx 检测')",
            APPS,
            "「这批域名是从 nginx 反代里认出来的」是域名的出处说明，不是和域名抢同一行的第三项",
        )
        self.assertNotIn(
            "h('span', { style: 'color:var(--cp-text-mute);font-size:var(--fs-2xs)' }, 'nginx 检测')",
            APPS,
            "内联样式的裸 span 排在队尾，列一窄就是它先被挤成竖排",
        )

    def test_the_overflow_counter_stays_readable_and_names_what_it_hid(self):
        self.assertIn(".dom-more) { flex: none", APPS, "「+1」被一起收缩就没意义了：它要说的是还有几个没显示")
        self.assertIn("title: ds.slice(2).join", APPS, "被折起来的域名要在 title 里点得出名字")


class NameRowShapeTests(unittest.TestCase):
    def test_the_name_row_wraps_instead_of_climbing_into_the_next_column(self):
        self.assertNotIn(
            "h(NSpace, { size: 6, align: 'center', wrap: false }",
            APPS,
            "375 档名称列只有 200px，而「rosetta-frontend [前端] [同步] ↗」自然宽 289px——"
            "nowrap 时超出的 89px 直接压在类型列上，和域名格是同一个病",
        )
        self.assertEqual(APPS.count("h(NSpace, { size: 6, align: 'center' }"), 2, "主行与组件行都要允许整项换行")


if __name__ == "__main__":
    unittest.main()
