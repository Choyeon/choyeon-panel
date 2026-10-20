"""批次㉞：后端「一个事实一个出处」收尾 + ?refresh=1 探测风暴合并。

三件事各自都会在线上咬人，所以各自钉住：

1. app/probe.py —— 七个只读探针原来各抄一份「TTL 缓存 + 一把锁」，抄出的差别是：
   updates / exposure / login 拿锁前不查缓存（`apt list` 跑 120 秒时整页转圈），
   拿锁后不复查（两人同时点「重新探测」就跑两轮完整探测）。这里按**到达时间**合并，
   不用「最少间隔」限流：单次点击永远真的回源，连点与多人只跑一轮。
2. systemd_ops.unit_states 的 alias / 裸名 —— 本机实测 `-- sshd` 回 Id=ssh.service、
   `-- fail2ban` 回 Id=fail2ban.service。按 Id 建键再把请求名 setdefault 成"未知"，
   正在跑的 sshd 与在跑的爆破防护都会被读成"查不到"。
3. 判词的措辞只有一个出处：严重度常量与标签（app/severity.py）、unit 状态说法、
   字节换算（app/util.human_bytes）、时区名（systemd_ops.timezone_name）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
from app import probe, severity  # noqa: E402
from app import util as app_util
from app.services import (  # noqa: E402
    exposure_service,
    load_service,
    login_audit_service,
    memory_service,
    pending_service,
    storage_service,
    systemd_ops,
    updates_service,
)

# (服务模块, 入口名)：七个探针都必须走同一个骨架，别再让第八份手抄。
PROBES = [
    (updates_service, "detect"),
    (exposure_service, "audit"),
    (login_audit_service, "audit"),
    (storage_service, "audit"),
    (memory_service, "audit"),
    (pending_service, "audit"),
    (load_service, "audit"),
]


def run(coro):
    return asyncio.run(coro)


class ProbeSkeletonTests(unittest.TestCase):
    """骨架本身的行为：合并、不合并、不排队读、时间戳打在哪一刻。"""

    def setUp(self):
        self.cache = probe.new_cache()
        self.lock = asyncio.Lock()
        self.calls = []

    def collect(self, gate=None, delay=0.0):
        def make():
            async def _c():
                self.calls.append(1)
                if gate is not None:
                    await gate.wait()
                if delay:
                    await asyncio.sleep(delay)
                return {"as_of": f"round-{len(self.calls)}", "n": len(self.calls)}

            return _c()

        return make

    def test_first_call_probes_and_second_hits_cache(self):
        first = run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=300))
        second = run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=300))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("refresh_merged", second)

    def test_expired_cache_probes_again(self):
        run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=300))
        self.cache["at"] -= 301
        out = run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=300))
        self.assertFalse(out["cached"])
        self.assertEqual(len(self.calls), 2)

    def test_refresh_after_a_finished_probe_still_probes(self):
        """刷新按钮不许被间隔悄悄吃掉：它到达时没人回过源，就真的回源。"""
        run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=3000))
        out = run(probe.read(self.cache, self.lock, self.collect(), refresh=True, ttl=3000))
        self.assertFalse(out["cached"])
        self.assertEqual(len(self.calls), 2)

    def test_concurrent_refreshes_collapse_to_one_probe(self):
        """风暴的那一档：三个人同时点「重新探测」，机器只该跑一轮。"""
        gate = asyncio.Event()

        async def scenario():
            tasks = [asyncio.create_task(
                probe.read(self.cache, self.lock, self.collect(gate), refresh=True, ttl=300))
                for _ in range(3)]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(len(self.calls), 1, "还在排队就该合并，不该各跑一轮")
            gate.set()
            return await asyncio.gather(*tasks)

        outs = run(scenario())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual({o["n"] for o in outs}, {1})
        self.assertFalse(outs[0]["cached"])
        self.assertTrue(all(o["cached"] and o.get("refresh_merged") for o in outs[1:]))

    def test_mixed_refresh_and_plain_readers_share_one_probe(self):
        gate = asyncio.Event()

        async def scenario():
            tasks = [asyncio.create_task(
                probe.read(self.cache, self.lock, self.collect(gate), refresh=refresh, ttl=300))
                for refresh in (True, False, True, False)]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            gate.set()
            return await asyncio.gather(*tasks)

        run(scenario())
        self.assertEqual(len(self.calls), 1)

    def test_plain_reader_does_not_queue_behind_a_slow_probe(self):
        """拿锁前不查缓存的那个旧写法：apt 在跑时，一个不带 refresh 的普通读取也得等它。"""
        gate = asyncio.Event()

        async def scenario():
            run_task = asyncio.create_task(
                probe.read(self.cache, self.lock, self.collect(gate), refresh=True, ttl=300))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.cache["data"] = {"as_of": "warm", "n": 0}
            self.cache["at"] = probe.time.monotonic()
            warm = await asyncio.wait_for(
                probe.read(self.cache, self.lock, self.collect(gate), refresh=False, ttl=300),
                timeout=0.2)
            gate.set()
            await run_task
            return warm

        warm = run(scenario())
        self.assertEqual(warm["n"], 0, "缓存里已有的一份就该直接给，不用等那轮慢探测")
        self.assertTrue(warm["cached"])

    def test_stamp_is_taken_after_the_probe(self):
        """TTL 从探测**结束**起算：从开始前取时刻，慢探测会把缓存寿命吃掉一截。"""
        clock = [1000.0]

        async def slow():
            self.calls.append(1)
            clock[0] += 120.0
            return {"as_of": "x"}

        with mock.patch.object(probe.time, "monotonic", lambda: clock[0]):
            run(probe.read(self.cache, self.lock, slow, refresh=False, ttl=300))
        self.assertEqual(self.cache["at"], 1120.0)

    def test_reset_forces_a_new_probe_but_keeps_the_shape(self):
        run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=3000))
        probe.reset(self.cache)
        self.assertIsNone(self.cache["data"])
        out = run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=3000))
        self.assertFalse(out["cached"])
        self.assertEqual(len(self.calls), 2)

    def test_served_copy_holds_the_flag_out_of_the_cache(self):
        run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=3000))
        out = run(probe.read(self.cache, self.lock, self.collect(), refresh=False, ttl=3000))
        self.assertTrue(out["cached"])
        self.assertIsNone(self.cache["data"].get("cached"), "缓存本体不该带这一次请求的标记")


class ProbeWiringTests(unittest.TestCase):
    """七个入口都接到同一个骨架上：参数带着自己的 cache/lock/ttl，refresh 一路传下去。"""

    def test_each_probe_uses_the_shared_read(self):
        for module, name in PROBES:
            with self.subTest(probe_module=module.__name__):
                seen = {}

                async def read(cache, lock, collect, *, refresh, ttl, **kw):
                    seen.update(cache=cache, lock=lock, refresh=refresh, ttl=ttl)
                    return {"ok": True}

                with mock.patch.object(probe, "read", read):
                    out = run(getattr(module, name)(refresh=True))
                self.assertEqual(out, {"ok": True})
                self.assertIs(seen["cache"], module._cache, f"{name} 没有用自己的 _cache")
                self.assertIs(seen["lock"], module._LOCK, f"{name} 没有用自己的 _LOCK")
                self.assertTrue(seen["refresh"])
                self.assertEqual(seen["ttl"], module.CACHE_TTL)

    def test_caches_share_the_probe_shape(self):
        for module, _name in PROBES:
            with self.subTest(module=module.__name__):
                self.assertIn("gen", module._cache, "缓存形状要有回源轮次，合并才有依据")
                self.assertIn("at", module._cache)
                self.assertIsInstance(module._LOCK, asyncio.Lock)

    def test_reset_cache_clears_every_probe(self):
        """暴露 reset_cache 的那几个（测试与巡检都在按这个名字清缓存）真的清干净。"""
        for module, _name in PROBES:
            if not hasattr(module, "reset_cache"):
                continue
            with self.subTest(module=module.__name__):
                module._cache["data"] = {"as_of": "x"}
                module.reset_cache()
                self.assertIsNone(module._cache["data"])


class UnitStatesNameTests(unittest.TestCase):
    """systemctl show 的回显名字和请求名字不是一回事，判词得按回显的 Names 认领。"""

    def ask(self, units, out):
        async def fake_run(cmd, args, **kw):
            self.assertEqual(cmd, "systemctl")
            self.assertIn("Names", args, "不回显 Names 就没法认领 alias")
            self.assertIn("--", args, "unit 名一律挡在 -- 之后")
            return {"code": 0, "out": out, "err": ""}

        with mock.patch.object(systemd_ops, "run", fake_run):
            return run(systemd_ops.unit_states(list(units)))

    def test_alias_is_resolved_for_the_requested_name(self):
        """本机实测：`show -p Id -- sshd` 回 Id=ssh.service、Names="ssh.service sshd.service"。"""
        got = self.ask(["sshd"], "Id=ssh.service\nNames=ssh.service sshd.service\n"
                                 "LoadState=loaded\nActiveState=active\nMainPID=671\n\n")
        self.assertTrue(got["sshd"]["active"],
                        "alias 查出来的在跑服务被读成「没查到」，登录安全页就永远说没防护")
        self.assertEqual(got["sshd"]["mainPid"], 671)

    def test_bare_name_matches_the_returned_id(self):
        got = self.ask(["fail2ban"], "MainPID=0\nId=fail2ban.service\nLoadState=not-found\n"
                                     "ActiveState=inactive\n\n")
        self.assertTrue(got["fail2ban"]["missing"])
        self.assertFalse(got["fail2ban"]["active"])
        self.assertEqual(got["fail2ban"]["load"], "not-found")

    def test_unanswered_name_is_unknown_not_stopped(self):
        got = self.ask(["ghost.service", "ssh.service"],
                       "Id=ssh.service\nLoadState=loaded\nActiveState=active\nMainPID=1\n\n")
        self.assertEqual(got["ghost.service"]["state"], "unknown")
        self.assertTrue(got["ssh.service"]["active"])

    def test_states_do_not_bleed_across_blocks(self):
        """属性顺序由 systemd 决定（本机实测 MainPID 排在 Id 之前），认领必须按块。"""
        out = ("MainPID=111\nId=a.service\nActiveState=active\nLoadState=loaded\n\n"
               "MainPID=0\nId=b.service\nActiveState=failed\nLoadState=loaded\n\n")
        got = self.ask(["a.service", "b.service"], out)
        self.assertEqual((got["a.service"]["mainPid"], got["b.service"]["mainPid"]), (111, 0))
        self.assertTrue(got["a.service"]["active"])
        self.assertFalse(got["b.service"]["active"])
        self.assertFalse(got["b.service"]["busy"], "failed 不是「正在占着文件」，恢复流程可以动它")

    def test_transition_states_are_the_documented_pair(self):
        self.assertEqual(set(systemd_ops.TRANSITION_STATES),
                         set(systemd_ops.BUSY_STATES) - set(systemd_ops.RUNNING_STATES))
        got = self.ask(["x.service"], "Id=x.service\nLoadState=loaded\nActiveState=activating\nMainPID=0\n\n")
        self.assertFalse(got["x.service"]["active"])
        self.assertTrue(got["x.service"]["busy"])

    def test_failed_counts_as_busy_never_as_running(self):
        """failed 既不在 RUNNING 也不在 BUSY：进程已经死了，但可以 reset-failed 再启动。"""
        got = self.ask(["x.service"], "Id=x.service\nLoadState=loaded\nActiveState=failed\nMainPID=0\n\n")
        self.assertFalse(got["x.service"]["active"])
        self.assertFalse(got["x.service"]["busy"])


class PendingDaemonReloadAttributionTests(unittest.TestCase):
    def test_need_reload_follows_its_own_block(self):
        listed = mock.Mock(returncode=0, stdout="  a.service  loaded active running A\n"
                                                "  b.service  loaded active exited  B\n", stderr="")
        # 真实回显是空行分块，且顺序不固定：MainPID 排在 Id 前面（本机实测）。
        shown = mock.Mock(returncode=0, stderr="",
                          stdout="MainPID=1\nId=a.service\nNeedDaemonReload=no\n\n"
                                 "NeedDaemonReload=yes\nMainPID=0\nId=b.service\n\n")
        with mock.patch.object(pending_service.subprocess, "run", mock.Mock(side_effect=[listed, shown])):
            out = pending_service._systemd_state()
        self.assertTrue(out["ok"])
        self.assertEqual(out["need_reload"], ["b.service"], "点名叫错了服务比不点名更糟")

    def test_need_reload_uses_the_requested_names(self):
        """alias 请求（sshd）回显 Id=ssh.service：这一条按回显点名，别把 alias 名字编进列表。"""
        listed = mock.Mock(returncode=0, stdout="  sshd.service  loaded active running S\n", stderr="")
        shown = mock.Mock(returncode=0, stderr="",
                          stdout="Id=sshd.service\nNames=ssh.service sshd.service\nNeedDaemonReload=yes\n\n")
        with mock.patch.object(pending_service.subprocess, "run", mock.Mock(side_effect=[listed, shown])):
            out = pending_service._systemd_state()
        self.assertEqual(out["need_reload"], ["sshd.service"])


class TimezoneNameTests(unittest.TestCase):
    """OnCalendar 的时区名只有一个出处：定时任务与备份 unit 读同一个函数。"""

    def test_etc_timezone_wins(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="Asia/Shanghai\n")), \
             mock.patch.object(systemd_ops.os.path, "realpath", lambda _p: "/nope"):
            self.assertEqual(systemd_ops.timezone_name(), "Asia/Shanghai")

    def test_falls_back_to_localtime_symlink(self):
        with mock.patch("builtins.open", mock.Mock(side_effect=OSError("no /etc/timezone"))), \
             mock.patch.object(systemd_ops.os.path, "realpath",
                               lambda _p: "/usr/share/zoneinfo/Asia/Tokyo"):
            self.assertEqual(systemd_ops.timezone_name(), "Asia/Tokyo")

    def test_never_returns_an_empty_zone(self):
        with mock.patch("builtins.open", mock.Mock(side_effect=OSError())), \
             mock.patch.object(systemd_ops.os.path, "realpath", lambda _p: "/etc/localtime"):
            self.assertEqual(systemd_ops.timezone_name(), "UTC")

    def test_cron_and_backup_share_the_source(self):
        from app.services import backup_service, cron_service

        for module in (cron_service, backup_service):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "_tz"), "时区名别再各留一份私有实现")


class HumanBytesTests(unittest.TestCase):
    def test_missing_value_is_a_dash_not_zero(self):
        self.assertEqual(app_util.human_bytes(None), "—")
        self.assertEqual(app_util.human_bytes("abc"), "—")
        self.assertEqual(app_util.human_bytes(0), "0B")

    def test_negative_is_clamped_and_units_scale(self):
        self.assertEqual(app_util.human_bytes(-5), "0B")
        self.assertEqual(app_util.human_bytes(1023), "1023B")
        self.assertEqual(app_util.human_bytes(1024 * 1024 * 1024 * 1024), "1.0TB")

    def test_overflow_stops_at_petabytes(self):
        self.assertEqual(app_util.human_bytes(3 * 1024 ** 5), "3.0PB")
        self.assertEqual(app_util.human_bytes(9 * 1024 ** 7), "9437184.0PB", "再大也不该跑出单位表")

    def test_no_private_copies_left_in_the_services(self):
        for module in (storage_service, memory_service):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "_human"), "字节换算只有 app.util.human_bytes 一处")


class SeveritySourceTests(unittest.TestCase):
    def test_probes_write_the_constants_not_strings(self):
        for module in (load_service, pending_service, memory_service, storage_service):
            with self.subTest(module=module.__name__):
                self.assertIs(module.SEV_RISK, severity.SEV_RISK)
                self.assertIs(module.SEV_WARN, severity.SEV_WARN)
                self.assertIs(module.SEV_INFO, severity.SEV_INFO)

    def test_the_cli_reads_the_table_from_its_source(self):
        """终端那句「要紧/值得处理/知道就好」直接取 app.severity，不再借某个服务模块转一手：
        借道意味着换个 CLI 命令就得先猜那个服务恰好 import 过没有。"""
        src = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8")
        self.assertIn("severity.SEV_LABEL", src)
        self.assertNotIn("_service.SEV_LABEL", src)
        for module in (load_service, pending_service, memory_service, storage_service,
                       login_audit_service):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "SEV_LABEL"), "标签表只有一个出处，服务层不转发")

    def test_order_covers_every_labelled_severity(self):
        self.assertEqual(set(severity.SEV_ORDER), set(severity.SEV_LABEL))
        self.assertEqual(severity.SEV_ORDER[severity.SEV_RISK],
                         min(severity.SEV_ORDER.values()))

    def test_info_and_note_read_the_same(self):
        """两档「知道就好」是历史包袱（改名等于改接口），措辞必须一致。"""
        self.assertEqual(severity.SEV_LABEL[severity.SEV_INFO], severity.SEV_LABEL[severity.SEV_NOTE])

    def test_the_one_sorting_site_uses_the_shared_order(self):
        self.assertIs(login_audit_service.SEV_ORDER, severity.SEV_ORDER)


class LoginGuardVerdictTests(unittest.TestCase):
    """防护判词经过 CLI 与自检之后说的还是同一句话——而且「没核对成」不等于「没防护」。"""

    @staticmethod
    def st(state: str, load: str = "loaded") -> dict:
        return systemd_ops.derive_state({"Id": "x.service", "LoadState": load,
                                         "ActiveState": state, "MainPID": "0"})

    def unit(self, name: str, st: dict) -> dict:
        return {"name": name, "state": st["state"], "active": st["active"], "busy": st["busy"],
                "missing": st["missing"], "unknown": st["state"] == "unknown",
                "desc": login_audit_service._unit_desc(st)}

    def test_unknown_is_not_reported_as_unguarded(self):
        """systemctl 没回话（UNKNOWN_STATE）不能被写成「都没在跑」：那是把探测失败说成安全结论。"""
        units = [self.unit("fail2ban", systemd_ops.UNKNOWN_STATE),
                 self.unit("sshguard", systemd_ops.UNKNOWN_STATE)]
        self.assertEqual(login_audit_service._guard_verdict(units), "unknown")
        self.assertEqual(login_audit_service._guard_phrase({"units": units, "status": "unknown"}),
                         "这一项没核对成")

    def test_pending_is_not_reported_as_unguarded(self):
        units = [self.unit("fail2ban", self.st("activating")), self.unit("sshguard", self.st("inactive"))]
        self.assertEqual(login_audit_service._guard_verdict(units), "pending")

    def test_one_confirmed_stopped_unit_is_a_verdict_not_an_unknown(self):
        """一个核对到没在跑、另一个查失败：「没有防护在跑」这句是核对过的，不必闭嘴。"""
        units = [self.unit("fail2ban", self.st("inactive")),
                 self.unit("sshguard", systemd_ops.UNKNOWN_STATE)]
        self.assertEqual(login_audit_service._guard_verdict(units), "unguarded")

    def test_unverified_guard_downgrades_the_doctor_pass_to_warn(self):
        data = {"ok": True, "as_root": True, "sshd": {"source": "sshd -T", "effective": True,
                                                     "port": 22, "reachable": False,
                                                     "verdict": "expected", "reason": ""},
                "settings": [], "keys": {"files": [], "truncated": False},
                "accounts": {"empty_password": [], "uid0_extra": []},
                "attempts": {"ok": True, "failed": 0}, "counts": {"risk": 0, "warn": 0, "note": 0},
                "findings": [], "notes": [],
                "guard": {"status": "unknown", "phrase": "这一项没核对成", "units": []}}
        item = login_audit_service.doctor_item(data)
        self.assertEqual(item["status"], "warn")
        self.assertIn("没核对成", item["detail"])

    def test_verified_guard_keeps_the_pass(self):
        data = {"ok": True, "as_root": True, "sshd": {"source": "sshd -T", "effective": True,
                                                     "port": 22, "reachable": False,
                                                     "verdict": "expected", "reason": ""},
                "settings": [], "keys": {"files": [], "truncated": False},
                "accounts": {"empty_password": [], "uid0_extra": []},
                "attempts": {"ok": True, "failed": 0}, "counts": {"risk": 0, "warn": 0, "note": 0},
                "findings": [], "notes": [],
                "guard": {"status": "guarded", "phrase": "fail2ban 正在跑",
                          "units": [self.unit("fail2ban", self.st("active"))]}}
        self.assertEqual(login_audit_service.doctor_item(data)["status"], "pass")

    def test_cli_prints_the_same_phrase_as_the_page(self):
        """CLI 只搬运 phrase/desc，不再自己比较状态串：判词若在终端再长一套，迟早和页面不一致。"""
        guard = {"units": [{"name": "fail2ban", "state": "inactive", "active": False, "busy": False,
                            "missing": False, "unknown": False, "desc": "没在跑"},
                           {"name": "sshguard", "state": "unknown", "active": False, "busy": False,
                            "missing": False, "unknown": True, "desc": "没查到"}],
                 "status": "unguarded"}
        guard["phrase"] = login_audit_service._guard_phrase(guard)
        data = {
            "sshd": {"source": "sshd -T", "port": 22, "reachable": False},
            "counts": {"risk": 0, "warn": 0, "note": 0},
            "attempts": {"ok": True, "failed": 0, "distinct_ips": 0, "top": [], "accepted": []},
            "keys": {"files": [], "total_keys": 0, "scanned": 0},
            "accounts": {"login_count": 1, "empty_password": [], "uid0_extra": []},
            "findings": [], "settings": [], "notes": [],
            "panel": {"ok": True, "accounts": 1, "with_totp": 1, "recent_failed": 0},
            "guard": guard,
        }
        text = cli._ssh_audit_text(data)
        line = next(ln for ln in text.splitlines() if ln.startswith("爆破防护："))
        self.assertEqual(line, "爆破防护：fail2ban 没在跑（fail2ban 没在跑、sshguard 没查到）")

    def test_one_name_still_reads_like_chinese(self):
        """只剩一个核对到的单元时不说「都没在跑」。"""
        guard = {"units": [{"name": "fail2ban", "state": "inactive", "active": False, "busy": False,
                            "missing": False, "unknown": False, "desc": "没在跑"},
                           {"name": "sshguard", "state": "unknown", "active": False, "busy": False,
                            "missing": False, "unknown": True, "desc": "没查到"}],
                 "status": "unguarded"}
        self.assertEqual(login_audit_service._guard_phrase(guard), "fail2ban 没在跑")


if __name__ == "__main__":
    unittest.main()
