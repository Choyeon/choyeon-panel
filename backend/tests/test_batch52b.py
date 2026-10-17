"""批次52B：概览的磁盘仪表报「哪块盘」，动效有唯一出处。

两件事实钉在这里：

1. 仪表标签报挂载点，不报我们从哪个目录量到的。`/root/www/choyeon-panel/data` 既读不出
   是哪块盘，又把面板自己的安装路径摊给每个登录者；而挂载表读不到的那一刻，宁可只写
   「磁盘」，也不许自称 `/`。盘名的唯一读者是 storage_service.mount_of（存储页签同一个）。
2. 动效不是装饰堆砌，而是「同一时刻的同一个值」：环、圆心读数、概览计数共用一个补间出处，
   曲线只在真正画出一组数据时播一次入场。JS 驱动的补间不受 style.css 那条 CSS kill
   switch 管，所以它必须自己读 prefers-reduced-motion。
"""
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
WEB = SRC / "web"


def view(name: str) -> str:
    return (WEB / "src/views" / f"{name}.vue").read_text(encoding="utf-8")


def comp(name: str) -> str:
    return (WEB / "src/components" / f"{name}.vue").read_text(encoding="utf-8")


class MountNameSingleReaderTests(unittest.TestCase):
    """盘名从挂载表来，而且只有那一个读者。"""

    def test_system_metrics_asks_storage_service_instead_of_reading_proc_itself(self):
        src = (SRC / "backend/app/services/system_metrics.py").read_text(encoding="utf-8")
        self.assertIn("storage_service.mount_of(path)", src,
                      "自己再开一次 /proc/mounts 就是第二个读者：两处解析一旦分叉，"
                      "概览说这块盘叫 /，存储页签说它叫 /srv")
        self.assertNotIn("MOUNTS_PATH", src,
                         "system_metrics 不许绕过 mount_of 自己开 /proc/mounts")

    def test_the_mount_name_is_asked_once_per_ttl_not_every_sample(self):
        src = (SRC / "backend/app/services/system_metrics.py").read_text(encoding="utf-8")
        self.assertIn("_mount_cache", src)
        self.assertIn("MOUNT_TTL", src,
                      "/system/stats 每 3s 一跳，盘不会自己改名；没有缓存就是每 3s 多读一次 /proc")


class GaugeLabelHonestyTests(unittest.TestCase):
    def test_disk_label_reads_the_mount_and_never_claims_root(self):
        src = view("Dashboard")
        self.assertIn("stat.value.diskMount", src)
        self.assertNotIn("磁盘 ${stat.value.diskMount || '/'}", src,
                         "挂载表读不到时兜底成 / 是在宣称量过挂载表")
        self.assertIn("const diskLabel = computed", src)
        self.assertIn(": '磁盘'", src, "读不到盘名就只写「磁盘」，不许编一个路径")

    def test_the_measured_directory_is_still_reachable_but_not_in_the_label(self):
        """出处不能丢：它是「这块盘的数字是从哪个目录量到的」的唯一说明，放 note 里。"""
        src = view("Dashboard")
        self.assertIn("量自数据目录 ${stat.value.diskPath}", src)
        self.assertIn(':note="g.note"', src)

    def test_history_curve_and_gauge_name_the_same_disk(self):
        src = view("Dashboard")
        self.assertIn("const diskHistTitle = computed", src,
                      "上面仪表写 `/`、下面曲线写别的，一页两个口径时读的人不知道信谁")
        self.assertIn("{{ diskHistTitle }}", src)


