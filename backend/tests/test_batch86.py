"""批次86：OOM 名单把同一场事故数成两三次，尾读与展示上限也不报总数。

内核为一次 OOM 会写两三行：`X invoked oom-killer`（触发者）、`oom-kill:…task=Y,pid=N`
与 `Out of memory: Killed process N (Y)`（受害者）。旧解析把这些行原样塞进名单，
于是一场事故在页面上出现两行、判词里的受害者重复两遍；本机三次 OOM 的 9 行日志
被 `MAX_OOM_EVENTS = 8` 又砍掉一行，页面仍写着「挑出来的这几行」。

这一批盯四件事：
① 同一 pid 的多行合并成一行，触发者不进名单；
② `journalctl -n 3000` 读满上限时自报截断与上限，不把「没读到」写成「没发生」；
③ 名单只列最近 8 个时报出总数与没列出的个数；
④ 内核计数与日志里认到的次数对不上时，判词、自检、CLI、页面说同一句话。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
from app.services import memory_service as ms  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# 生产机 2026-10-23 / 10-29 / 10-31 三次 OOM 的原始 kernel 行（-o short-iso）。
# 每次事故两行受害者记录 + 一行触发者；触发者不是受害者。
HOST_LOG = """2026-10-23T21:02:30+08:00 Nibiru kernel: postgres invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=0
2026-10-23T21:02:30+08:00 Nibiru kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/user.slice/user-0.slice/session-259.scope,task=python,pid=250726,uid=0
2026-10-23T21:02:30+08:00 Nibiru kernel: Out of memory: Killed process 250726 (python) total-vm:2637228kB, anon-rss:832008kB
2026-10-29T23:03:28+08:00 Nibiru kernel: esbuild invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=0
2026-10-29T23:03:28+08:00 Nibiru kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/user.slice/user-0.slice/session-2230.scope,task=python,pid=2002758,uid=0
2026-10-29T23:03:28+08:00 Nibiru kernel: Out of memory: Killed process 2002758 (python) total-vm:2897212kB, anon-rss:1037588kB
2026-10-31T09:27:00+08:00 Nibiru kernel: python invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=0
2026-10-31T09:27:00+08:00 Nibiru kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/user.slice/user-0.slice/session-3840.scope,task=python,pid=2709534,uid=0
2026-10-31T09:27:00+08:00 Nibiru kernel: Out of memory: Killed process 2709534 (python) total-vm:4643980kB, anon-rss:2846320kB
"""


def _oom(**kw) -> dict:
    base = {"kernel_total": 0, "unit_kills": {}, "events": [], "events_total": 0,
            "events_omitted": 0, "truncated": False, "line_limit": ms.OOM_LOG_LINES,
            "source": "counter", "reason": ""}
    base.update(kw)
    return base


def _run(text: str, oom_kill: int = 3):
    with mock.patch.object(ms.subprocess, "run", return_value=mock.Mock(stdout=text)) as run:
        return ms._oom({"oom_kill": oom_kill}, []), run


class DedupeTests(unittest.TestCase):
    def test_one_row_per_killed_pid(self):
        evs = ms._oom_events(HOST_LOG)
        self.assertEqual([e["pid"] for e in evs], [250726, 2002758, 2709534])
        self.assertEqual([e["comm"] for e in evs], ["python", "python", "python"])
        # 展示取信息最全的那行，而不是先到的 oom-kill 行。
        self.assertIn("Out of memory: Killed process 250726", evs[0]["line"])
        # 时间戳的时区不能被切掉最后一位（short-iso 是 25 个字符）。
        self.assertEqual(evs[0]["at"], "2026-10-23T21:02:30+08:00")

    def test_trigger_process_never_enters_the_list(self):
        evs = ms._oom_events(HOST_LOG)
        joined = " ".join(e["line"] for e in evs)
        self.assertNotIn("invoked oom-killer", joined)
        self.assertNotIn("postgres", " ".join(e["comm"] or "" for e in evs))

    def test_task_line_and_reaper_line_merge_into_one(self):
        text = ("2026-10-31T09:27:00+08:00 h kernel: oom-kill:constraint=NONE,"
                "task:node,pid=42,uid=0\n"
                "2026-10-31T09:27:00+08:00 h kernel: oom_reaper: reaped process 42 (node)\n")
        evs = ms._oom_events(text)
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["pid"], evs[0]["comm"]), (42, "node"))

    def test_unparsable_line_is_kept_raw(self):
        """猜错的受害者比没有受害者更容易把人引到错方向，原文照留。"""
        evs = ms._oom_events("2026-10-31T09:27:00+08:00 h kernel: "
                             "Out of memory: some line we cannot parse")
        self.assertEqual(len(evs), 1)
        self.assertIsNone(evs[0]["pid"])
        self.assertIn("cannot parse", evs[0]["line"])

    def test_unrelated_lines_ignored(self):
        self.assertEqual(ms._oom_events("kernel: normal boot message\n"), [])


class PayloadTests(unittest.TestCase):
    def test_total_and_omitted_are_reported(self):
        text = "\n".join(f"at{i} kernel: Out of memory: Killed process {i} (p{i})"
                         for i in range(12))
        d, _ = _run(text, 12)
        self.assertEqual(len(d["events"]), ms.MAX_OOM_EVENTS)
        self.assertEqual(d["events_total"], 12)
        self.assertEqual(d["events_omitted"], 12 - ms.MAX_OOM_EVENTS)

    def test_no_omission_below_the_display_cap(self):
        d, _ = _run(HOST_LOG, 3)
        self.assertEqual(d["events_total"], 3)
        self.assertFalse(d["events_omitted"])
        self.assertFalse(d["truncated"])

    def test_truncated_when_the_tail_fills_the_line_cap(self):
        with mock.patch.object(ms, "OOM_LOG_LINES", 5):
            text = "\n".join(f"at{i} kernel: filler" for i in range(5))
            d, run = _run(text, 2)
        self.assertTrue(d["truncated"])
        self.assertEqual(d["line_limit"], 5)
        self.assertIn("-n 5", " ".join(run.call_args[0][0]))

    def test_not_truncated_when_the_tail_is_shorter_than_the_cap(self):
        d, _ = _run(HOST_LOG, 3)
        self.assertFalse(d["truncated"])


class FindingsHonestyTests(unittest.TestCase):
    def _f(self, oom: dict) -> list[dict]:
        return ms._findings({"ok": False, "reason": "test"}, {"ok": False}, {"ok": False},
                            {"ok": True, "items": [], "seen": 0, "omitted": 0},
                            oom, {"swappiness": 60, "overcommit_memory": 0})

    def _oom_finding(self, oom: dict) -> dict:
        return next(f for f in self._f(oom) if f["id"] == "oom-kills")

    def test_victims_are_named_once_each(self):
        d, _ = _run(HOST_LOG, 3)
        detail = self._oom_finding(d)["detail"]
        for pid in (250726, 2002758, 2709534):
            self.assertEqual(detail.count(f"python({pid})"), 1, detail)

    def test_count_gap_names_the_reason(self):
        oom = _oom(kernel_total=3, source="journal", truncated=True,
                   events_total=2,
                   events=[{"pid": 1, "comm": "a", "at": "x", "line": "y"},
                           {"pid": 2, "comm": "b", "at": "x", "line": "y"}])
        detail = self._oom_finding(oom)["detail"]
        self.assertIn("内核计数 3 次，日志里只认到 2 次", detail)
        self.assertIn("只读了末尾一段 kernel 日志", detail)

    def test_omitted_rows_are_declared(self):
        oom = _oom(kernel_total=12, source="journal", events_total=12,
                   events_omitted=4,
                   events=[{"pid": i, "comm": f"p{i}", "at": "x", "line": "y"}
                           for i in range(8)])
        detail = self._oom_finding(oom)["detail"]
        self.assertIn("另有 4 个没列出", detail)


class DoctorAndCliTests(unittest.TestCase):
    def _data(self, oom: dict) -> dict:
        return {"mem": {"ok": True, "used_pct": 70, "available": 1024 ** 3},
                "oom": oom,
                "findings": [{"severity": ms.SEV_RISK, "title": "内核 OOM 杀过 3 次进程"}],
                "counts": {"risk": 1, "warn": 0, "info": 0}}

    def test_doctor_says_when_the_log_lags_the_counter(self):
        oom = _oom(kernel_total=3, source="journal", events_total=2)
        item = ms.doctor_item(self._data(oom))
        self.assertEqual(item["status"], "fail")
        self.assertIn("kernel 日志里只认到 2 次", item["detail"])

    def test_doctor_stays_quiet_when_all_kills_are_recognized(self):
        oom = _oom(kernel_total=3, source="journal", events_total=3)
        item = ms.doctor_item(self._data(oom))
        self.assertNotIn("只认到", item["detail"])

    def _cli(self, oom: dict) -> str:
        return cli._memory_text({"mem": {"ok": False, "reason": "x"}, "oom": oom,
                                 "counts": {"risk": 1}})

    def test_cli_declares_omitted_truncated_and_gap(self):
        oom = _oom(kernel_total=12, source="journal", truncated=True, line_limit=3000,
                   events_total=12, events_omitted=4,
                   events=[{"pid": i, "comm": f"p{i}", "at": "x", "line": "y"}
                           for i in range(8)])
        text = self._cli(oom)
        self.assertIn("名单只列最近 8 个，另有 4 个没列出", text)
        self.assertIn("kernel 日志只读了最后 3000 行", text)

    def test_cli_names_the_counter_gap(self):
        oom = _oom(kernel_total=3, source="journal", events_total=2,
                   events=[{"pid": 1, "comm": "a", "at": "x", "line": "y"}])
        self.assertIn("内核计数 3 次，日志里只认到 2 次", self._cli(oom))

    def test_cli_does_not_invent_a_cap_when_the_tail_is_whole(self):
        oom = _oom(kernel_total=1, source="journal", events_total=1,
                   events=[{"pid": 1, "comm": "a", "at": "x", "line": "y"}])
        text = self._cli(oom)
        self.assertNotIn("只读了最后", text)
        self.assertNotIn("没列出", text)


class FrontendContractTests(unittest.TestCase):
    """页面的说法只能来自载荷，不能在模板里再抄一份 3000 或「这几行」。"""

    SRC = (REPO / "web/src/views/Services.vue").read_text(encoding="utf-8")

    def test_caption_reads_the_payload_fields(self):
        self.assertIn("oomBlock.truncated", self.SRC)
        self.assertIn("oomBlock.line_limit", self.SRC)
        self.assertIn("oomBlock.events_omitted", self.SRC)
        self.assertIn("oomBlock.events_total", self.SRC)

    def test_no_copy_of_the_line_cap(self):
        self.assertNotIn("3000 行", self.SRC)

    def test_pid_is_the_row_key(self):
        self.assertIn("r.pid ?? r.line", self.SRC)


if __name__ == "__main__":
    unittest.main()
