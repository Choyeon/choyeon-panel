"""批次㊲：往盘上写的字节要有界，格子里的字要看得见。

后端这一半的根因是「同一个事实有两个出处，而其中一个根本没说」。面板以 root 运行，
任何"按别人给的尺寸往磁盘写"的入口都是一个能让整机停摆的口子：写满磁盘的代价不是这一次
请求失败，而是这台机器上所有业务一起写不进去（第一个撞墙的往往还是面板自己的 SQLite）。
文件页打包早就有容量预检，解压也有 `CP_MAX_EXTRACT_MB`/`CP_MAX_EXTRACT_FILES` 两道闸，
但**恢复解包**走的是另一条路（`backup_service._tar_checked` → `extractall`），它只验条目类型
与路径，声明要写 40GB 的归档照样往下解。而把文件页那对固定上限直接搬过来是错的：这台机器
真实应用目录就是 992M/上万文件（`du` 实测 Rosetta 992M、frontend 759M、choyeon-video 748M），
`CP_MAX_EXTRACT_MB=512` 会把正当恢复全挡在门外。恢复要的判据不是"包有多大"，而是
**"这块盘按管理员自己划的警戒线还能写多少"** —— 于是 `write_budget()` 成为这条线的唯一读者，
打包、解压、恢复三个写入者共用它（`disk_thresholds()["warn"]` 与自检/存储对账/巡检同源，
见批次⑱A）。余量按 `f_bavail` 算：写不进去的都算已用，含 ext4 给 root 预留的那 5%。
这条线的**方向**是这一批真正花价值的地方：`alert_disk_pct=80` 读作"已用量到 80% 才告警"，
所以可写的上界是总量的 80%，而不是剩下的 20%。第一版写成 `total * (100 - line) // 100`，
线上实测这块 49GB、已用约 11GB 的盘算出 `room=0B`——打包、解压、应用目录恢复三个入口会被
自己的预检全数封死，而且不报错，只是永远"装不下"。于是这条界同时钉两处：方向用假 statvfs
算成具体字节数（`test_the_line_is_a_used_share_not_a_free_share`），线上再用真实应用目录复验
（992MB 的正当恢复放行、40GB 的声明值拒绝）。

前端这一半的根因是**守卫的覆盖面和被守的写法不匹配**。㊱B 那条"nowrap 类必须挂换行触发器"
的守卫只扫模板里的 `class="…"`，而全站有大量表格格子是在 `h()` 渲染函数里写的
`class: '…'`——两种形态字面不同，于是 414 处渲染函数 class 里 170 处带 nowrap 类的站点整批在闸外。
375px 逐路由实测（17 条路由，含 9 个服务页签）抓到唯一被真裁断的正是其中一处：备份任务「产物」列的
`合计 188.6 KB · 最近 2026-10-14 23:28:54 UTC`，内容 249px 压进 226px 的格子，
列上连 title 都没有，手机上第二个入口也不存在。同一轮实测还确认了两件"不是缺陷"的事，
一并钉在这里免得下次再怀疑：满屏表格右边缘顶到 1081~1343px 全都在
`.n-data-table-base-table-header` / `.n-scrollbar-container` / `v-x-scroll` 里横滚，
17 条路由的 `documentElement.scrollWidth` 全是 375（没有一处整页横向溢出）；
Services 的 `.ls-wrap`/`.mm-break` 由 `.cp-tbl :deep(...)` 定义，页签走查 0 处失效。
"""

import io
import os
import re
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import files_service
from app.services.files_service import assert_write_budget, write_budget

BACKEND = Path(__file__).resolve().parents[1]
WEB = BACKEND.parent / "web" / "src"

NOWRAP = ("cell-sub", "mono-dim", "mono-1l", "cell-name")
TRIGGERS = ("cp-break", "cp-prose")

