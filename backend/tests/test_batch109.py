"""批次109：应用详情的进程读数——systemctl 查不成时，一格也不许替它把话说完。

`GET /api/apps/{id}/proc` 这份载荷里，判词早就只有一处出处（批次㉜把
`activating` / `not-found` / 「systemctl 查失败」三档收进了 `systemd_ops.derive_state`，
查不成时 `state` 如实是 `unknown`）。可同一轮返回的另外几格却还在自己兜底：

1. `restarts = _int("NRestarts") or 0`——查不到就是「重启 0 次」。0 不是空格，是一个断言：
   「这个服务从没重启过」。而 systemd 的 `StartLimitBurst` 账就记在这个数上，一个反复
   crash-loop 的应用，恰好最容易在 systemctl 超时的那一轮被读成「一次都没重启」，
   于是照着界面判断的人会排掉整条重启线索。老 systemd / 外部 unit 根本不回显这个属性，
   答案同样是 0。
2. `subState = got.get("SubState") or "unknown"`——把「没读到子状态」当成 systemd 报的子状态
   端出去，中文界面上直接出现一格英文 `unknown`，和旁边 `状态未知` 那个标签看着像两回事。
3. 前端 `p.running && p.pid ? … : '未运行'`——`running` 在 state 读不成时是 False，于是
   PID 格把「这次没查到」画成「这个服务已停止」，正是批次㉜从存活口径里清掉的那句话；
   CPU 累计、重启次数、自启动时间、子状态四格在没读到时一律留空白，而空白格和
   「这个页面没有这个字段」同形。

这一批：后端逐字段说「读到了多少」（读不到给 null），前端一格一条读法，短名仍只有
`ui.ts` 的 `UNKNOWN_LABEL` 一个出处。真实读到的 0（活跃且 `NRestarts=0`）照原样印成 `0`。
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch109_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch109_roots_"))

from app import config  # noqa: E402
from app.services import apps_service  # noqa: E402

REPO = Path(config.BASE)
SVC_SRC = (REPO / "backend" / "app" / "services" / "apps_service.py").read_text(encoding="utf-8")
ROUTER_SRC = (REPO / "backend" / "app" / "routers" / "apps.py").read_text(encoding="utf-8")
UI_TS = (REPO / "web" / "src" / "ui.ts").read_text(encoding="utf-8")
DETAIL_VUE = (REPO / "web" / "src" / "views" / "AppDetail.vue").read_text(encoding="utf-8")


async def _proc(out: str, code: int = 0) -> dict:
    """喂一段 systemctl show 的原文给 app_proc，其余依赖全部替掉。"""

    async def fake_run(cmd, args, **kw):
        return {"code": code, "out": out}

    app = {"name": "demo", "unit_override": ""}
    with mock.patch.object(apps_service, "get_app", lambda _id: app), \
         mock.patch.object(apps_service, "run", fake_run):
        return await apps_service.app_proc(1)


ACTIVE = ("Id=panel-demo.service\nMainPID=55\nLoadState=loaded\nActiveState=active\n"
          "SubState=running\nNRestarts=0\nMemoryCurrent=1048576\nCPUUsageNSec=2000000\n"
          "ExecMainStartTimestamp=Thu 2026-10-15 09:00:00 CST\n")


class TestUnreadableReadsAreNull(unittest.IsolatedAsyncioTestCase):
    async def test_probe_failure_is_not_zero_restarts(self):
        got = await _proc("[Errno 2] No such file or directory: 'systemctl'", code=127)
        self.assertEqual(got["state"], "unknown")
        self.assertIsNone(got["restarts"], "查不成 ≠ 「这个服务从没重启过」")
        self.assertIsNone(got["subState"], "unknown 不是 systemd 报的子状态")
        self.assertIsNone(got["pid"])
        self.assertIsNone(got["memoryBytes"])

    async def test_real_zero_stays_zero(self):
        got = await _proc(ACTIVE)
        self.assertEqual(got["state"], "active")
        self.assertEqual(got["restarts"], 0, "活跃且 NRestarts=0 是真读数，不许一并糊成未读到")
        self.assertEqual(got["subState"], "running")
        self.assertEqual(got["pid"], 55)

    async def test_unsupported_property_is_null(self):
        """老 systemd / 外部 unit 不回显 NRestarts：那一格的答案是没读到，不是 0。"""
        got = await _proc(ACTIVE.replace("NRestarts=0\n", ""))
        self.assertEqual(got["state"], "active")
        self.assertIsNone(got["restarts"])
        self.assertEqual(got["subState"], "running", "同轮里读到的别的字段照常报")

    async def test_empty_value_is_null(self):
        got = await _proc(ACTIVE.replace("NRestarts=0", "NRestarts="))
        self.assertIsNone(got["restarts"])

    async def test_unstarted_unit_reports_no_timestamp(self):
        got = await _proc("Id=panel-demo.service\nMainPID=0\nLoadState=loaded\n"
                          "ActiveState=inactive\nSubState=dead\nNRestarts=3\n")
        self.assertEqual(got["restarts"], 3)
        self.assertIsNone(got["execStartAt"])
        self.assertIsNone(got["activeSince"])

    async def test_the_collapse_is_gone_from_the_reader(self):
        self.assertNotIn('_int("NRestarts") or 0', SVC_SRC)
        self.assertNotIn('or "unknown"', SVC_SRC)
        block = SVC_SRC[SVC_SRC.index("async def app_proc("):]
        self.assertIn('"restarts": _int("NRestarts"),', block)
        self.assertIn('"subState": got.get("SubState") or None,', block)

    async def test_the_route_does_not_re_coalesce(self):
        start = ROUTER_SRC.index('@router.get("/api/apps/{app_id}/proc")')
        rest = ROUTER_SRC[start + 10:]
        nxt = rest.find("\n@router.")
        handler = rest[:nxt] if nxt > 0 else rest
        self.assertIn("app_proc", handler, "路由里读进程的那一节变了，这条守卫就得跟着找新位置")
        self.assertNotIn("or 0", handler, "路由再把 null 折回 0，服务层就白改了")


class TestFrontendReadsEachCell(unittest.TestCase):
    def _res_items(self) -> str:
        start = DETAIL_VUE.index("const resItems = computed(")
        return DETAIL_VUE[start:DETAIL_VUE.index("});", start) + 3]

    def test_no_blank_cell_left(self):
        for line in self._res_items().splitlines():
            if "label:" not in line:
                continue
            self.assertNotIn(": ''", line, f"空白格和「没有这个字段」同形：{line.strip()}")
            self.assertNotIn("|| ''", line, f"空白格和「没有这个字段」同形：{line.strip()}")

    def test_pid_does_not_re_derive_liveness(self):
        self.assertNotIn("p.running && p.pid", DETAIL_VUE, "PID 格不许在 state 之外另起一套存活口径")
        self.assertRegex(DETAIL_VUE, r"p\.state === 'unknown' \? UNKNOWN_LABEL : '未运行'")

    def test_restarts_and_substate_go_through_one_reader(self):
        self.assertIn("cellText(p.restarts)", self._res_items())
        self.assertIn("cellText(p.subState)", self._res_items())
        self.assertIn("cellText(p.execStartAt || p.activeSince)", self._res_items())

    def test_short_name_has_one_source(self):
        self.assertEqual(UI_TS.count("export const UNKNOWN_LABEL"), 1)
        self.assertEqual(len(re.findall(r"export function cellText", UI_TS)), 1)
        self.assertIn("if (v === null || v === undefined || v === '') return UNKNOWN_LABEL;", UI_TS)
        self.assertNotIn("未读到", DETAIL_VUE, "短名从 UNKNOWN_LABEL 来，别在页面里另写一份")


if __name__ == "__main__":
    unittest.main()
