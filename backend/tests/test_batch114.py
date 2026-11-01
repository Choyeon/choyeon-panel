"""批次114：自检与终端里的四处「读不到 = 确认没有 / 绿灯」。

同一类塌陷（见批次75、98、106、110、112、113）在这四处各自站着一份：

- `meta._check_backup`：份数来自 `glob`，而 glob 把 `opendir` 的异常吞成空列表。于是一台
  备份全在、但面板读不动 `/root/backups/panel` 的机器，在自检里读到的是「备份目录下还没有备份」
  ——这一项查的正是「出事还能不能重来」，把没查成写成没有，等于把最要紧的账销掉。现在
  `dir_state` 先判读不读得动，读不动只说「确认不了」；顺带把 `_recent_backups` 里 glob 与
  `getmtime` 之间被轮替删掉的文件跳过（批次96 给 CLI 那条读者修过同一件事，这里是它的另一半，
  原来是未处理异常，整条 doctor 打成 500）。
- `meta._unit_environment`：读不动时旧写法回一张空表，调用方拿 `k in unit_env` 当筛子，
  空表让每一项都落不进对账，最后写出「N 项部署参数一致」这条绿灯。现在回（表，原因），
  原因非空就是这一轮没核对成。
- `updates_service._apt_cache_age`：`apt list` 读的是本地索引，索引旧则结论旧。旧写法一律回
  None，`_clean_verdict` 把 None 当「没有陈旧问题」，对着量不到龄的机器给「没有待装安全更新」。
  现在回（年龄，原因），`index_age_reason` 进载荷，判词、CLI 与页签共用这一个出处；
  「这台机器不是 Debian 族、压根没有索引这回事」不带原因，仍走 pass。
- `cli._memory_text`：批次110 给网页与 doctor 定了 `counter_ok`（读空 ≠ 真 0），终端那条读者
  是漏下的——同一个字段读不到时它印「OOM：自启动以来没有杀过进程」，把一次没做的测量念成绿灯。

`choyeonctl backup list` 同属第一处：`(无备份)` 那份平铺 glob 也是同一个塌陷。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch114_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch114_roots_"))

import cli  # noqa: E402
from app import config  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import backup_service as B  # noqa: E402
from app.services import updates_service as us  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SERVICES_VUE = (ROOT / "web" / "src" / "views" / "Services.vue").read_text(encoding="utf-8")


class BackupDirTriState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp_b114_backup_")
        old = config.BACKUP_DIR
        config.BACKUP_DIR = self.tmp
        self.addCleanup(setattr, config, "BACKUP_DIR", old)

    def _check(self, dir_state):
        with (mock.patch.object(B, "failing_tasks", lambda: []),
              mock.patch.object(meta, "_recent_backups", lambda: (0, None)),
              mock.patch.object(meta, "dir_state", lambda p: dir_state)):
            return asyncio.run(meta._check_backup())

    def test_unreadable_directory_is_not_reported_as_absent(self):
        got = self._check((None, [], "PermissionError"))
        self.assertEqual(got["status"], "warn")
        self.assertIn("读不到备份目录", got["detail"])
        self.assertIn("PermissionError", got["detail"])
        self.assertIn("确认不了", got["detail"])
        self.assertNotIn("还没有备份", got["detail"], "读不动时不能宣布「没有备份」")
        self.assertNotIn("choyeonctl backup run", got["fix"], "修复方向是权限，不是再跑一份备份")

    def test_missing_directory_says_so(self):
        got = self._check((False, [], ""))
        self.assertEqual(got["status"], "warn")
        self.assertIn("这个目录还不存在", got["detail"])
        self.assertIn("choyeonctl backup run", got["fix"])

    def test_readable_but_empty_is_the_real_absence(self):
        got = self._check((True, ["notes.md"], ""))
        self.assertEqual(got["status"], "warn")
        self.assertIn("下还没有备份", got["detail"])

    def test_vanished_file_does_not_crash_the_whole_check(self):
        Path(self.tmp, "panel-app-1.tar.gz").write_text("x", encoding="utf-8")
        with mock.patch.object(meta.os.path, "getmtime", side_effect=FileNotFoundError):
            count, mtime = meta._recent_backups()
        self.assertEqual((count, mtime), (0, None), "少算一份，不能让整条 doctor 打成 500")

    def test_the_real_count_still_wins(self):
        Path(self.tmp, "panel-app-1.tar.gz").write_text("x", encoding="utf-8")
        count, mtime = meta._recent_backups()
        self.assertEqual(count, 1)
        self.assertIsNotNone(mtime)


class UnitEnvironmentTriState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cp_b114_unit_"))

    def test_directory_instead_of_file_returns_the_reason(self):
        (self.tmp / "x.service").mkdir()
        env, why = meta._unit_environment(self.tmp / "x.service")
        self.assertEqual(env, {})
        self.assertEqual(why, "IsADirectoryError")

    def test_missing_file_also_returns_the_reason(self):
        _env, why = meta._unit_environment(self.tmp / "nope.service")
        self.assertEqual(why, "FileNotFoundError")

    def test_readable_unit_parses_the_values(self):
        p = self.tmp / "ok.service"
        p.write_text('[Service]\nEnvironment=CP_PORT="3210"\nEnvironment=CP_HOST=127.0.0.1\n',
                     encoding="utf-8")
        env, why = meta._unit_environment(p)
        self.assertEqual(why, "")
        self.assertEqual(env, {"CP_PORT": "3210", "CP_HOST": "127.0.0.1"})

    def _check_env(self, unit_text):
        unit_dir = self.tmp / "units"
        unit_dir.mkdir()
        (unit_dir / f"{meta.SERVICE_NAME}.service").write_text(unit_text, encoding="utf-8")
        with (mock.patch.object(config, "UNIT_DIR", str(unit_dir)),
              mock.patch.object(meta.config, "_parse_env_file",
                                lambda p: {"CP_HOST": "127.0.0.1", "CP_PORT": "3210"})):
            return asyncio.run(meta._check_env_file())

    def test_unit_present_but_unreadable_is_not_a_pass(self):
        # unit 目录本身不给读：exists() 只看父目录，read_text 才是真正动手的那一步。
        unit_dir = self.tmp / "locked"
        units = unit_dir / f"{meta.SERVICE_NAME}.service"
        units.parent.mkdir(parents=True)
        units.write_text("Environment=CP_HOST=127.0.0.1\nEnvironment=CP_PORT=3210\n", encoding="utf-8")
        with (mock.patch.object(config, "UNIT_DIR", str(unit_dir)),
              mock.patch.object(meta.config, "_parse_env_file",
                                lambda p: {"CP_HOST": "127.0.0.1", "CP_PORT": "3210"}),
              mock.patch.object(Path, "read_text", side_effect=PermissionError)):
            got = asyncio.run(meta._check_env_file())
        self.assertEqual(got["status"], "warn", "读不动的 unit 不能因为「没发现差异」报一致")
        self.assertIn("确认不了", got["detail"])
        self.assertNotIn("一致", got["detail"])

    def test_aligned_values_still_pass(self):
        got = self._check_env("Environment=CP_HOST=127.0.0.1\nEnvironment=CP_PORT=3210\n")
        self.assertEqual(got["status"], "pass")
        self.assertIn("一致", got["detail"])

    def test_drift_is_still_a_fail(self):
        got = self._check_env("Environment=CP_HOST=127.0.0.1\nEnvironment=CP_PORT=3211\n")
        self.assertEqual(got["status"], "fail")
        self.assertIn("CP_PORT", got["detail"])


class AptIndexAgeTriState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cp_b114_lists_"))

    def test_empty_dir_has_a_reason_not_a_none(self):
        with mock.patch.object(us, "APT_LISTS", self.tmp):
            age, why = us._apt_cache_age()
        self.assertIsNone(age)
        self.assertIn("没有索引文件", why)

    def test_missing_dir_is_named_as_missing(self):
        with mock.patch.object(us, "APT_LISTS", self.tmp / "absent"):
            age, why = us._apt_cache_age()
        self.assertIsNone(age)
        self.assertEqual(why, "索引目录不存在")

    def test_unreadable_dir_keeps_the_exception_type(self):
        with mock.patch.object(us, "dir_state", lambda p: (None, [], "PermissionError")):
            age, why = us._apt_cache_age()
        self.assertIsNone(age)
        self.assertEqual(why, "PermissionError")

    def test_a_real_index_measures_an_age(self):
        f = self.tmp / "Packages"
        f.write_text("x", encoding="utf-8")
        stamp = time.time() - 3 * 3600
        os.utime(f, (stamp, stamp))
        with mock.patch.object(us, "APT_LISTS", self.tmp):
            age, why = us._apt_cache_age()
        self.assertEqual(why, "")
        self.assertTrue(2.5 < age < 3.5, age)

    def test_clean_verdict_needs_a_denominator(self):
        with_reason = us._clean_verdict({"index_age_hours": None, "index_age_reason": "PermissionError",
                                         "commands": {"refresh_index": "apt update"}},
                                        "没有待装安全更新", "")
        self.assertEqual(with_reason["status"], "warn")
        self.assertIn("没有可比的分母", with_reason["detail"])
        self.assertEqual(with_reason["fix"], "apt update")
        # 非 Debian 族没有 apt 索引这回事：age=None 且不带原因，仍是真 pass。
        no_index = us._clean_verdict({"index_age_hours": None, "index_age_reason": ""},
                                     "没有待装安全更新", "")
        self.assertEqual(no_index["status"], "pass")

    def _detect(self, apt_return):
        async def fake_run(cmd, args, **kw):
            return {"code": 0, "out": "Listing..."}

        patches = [
            mock.patch.object(us, "run", fake_run),
            mock.patch.object(us, "family_of", lambda m, a: us.DEBIAN),
            mock.patch.object(us, "_apt_cache_age", lambda: apt_return),
            mock.patch.object(us, "_auto_updates",
                              lambda f: _const({"state": "enabled", "unit": "u"})),
            mock.patch.object(us, "_reboot_verdict", lambda f: _const((False, None))),
            mock.patch.object(us.shutil, "which", lambda c: f"/usr/bin/{c}"),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(us._collect())
        finally:
            for p in reversed(patches):
                p.stop()

    def test_the_reason_travels_in_the_payload(self):
        data = self._detect((None, "PermissionError"))
        self.assertIsNone(data["index_age_hours"])
        self.assertEqual(data["index_age_reason"], "PermissionError")
        self.assertTrue(any("索引龄读不到" in n for n in data["notes"]), data["notes"])
        self.assertEqual(us.doctor_item(data)["status"], "warn",
                         "探测成功但量不到龄，绿灯没有分母")
        self.assertTrue(any("没有可比的分母" in n for n in [us.doctor_item(data)["detail"]]))

    def test_no_reason_stays_a_pass(self):
        data = self._detect((6.0, ""))
        self.assertEqual(us.doctor_item(data)["status"], "pass")


class CliTexts(unittest.TestCase):
    def test_updates_text_names_the_missing_denominator(self):
        text = cli._updates_text({"pending_total": 0, "security_count": 0, "probe_error": None,
                                  "index_age_hours": None, "index_age_reason": "PermissionError",
                                  "auto_updates": {"state": "enabled", "unit": "u"},
                                  "packages": [], "notes": [], "supported": True,
                                  "commands": {"refresh_index": "sudo apt update"},
                                  "checked_at": "x"})
        self.assertIn("本地 apt 索引龄读不到", text)
        self.assertIn("没有可比的分母", text)
        self.assertIn("刷新索引：  sudo apt update", text, "量不到龄也要给得出刷新命令")

    def test_updates_text_without_a_reason_stays_clean(self):
        text = cli._updates_text({"pending_total": 0, "security_count": 0, "probe_error": None,
                                  "index_age_hours": 6.0, "index_age_reason": "",
                                  "auto_updates": {"state": "enabled", "unit": "u"},
                                  "packages": [], "notes": [], "supported": True,
                                  "commands": {"refresh_index": "sudo apt update"},
                                  "checked_at": "x"})
        self.assertNotIn("没有可比的分母", text)
        self.assertNotIn("刷新索引", text)

    def _memory(self, oom):
        return cli._memory_text({"mem": {"ok": True, "total": 4_000_000_000, "used_pct": 30,
                                         "used": 1_200_000_000, "available": 2_800_000_000,
                                         "reclaimable": 0},
                                 "pressure": {"ok": True}, "rates": {"ok": True, "secs": 1},
                                 "oom": oom, "units": {"ok": True, "items": [], "seen": 1,
                                                       "managed": 0},
                                 "counts": {"risk": 0, "warn": 0, "info": 0}, "findings": []})

    def test_terminal_does_not_claim_no_kills_when_the_counter_was_not_read(self):
        text = self._memory({"kernel_total": 0, "unit_kills": {}, "counter_ok": False,
                             "reason": "读不到 /proc/vmstat 的 oom_kill 计数（这一轮没读到，不代表没杀过进程）",
                             "events": [], "events_total": 0})
        self.assertIn("这一轮没读到", text)
        self.assertNotIn("自启动以来没有杀过进程", text)

    def test_a_real_zero_is_still_reported_as_zero(self):
        text = self._memory({"kernel_total": 0, "unit_kills": {}, "counter_ok": True,
                             "reason": "", "events": [], "events_total": 0})
        self.assertIn("OOM：自启动以来没有杀过进程", text)

    def test_the_flag_is_read_fail_closed(self):
        """缺 counter_ok 的老载荷不能又落回「没杀过进程」那句话。"""
        text = self._memory({"kernel_total": 0, "unit_kills": {}, "events": [], "events_total": 0})
        self.assertNotIn("自启动以来没有杀过进程", text)


class BackupListText(unittest.TestCase):
    """`choyeonctl backup list` 的那条平铺 glob 与自检同一个读者、同一个三态。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp_b114_blist_")
        old = config.BACKUP_DIR
        config.BACKUP_DIR = self.tmp
        self.addCleanup(setattr, config, "BACKUP_DIR", old)

    def _list(self, dir_state):
        out = io.StringIO()
        with (mock.patch.object(cli, "dir_state", lambda p: dir_state),
              mock.patch.object(cli.Out, "json_mode", False),
              contextlib.redirect_stdout(out)):
            payload = asyncio.run(cli.cmd_backup(_ns("list")))
        return payload, out.getvalue()

    def test_unreadable_dir_does_not_say_no_backups(self):
        payload, text = self._list((None, [], "PermissionError"))
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["dir_unreadable"], "PermissionError")
        self.assertIn("读不到备份目录", text)
        self.assertNotIn("(无备份)", text)

    def test_missing_dir_says_missing(self):
        payload, text = self._list((False, [], ""))
        self.assertTrue(payload["dir_missing"])
        self.assertIn("这个目录还不存在", text)

    def test_real_files_still_list(self):
        Path(self.tmp, "panel-20261031.tar.gz").write_text("x", encoding="utf-8")
        payload, text = self._list((True, ["panel-20261031.tar.gz"], ""))
        self.assertEqual(payload["total"], 1)
        self.assertNotIn("dir_unreadable", payload)
        self.assertIn("panel-20261031.tar.gz", text)