# 用了、但整个仓库（全局 style.css 与本文件 <style>）都没有一条规则命中它的 class token。
# 这 29 个都是**标在 naive 组件根节点上的命名钩子**（`class="cp-tbl tbl-mounts"`、
# `class="cp-opd"`）：真正生效的样式在 cp-tbl / cp-opd-msg 那一层，这个 token 只是给
# 人和探针留的名字，删掉不会改变一个像素。列成封闭集合而不是不管，是因为下一次再写出
# `class="cell-sub ls-wrap2"` 这种"以为是触发器"的死名字必须失败——㊱B 那批踩过一次
# 失效 cp-break 的坑（见批次㉕A）。
DEAD_TOKEN_WHITELIST = frozenset({
    "comp-bar", "cp-footer-copy", "cp-opd", "cp-set-row", "cp-tool-card", "mono-on", "ptb-card",
    "tbl-bans", "tbl-bigfiles", "tbl-findings", "tbl-keys", "tbl-ld-busy", "tbl-ld-findings", "tbl-ld-quota",
    "tbl-ld-states", "tbl-log", "tbl-mem-findings", "tbl-mem-oom", "tbl-mem-procs", "tbl-mem-swaps",
    "tbl-mem-units", "tbl-mounts", "tbl-patches", "tbl-pend-findings", "tbl-pend-reloads",
    "tbl-pend-units", "tbl-ports", "tbl-roots", "tbl-settings",
})

# 渲染函数形态里没挂触发器也没写 title 的 nowrap 站点数（实测：414 处 class 里 170 处带
# nowrap 类，其中 112 处两样都没有）。这批绝大多数是**结构上短**的值（'—'、'NULL'、'IPv6'、
# `pid 1234`、一个时间戳），折行对它们没有意义，所以不一刀切改名，只把这颗钉子打在这里：
# 只能降，不能升。降够了就把断言改成 == 0。
RENDER_NOWRAP_BASELINE = 112