class ToneHasOneJudgeTests(unittest.TestCase):
    """颜色、环上刻度、越线底光必须来自同一次比大小。"""

    def test_the_band_helper_is_gone_and_tone_is_judged_once(self):
        src = view("Dashboard")
        self.assertEqual(src.count("if (err > 0 && v >= err)"), 1,
                         "又多一处比大小：环红了一格、刻度却画在另一处，仪表开始自相矛盾")
        self.assertEqual(src.count("if (warn > 0 && v >= warn)"), 1)
        self.assertIn("const tone = toneOf(r.percent, r.warn ?? 0, r.crit ?? 0)", src)
        self.assertNotIn("band(", src, "band 还在就是判档没收到 toneOf 一个出口上")

    def test_the_gauge_renders_the_line_it_was_judged_by(self):
        src = comp("StatGauge")
        self.assertIn(':warn="g.warn"', view("Dashboard"))
        self.assertIn(':crit="g.crit"', view("Dashboard"))
        self.assertIn("function tickAt(", src)
        self.assertIn("<line", src, "档位只改颜色不改环上的刻度，红环就说不清越的是哪条线")


class MotionSourceTests(unittest.TestCase):
    def test_the_tween_is_the_only_number_animator_and_it_respects_reduced_motion(self):
        src = (WEB / "src/composables/useTween.ts").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", src,
                      "JS 补间不受 style.css 那条 CSS kill switch 管，只写 CSS 等于漏掉这一层")
        self.assertIn("document.hidden", src,
                      "后台标签页一帧都不排，不夹平就会留下「读数已到、环还停在 0」")
        self.assertIn("cancelAnimationFrame", src)
        self.assertIn("onScopeDispose", src, "卸载时不停掉 rAF，就是在给已经不存在的组件继续画帧")
        for f in ("StatGauge.vue", "TweenNumber.vue"):
            self.assertIn("useTweenNumber", comp(f[:-4]), f + " 该用同一个补间出处")
        self.assertNotIn("requestAnimationFrame", comp("TweenNumber"),
                         "组件里各起一个 rAF 就是第二个补间实现，帧率与豁免会各自漂移")

    def test_the_ring_offset_is_driven_by_the_tween_not_a_css_transition(self):
        src = comp("StatGauge")
        self.assertIn("const shownPct = useTweenNumber(clamped, 900)", src)
        self.assertIn("C * (1 - (shownPct.value ?? 0) / 100)", src)
        self.assertNotIn("stroke-dashoffset var(--dur", src,
                         "rAF 与 CSS transition 一起动，每一帧环都会落后读数一小截")

    def test_non_numeric_readings_are_not_tweened(self):
        """负载那一格写的是「运行流畅」：给一个词补间会逐帧插出半个词。"""
        src = comp("StatGauge")
        self.assertIn("Number.isFinite(v)", src)
        self.assertIn("numeric.value == null || shownText.value == null", src)

    def test_charts_replay_only_when_a_new_dataset_arrives(self):
        src = comp("MetricChart")
        self.assertIn("const animate = !animated && props.xs.length > 0 && !prefersReducedMotion()", src,
                      "空数据也播动画，等于给一条还不存在的线做入场；稳态 3s 刷新播动画，"
                      "等于每 3s 把四条曲线重补间一遍")
        self.assertIn("watch(() => props.animKey", src)
        d = view("Dashboard")
        self.assertEqual(d.count(':anim-key="histRange"'), 4, "四张历史卡换区间都要重播一次")
        self.assertIn(':anim-key="monMode"', d)

    def test_page_arrival_and_folding_are_one_motion_language(self):
        d = view("Dashboard")
        self.assertNotIn('mode="out-in"', d,
                         "out-in 要等离场那一帧播完才挂新内容，页面拿不到帧时列表永远不出现；"
                         "Shell.vue 也因「多等一拍」明确不用它")
        self.assertNotIn("cp-fold", d,
                         "折叠改回瞬时挂载后这套类就是死的，留着只会误导下一次改动画的人")
        self.assertIn('class="proc-row cp-rise"', d,
                      "展开的列表靠逐行错峰落位；列表每次展开都重新挂载，入场动画自然会重播")
        self.assertIn("<TweenNumber v-if=\"it.value != null\"", d,
                      "概览计数每 30s 与后端对一次账，硬切的数字读起来像页面自己抖了一下")


if __name__ == "__main__":
    unittest.main()
