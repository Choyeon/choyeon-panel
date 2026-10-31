"""批次110：OOM 计数读不到的那一轮，不许宣布「没有杀过进程」。

内存页与自检的「内存压力」这一项都从 `memory_service._collect` 的同一份载荷读结论，其中
`oom.kernel_total` 来自 `/proc/vmstat` 的 `oom_kill`。原来 `_oom` 只写
`int(vm_now.get("oom_kill") or 0)`——读空（没有权限、正赶上 proc 读失败、采样窗口里那一次
`_read` 返回空串）和「这台机器从开机到现在一次都没杀过」得到的是同一个 0，于是：

- 界面上 OOM 那一格写「没有杀过进程」，而**同一排**旁边两格正写着「速率没采到」
  （同一个 vmstat 读空，速率那侧早就如实了）；
- `choyeonctl doctor` 的内存项写「内存没有压力：…，没有停顿、换页与 OOM 记录」——
  一句绿灯里包含了它并没有依据的那半句；这台机器真被杀过进程时，这一项是唯一的红点
  （见 ㉞ 的说明），把它写成绿灯比少一项检查更糟。

现在 `_oom` 附 `counter_ok`（这一轮到底读没读到 oom_kill），读空时 `source` 也不再冒充
`counter`；自检与界面都按这个旗标分支，和批次108 的 `measured`、批次106 的 `_unchecked` 同一条线。
真实的 0（读到了 `oom_kill 0`）照旧写「没有杀过进程」与「无 OOM 记录」。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch110_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch110_roots_"))

from app import config  # noqa: E402
from app.services import memory_service as ms  # noqa: E402

REPO = Path(config.BASE)
SVC_SRC = (REPO / "backend" / "app" / "services" / "memory_service.py").read_text(encoding="utf-8")
SERVICES_VUE = (REPO / "web" / "src" / "views" / "Services.vue").read_text(encoding="utf-8")


def _payload(oom: dict, **extra) -> dict:
    data = {"mem": {"ok": True, "used_pct": 31, "available": 2_700_000_000},
            "oom": oom, "counts": {"risk": 0, "warn": 0, "info": 0}, "findings": []}
    data.update(extra)
    return data


class TestCounterFlag(unittest.TestCase):
    def test_unread_vmstat_is_not_a_zero(self):
        got = ms._oom({}, [])
        self.assertFalse(got["counter_ok"], "/proc/vmstat 那一次没读回来，不等于计数是 0")
        self.assertEqual(got["source"], "unknown")
        self.assertIn("不代表没杀过进程", got["reason"])

    def test_real_zero_is_reported_as_zero(self):
        got = ms._oom({"oom_kill": 0}, [])
        self.assertTrue(got["counter_ok"])
        self.assertEqual(got["kernel_total"], 0)
        self.assertEqual(got["source"], "counter")
        self.assertEqual(got["reason"], "")

    def test_flag_survives_when_a_unit_was_killed(self):
        """单元自己 memory.events 里的计数不能替整机计数背书。"""
        units = [{"unit": "tiny.service", "events": {"oom_kill": 2}}]
        with mock.patch.object(ms.subprocess, "run",
                               return_value=type("P", (), {"stdout": ""})()):
            got = ms._oom({}, units)
        self.assertFalse(got["counter_ok"])
        self.assertEqual(got["unit_kills"], {"tiny.service": 2})

    def test_the_reader_is_the_only_place_the_flag_is_born(self):
        self.assertIn('counter_ok = "oom_kill" in vm_now', SVC_SRC)
        self.assertIn('"counter_ok": counter_ok', SVC_SRC)


class TestDoctorSentence(unittest.TestCase):
    def test_green_light_does_not_claim_no_oom(self):
        got = ms.doctor_item(_payload(ms._oom({}, [])))
        self.assertEqual(got["status"], "pass")
        self.assertNotIn("没有停顿、换页与 OOM 记录", got["detail"])
        self.assertIn("OOM 计数这一轮没读到", got["detail"])
        self.assertIn("不代表没杀过进程", got["detail"])

    def test_real_quiet_still_reads_quiet(self):
        got = ms.doctor_item(_payload(ms._oom({"oom_kill": 0}, [])))
        self.assertEqual(got["status"], "pass")
        self.assertIn("没有停顿、换页与 OOM 记录", got["detail"])

    def test_a_real_kill_is_still_the_only_red_dot(self):
        got = ms.doctor_item(_payload({"kernel_total": 3, "unit_kills": {}, "events_total": 0,
                                       "source": "counter-only", "counter_ok": True}))
        self.assertEqual(got["status"], "fail", "杀过人这件事仍然只有这里会红")


class TestFrontendCell(unittest.TestCase):
    def test_the_chip_asks_the_flag(self):
        self.assertIn("oomBlock.value.counter_ok === true", SERVICES_VUE,
                      "缺旗标就当没读到，别从 kernel_total 反推")
        self.assertIn('<template v-else-if="!oomCounterOk">计数没读到</template>', SERVICES_VUE)

    def test_the_unknown_comes_before_the_claim(self):
        unknown = SERVICES_VUE.index('!oomCounterOk">计数没读到')
        quiet = SERVICES_VUE.index("没有杀过进程")
        self.assertLess(unknown, quiet, "分支顺序反了就读空时也宣布「没杀过」")

    def test_the_siblings_already_tell_the_same_story(self):
        """同一排 chip 里速率那几格早就如实说「没采到」，OOM 不许例外。"""
        self.assertIn("速率没采到", SERVICES_VUE)


if __name__ == "__main__":
    unittest.main()