def _web(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def _zeros(n: int) -> io.BytesIO:
    return io.BytesIO(b"\0" * n)


def _body(rel: str, header: str) -> str:
    src = (BACKEND / rel).read_text(encoding="utf-8")
    start = src.index(header) + len(header)
    rest = src[start:]
    m = re.search(r"\n(?=(async\s+def |def |class ))", rest)
    return rest[: m.start()] if m else rest


class WriteBudgetIsTheOnlyCeiling(unittest.TestCase):
    """三个写入者都只能问同一句「这块盘按警戒线还能写多少」。"""

    def test_budget_reads_the_alert_line_and_f_bavail(self):
        body = _body("app/services/files_service.py", "def write_budget(")
        self.assertIn('disk_thresholds()["warn"]', body)
        self.assertIn("st.f_bavail", body)
        # f_bfree 会把 ext4 给 root 预留的 5% 也算成能写，凭空多出一块永远写不到的地板
        self.assertNotIn("st.f_bfree", body)

    def test_reject_is_a_no_write_and_names_the_knob(self):
        with tempfile.TemporaryDirectory() as d:
            room = write_budget(d)["room"]
            # 正好卡在界内：不许多拦一点，否则正当恢复/打包会被自己的预检挡住
            assert_write_budget(d, room, "测试")
            with self.assertRaises(RuntimeError) as ctx:
                assert_write_budget(d, room + 1, "恢复这份归档")
            msg = str(ctx.exception)
            self.assertIn("警戒线", msg)
            self.assertIn("未写入任何文件", msg)
            self.assertIn("磁盘告警阈值", msg)

    def test_unreadable_partition_is_refused_not_faked(self):
        # 读不到剩余空间就不能宣布"有界"：把判据失败粉饰成检查通过，等于给无界写入放行
        with self.assertRaises(RuntimeError) as ctx:
            write_budget(os.path.join(tempfile.gettempdir(), "cp-batch37-no-such-dir"))
        self.assertIn("剩余空间", str(ctx.exception))

    def test_the_line_is_a_used_share_not_a_free_share(self):
        """方向钉死：80% 的警戒线读作「已用到 80% 就告警」，能写到的上界是总量的 80%。

        线上复验时这里先写成了 `total * (100 - line) // 100`，实测结果是这块 78GB、用了 51%
        的盘算出 room=0B —— 打包、解压、应用目录恢复会被自己的预检全部挡死，而 992MB 的
        Rosetta 恢复恰恰是正当操作。方向反掉不会报错，只会静默地把功能封死，所以钉成断言。
        """
        from app.services import alerts_service

        class _Vfs:
            f_frsize = 4096
            f_blocks = 26_214_400  # 100 GiB
            f_bavail = 12_845_056  # 剩 49 GiB，即已用 51%

        with tempfile.TemporaryDirectory() as d:
            with (
                mock.patch.object(alerts_service, "disk_thresholds", lambda: {"warn": 80, "crit": 90}),
                mock.patch.object(files_service.os, "statvfs", lambda *_a: _Vfs()),
            ):
                b = write_budget(d)
        self.assertEqual(b["room"], 29 * 1024**3, "上界是总量的 80%（80GiB）减去已用（51GiB）")
        self.assertGreater(b["room"], 0, "一块没到警戒线的盘，room 必须是正数")

    def test_all_three_writers_share_the_one_reader(self):
        files = _body("app/services/files_service.py", "def extract_archive(")
        pack = _body("app/services/files_service.py", "def make_archive(")
        restore = _body("app/services/backup_service.py", "async def _restore_app(")
        self.assertEqual(files.count("assert_write_budget("), 2, "zip 与 tar 两条分支各一次")
        self.assertEqual(pack.count("assert_write_budget("), 1)
        self.assertIn('write_budget(common)["room"]', pack, "估算扫描的停止界也必须是同一条线")
        self.assertIn('assert_write_budget(dest, sum(m.size for m in members if m.isfile())', restore)
        # 旧的那句"整块盘的空闲就是额度"不能再有副本：它会允许写到只剩 1KB
        self.assertNotIn("shutil.disk_usage(common).free", files + pack + restore)

    def test_restore_checks_before_it_touches_the_service(self):
        """余量不够必须在停服务之前拒：恢复没有整份快照可退，停掉线上服务再失败是最坏顺序。"""
        body = _body("app/services/backup_service.py", "async def _restore_app(")
        budget = body.index("assert_write_budget(")
        stop = body.index("_stop_for_overwrite(")
        unpack = body.index("tf.extractall")
        self.assertLess(budget, stop)
        self.assertLess(budget, unpack)
        self.assertLess(body.index("_reject_symlink_ancestors"), stop)

    def test_tar_index_rejects_bomb_before_any_write(self):
        """真造一份声明巨大的归档：只读索引阶段就该被拒，目标目录一个字节都不许多。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "big.tar.gz")
            with tarfile.open(src, "w:gz") as tf:
                for name, size in (("a.txt", 4096), ("b.txt", 4096)):
                    info = tarfile.TarInfo(name)
                    info.size = size
                    tf.addfile(info, _zeros(size))
            dest = os.path.join(d, "out")
            os.makedirs(dest)
            with tarfile.open(src, "r:gz") as tf:
                declared = sum(m.size for m in tf.getmembers() if m.isfile())
            self.assertEqual(declared, 8192)
            room = write_budget(dest)["room"]
            with self.assertRaises(RuntimeError):
                assert_write_budget(dest, room + declared, "恢复这份归档")
            self.assertEqual(os.listdir(dest), [], "拒绝必须发生在任何落盘之前")


class RenderFormNowrapSitesAreGuarded(unittest.TestCase):
    """`h()` 渲染函数里的 `class: '…'` 也是格子：㊱B 的模板守卫看不见它。"""

    def _scan(self):
        offenders = []
        for p in sorted(WEB.rglob("*.vue")):
            text = p.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                for cls in re.findall(r"""class:\s*[`'"]([^`'"]*)[`'"]""", line):
                    if not any(c in NOWRAP for c in cls.split()):
                        continue
                    if any(t in cls for t in TRIGGERS) or "title" in line:
                        continue
                    offenders.append(f"{p.name}:{i}")
        return offenders

    def test_render_form_is_counted_and_only_shrinks(self):
        got = self._scan()
        self.assertLessEqual(
            len(got),
            RENDER_NOWRAP_BASELINE,
            "渲染函数里又多了一处没触发器、没 title 的 nowrap 格子：" + ", ".join(got[:12]),
        )

    def test_the_measured_clip_site_now_wraps_and_carries_the_full_text(self):
        """375px 实测被裁断的那一格（备份任务「产物」列的合计行）要同时有折行触发器和全文。

        内容 249px、格子只有 226px，而 .cell-sub 自带 nowrap+ellipsis，
        列上又没有第二个入口，于是「合计 … · 最近 … UTC」这半句会被吃掉。
        只写 title 不算修好（手机上没有 hover），只折行也不算（折完仍可能超宽），
        所以两样都得在；这一格是那一批唯一的实测缺陷，钉子单独打在这里。
        """
        text = _web("views/Backups.vue")
        self.assertIn("const sub = `合计", text, "产物列的合计行没了：那一格的实测结论要重新做一次")
        self.assertIn(
            "h('div', { class: 'cell-sub cp-prose', title: sub }, sub)",
            text,
            "合计行必须同时挂 cp-prose 触发器并把全文写进 title",
        )

    def test_dead_class_names_stay_a_closed_set(self):
        """用了却没任何规则命中的 class token 必须是已点名的那 29 个命名钩子。"""
        glob_defined = set()
        for css in [WEB / "style.css", *WEB.glob("**/*.css")]:
            glob_defined |= set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", css.read_text(encoding="utf-8")))
        found = set()
        for p in sorted(WEB.rglob("*.vue")):
            text = p.read_text(encoding="utf-8")
            own = set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", "".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))))
            chunks = re.findall(r'(?<![:\w])class="([^"]*)"', text) + re.findall(r"(?<![:\w])class='([^']*)'", text)
            chunks += re.findall(r"""[{\s,]class:\s*[`'"]([^`'"]*)[`'"]""", text)
            for c in chunks:
                for tok in c.split():
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", tok) is None:
                        continue
                    if tok in glob_defined or tok in own or tok.startswith("n-") or re.match(r"(is|has)-", tok):
                        continue
                    found.add(tok)
        self.assertEqual(found - DEAD_TOKEN_WHITELIST, set(), "新出现的死类名（以为是样式其实没人写过它）")
        self.assertEqual(DEAD_TOKEN_WHITELIST - found, set(), "白名单里有过期项：那个类名已经不再被引用，删掉它")

    def test_wrap_triggers_are_still_double_class_names(self):
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertIn(".cp-break.cp-break {", css)
        self.assertIn(".cp-prose.cp-prose {", css)
        # 单类名就回到 0-1-0，又被 .cell-sub/.mono-dim 自己的 nowrap 压回去（0-2-0 是打法本身）
        self.assertNotIn("\n.cp-break {", css)
        self.assertNotIn("\n.cp-prose {", css)


class SegmentedControlsAreSquareEnough(unittest.TestCase):
    def test_narrow_rule_covers_width_as_well_as_height(self):
        css = (WEB / "style.css").read_text(encoding="utf-8")
        rules = re.findall(r"\.cp-seg > button \{([^}]*)\}", css)
        joined = " ".join(rules)
        self.assertIn("min-height: 44px", joined)
        self.assertIn("min-width: 44px", joined, "375px 实测改前概览段选 25px 高、时长档只有 38~39px 宽")


if __name__ == "__main__":
    unittest.main()