class FrontendTab(unittest.TestCase):
    def test_the_chip_renders_when_only_the_reason_exists(self):
        self.assertIn('typeof patches.index_age_hours === \'number\' || patches.index_age_reason',
                      SERVICES_VUE, "原来只在年龄是数字时画这颗 chip，量不到龄时整块信息消失")

    def test_the_green_alert_waits_for_a_denominator(self):
        success = SERVICES_VUE.index("补丁已全部装上")
        self.assertLess(SERVICES_VUE.index("index_age_reason"), success,
                        "带原因的分支必须排在 success 之前，否则页签继续给绿灯")
        self.assertIn("没有可比的分母", SERVICES_VUE)

    def test_the_tab_badge_and_the_refresh_command_follow(self):
        self.assertIn("!!patches.value?.index_age_reason", SERVICES_VUE)
        self.assertIn("patches.probe_error || patches.index_stale || patches.index_age_reason",
                      SERVICES_VUE)

    def test_no_local_copy_of_the_short_name(self):
        self.assertNotIn("未读到", SERVICES_VUE)


def _const(value):
    async def _inner(*_a, **_k):
        return value
    return _inner


def _ns(cmd):
    import argparse
    return argparse.Namespace(backup_cmd=cmd)


if __name__ == "__main__":
    unittest.main()
