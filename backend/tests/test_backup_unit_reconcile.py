"""备份 unit 与 systemd failed 状态的对账回归测试。

覆盖的是生产上真实发生过的那个故障（2026-11-10 查清）：

面板从 /root/choyeon-panel 迁到 /root/www/choyeon-panel 之后，盘上留着旧前缀
渲染出来的 panel-backup-1/2.service，里面的 CP_DATA_DIR 仍指向旧目录。旧目录里
那份 panel.db 是上一代安装留下的另一份库，backups 表是空的 —— 于是每晚 03:30
runner 都读到"没有这条任务"，退出码 2。

而面板自己一切正常：它在读新库，备份任务也在新库里，界面上定时备份显示得好好的。
红字来自两个地方叠在一起：

1. **孤儿 unit**：库里没有对应任务的 unit 谁都不管（旧写法只遍历库里的行），
   它留在 /etc/systemd/system 里，timer 照常到点触发。
2. **failed 状态不会自己消失**：Type=oneshot 失败后，failed 要挂到**下一次成功
   运行**才被 systemd 清掉。备份一天只跑一次，于是红字趴一整天，
   failed-units 告警（crit 级）跟着长亮 —— 用户看到的就是"服务器正常却天天报错"。

所以修复必须两处都做：清孤儿 unit + 归零 failed 状态。只做一处，另一处照样报。
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import config
from app.services import backup_service as B


def _fake_run(calls):
    async def run(cmd, args, cwd=None, timeout=120, env=None):
        calls.append([cmd, *args])
        return {"code": 0, "out": ""}

    return run


class _UnitDirCase(unittest.TestCase):
    """给一批用例共用的环境：临时 unit 目录 + 顶掉 systemctl。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_bk_sync_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.calls: list[list] = []
        self.reset_calls: list[list] = []
        p1 = mock.patch.object(config, "UNIT_DIR", self.dir)
        p2 = mock.patch.object(B, "run", _fake_run(self.calls))
        p3 = mock.patch.object(B.systemd_ops, "reset_failed", self._fake_reset)
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    async def _fake_reset(self, unit):
        self.reset_calls.append(unit)

    def _write_unit(self, bid: int, data_dir: str | None = None, body: str | None = None) -> str:
        """在临时 unit 目录里放一个 panel-backup-<bid>.service。"""
        u = f"panel-backup-{bid}"
        path = f"{self.dir}/{u}.service"
        if body is None:
            data = data_dir if data_dir is not None else config.DATA_DIR
            body = (
                f"[Unit]\nDescription=choyeon-panel backup #{bid}\n[Service]\nType=oneshot\n"
                "WorkingDirectory=/nowhere/backend\n"
                f'ExecStart="/nowhere/python" -m app.backup_runner {bid}\n'
                f'Environment="CP_DATA_DIR={data}"\n'
            )
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path


class TestUnitDataDirReconcile(_UnitDirCase):
    """unit 声明的数据目录与面板当前用的不是同一处 → 必须重渲染。"""

    def test_mismatched_data_dir_is_detected(self):
        path = self._write_unit(1, data_dir="/root/choyeon-panel/data")
        self.assertTrue(B._unit_needs_data_dir_fix(path),
                        "指向旧前缀的 unit 必须被判为需要修正")

    def test_matching_data_dir_is_not_flagged(self):
        path = self._write_unit(1, data_dir=config.DATA_DIR)
        self.assertFalse(B._unit_needs_data_dir_fix(path))

    def test_missing_unit_is_not_flagged(self):
        # 文件不存在时该走渲染路径重建，不在这里猜
        self.assertFalse(B._unit_needs_data_dir_fix(f"{self.dir}/panel-backup-99.service"))

    def test_unit_without_data_dir_line_is_not_flagged(self):
        path = self._write_unit(1, body="[Unit]\nDescription=x\n[Service]\nType=oneshot\n")
        self.assertFalse(B._unit_needs_data_dir_fix(path))

    def test_declared_dir_is_read_from_both_quoting_styles(self):
        # 面板生成的是 Environment="CP_DATA_DIR=…"，模板里还有不带引号的写法，
        # 两种都要认，否则手改过的 unit 永远查不出来
        quoted = self._write_unit(1, body='[Service]\nEnvironment="CP_DATA_DIR=/old/data"\n')
        bare = self._write_unit(2, body="[Service]\nEnvironment=CP_DATA_DIR=/old/data\n")
        self.assertEqual(B._unit_data_dir_of(quoted), "/old/data")
        self.assertEqual(B._unit_data_dir_of(bare), "/old/data")


class TestOrphanUnitCleanup(_UnitDirCase):
    """盘上有、库里没有的 unit 是残留：必须清掉，不能让它每晚失败一次。"""

    def test_orphan_service_and_timer_are_removed(self):
        svc = self._write_unit(77)
        tmr = f"{self.dir}/panel-backup-77.timer"
        with open(tmr, "w", encoding="utf-8") as f:
            f.write("[Timer]\n")
        removed = asyncio.run(B._remove_orphan_units(self.dir, {1, 2}))
        self.assertEqual(removed, [77])
        self.assertFalse(os.path.exists(svc), "孤儿 service 没删掉")
        self.assertFalse(os.path.exists(tmr), "孤儿 timer 没删掉")

    def test_orphan_cleanup_clears_failed_state(self):
        """删了文件还不够：oneshot 的 failed 不会自己消失，红字得一起清。"""
        self._write_unit(77)
        asyncio.run(B._remove_orphan_units(self.dir, set()))
        self.assertIn("panel-backup-77.service", self.reset_calls)
        self.assertIn("panel-backup-77.timer", self.reset_calls)

    def test_orphan_cleanup_disables_its_timer(self):
        self._write_unit(77)
        asyncio.run(B._remove_orphan_units(self.dir, set()))
        self.assertIn(
            ["systemctl", "disable", "--now", "panel-backup-77.timer"],
            self.calls,
            "孤儿 timer 必须先停掉，否则它还会到点触发",
        )

    def test_units_of_known_tasks_are_kept(self):
        self._write_unit(1)
        self._write_unit(2)
        removed = asyncio.run(B._remove_orphan_units(self.dir, {1, 2}))
        self.assertEqual(removed, [])
        self.assertTrue(os.path.exists(f"{self.dir}/panel-backup-1.service"))
        self.assertTrue(os.path.exists(f"{self.dir}/panel-backup-2.service"))

    def test_non_backup_units_are_untouched(self):
        other = f"{self.dir}/nginx.service"
        with open(other, "w", encoding="utf-8") as f:
            f.write("[Service]\n")
        asyncio.run(B._remove_orphan_units(self.dir, set()))
        self.assertTrue(os.path.exists(other), "别的 unit 不该被这个清理逻辑删掉")

    def test_non_numeric_ids_are_ignored(self):
        weird = f"{self.dir}/panel-backup-abc.service"
        with open(weird, "w", encoding="utf-8") as f:
            f.write("[Service]\n")
        removed = asyncio.run(B._remove_orphan_units(self.dir, set()))
        self.assertEqual(removed, [], "非数字 id 不是本面板的备份 unit，不该动")


class TestSyncAllTimersReconcile(_UnitDirCase):
    """面板启动时的对账要双向：库里有→渲染，盘上有→清孤儿。"""

    def test_orphan_unit_is_removed_on_startup(self):
        """真实故障形态：库里两条任务 + 盘上多一个残留 unit。"""
        orphan = self._write_unit(88)
        with mock.patch.object(B, "dbm", mock.Mock(query=lambda sql: [])):
            asyncio.run(B.sync_all_timers())
        self.assertFalse(os.path.exists(orphan), "残留 unit 在启动对账时必须被清掉")

    def test_known_task_unit_is_rendered_with_current_data_dir(self):
        """unit 声明旧目录时，重渲染后必须指向面板当前用的目录。"""
        row = {"id": 1, "schedule": "daily", "hour": 3, "minute": 30,
               "enabled": 1, "kind": "pg", "target": "appdb", "keep": 7}
        svc = self._write_unit(1, data_dir="/root/choyeon-panel/data")
        with mock.patch.object(B, "dbm", mock.Mock(query=lambda sql: [row])):
            asyncio.run(B.sync_all_timers())
        text = open(svc, encoding="utf-8").read()
        # 写进 unit 的值经过 unit_env_value 转义（反斜杠、%、引号），断言要用同一个出口
        expected = B.unit_env_value(config.DATA_DIR)
        self.assertIn(f'Environment="CP_DATA_DIR={expected}"', text)
        self.assertNotIn("/root/choyeon-panel/data", text,
                         "旧前缀还留在 unit 里 —— 这就是每晚失败的那一份")


class TestFailedStateReset(unittest.TestCase):
    """oneshot 的 failed 不会自己消失：修好之后必须主动归零。"""

    def setUp(self):
        self.calls: list[list] = []
        self.reset_calls: list[str] = []
        self.dir = tempfile.mkdtemp(prefix="cp_bk_failed_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (
            mock.patch.object(config, "UNIT_DIR", self.dir),
            mock.patch.object(B, "run", _fake_run(self.calls)),
            mock.patch.object(B.systemd_ops, "reset_failed", self._fake_reset),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def _fake_reset(self, unit):
        self.reset_calls.append(unit)

    def _task(self, **kw):
        row = {"id": 5, "schedule": "daily", "hour": 3, "minute": 30,
               "enabled": 1, "kind": "pg", "target": "appdb", "keep": 7}
        row.update(kw)
        return row

    def test_sync_resets_failed_state_of_enabled_task(self):
        asyncio.run(B._sync_timer(self._task()))
        self.assertIn("panel-backup-5.service", self.reset_calls,
                      "unit 已与面板对齐，旧 failed 是历史，不该继续占着告警位")

    def test_manual_task_also_resets_failed_state(self):
        """改成手动的任务：unit 文件被删，但它之前的 failed 还挂着 —— 得一起清。"""
        asyncio.run(B._sync_timer(self._task(schedule="manual")))
        self.assertIn("panel-backup-5.service", self.reset_calls)

    def test_delete_backup_resets_failed_state(self):
        from app import database as dbm
        bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) "
            "VALUES('pg','appdb','daily',3,30,7,1)"
        )
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (bid,))
        asyncio.run(B.delete_backup(bid))
        self.assertIn(f"panel-backup-{bid}.service", self.reset_calls,
                      "任务删了，unit 没了，但 failed 残留会让它继续被报")


if __name__ == "__main__":
    unittest.main()
