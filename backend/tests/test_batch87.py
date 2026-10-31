"""批次87：备份任务列表把「前 30 个产物」当成整条任务的总量。

`list_backups` 一直只 stat 最近 30 个文件（这是给页面体积设的上限），但网页那一列
把 `files.length` 写成「N 份」、把 `files` 的 size 相加写成「合计」，CLI
`backup jobs` 也把 `len(files)` 写进 `artifacts`。keep 设到 30 以上的任务
（允许 1-365）攒到第 31 份起，这三个数字就全是假的：份数停在 30、合计少一大截，
而且没有任何一个字说"还有没列出的"。

批次83-86 修的是同一种病：截断可以，冒充总量不行。这里把份数与合计改成按全部
产物算，列表仍只端最近 30 个，并同时给出 `files_omitted` 让读者自己说出来。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import itertools
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch87_data_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch87_bak_"))

from app import database as dbm  # noqa: E402
from app.services import backup_service as B  # noqa: E402
from cli import cmd_backup  # noqa: E402

_seq = itertools.count()


def _mk_job(kind: str, target: str, keep: int = 100) -> int:
    return dbm.execute(
        "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) "
        "VALUES(?,?,'manual',0,0,?,1)",
        (kind, target, keep),
    )


def _artifact(pid: int, i: int) -> str:
    """名字按时间递增排：字典序与"新旧"一致，倒数第一个是最近的。"""
    return f"bk{pid}-202610{i:02d}T000000.dump.gz"


class _Case(unittest.TestCase):
    kind = "pg"
    target = "somedb"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix=f"cp_b87_{next(_seq)}_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.patcher = mock.patch.object(B, "BACKUP_DIR", self.dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.bid = _mk_job(self.kind, self.target)
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (self.bid,))

    def write(self, name: str, size: int) -> None:
        Path(self.dir, name).write_bytes(b"x" * size)

    def row(self) -> dict:
        rows = [b for b in B.list_backups() if b["id"] == self.bid]
        self.assertEqual(len(rows), 1)
        return rows[0]


class TotalsTests(_Case):
    def test_more_than_thirty_artifacts_still_report_the_true_total(self):
        sizes = {}
        for i in range(1, 36):
            name = _artifact(self.bid, i)
            self.write(name, i)
            sizes[name] = i
        b = self.row()
        self.assertEqual(b["files_total"], 35, "份数必须按全部产物算，不是列表长度")
        self.assertEqual(b["files_omitted"], 5, "没列出来的 5 个要能被读者说出来")
        self.assertEqual(len(b["files"]), 30, "列表本身仍只端最近 30 个")
        self.assertEqual(b["files"][0]["name"], _artifact(self.bid, 35), "列表按新到旧")
        self.assertEqual(b["files_bytes"], sum(sizes.values()), "合计不许只加列出来的那些")

    def test_small_task_declares_nothing_omitted(self):
        self.write(_artifact(self.bid, 1), 10)
        self.write(_artifact(self.bid, 2), 20)
        b = self.row()
        self.assertEqual(b["files_total"], 2)
        self.assertEqual(b["files_omitted"], 0)
        self.assertEqual(b["files_bytes"], 30)
        self.assertEqual([f["name"] for f in b["files"]], [_artifact(self.bid, 2), _artifact(self.bid, 1)])

    def test_other_tasks_artifacts_are_not_counted(self):
        other = _mk_job("pg", "otherdb")
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (other,))
        for i in range(1, 4):
            self.write(_artifact(self.bid, i), 1)
            self.write(_artifact(other, i), 1)
        self.assertEqual(self.row()["files_total"], 3)


class CliTests(_Case):
    def test_jobs_artifacts_use_the_payload_total(self):
        self.write(_artifact(self.bid, 1), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            res = asyncio.run(cmd_backup(argparse.Namespace(backup_cmd="jobs")))
        job = [j for j in res["jobs"] if j["id"] == self.bid][0]
        self.assertEqual(job["artifacts"], 1)
        self.assertEqual(job["artifacts_omitted"], 0)

    def test_jobs_do_not_shrink_to_the_visible_window(self):
        capped = {"id": 7, "kind": "pg", "target": "somedb", "schedule": "manual", "enabled": 1,
                  "last_status": "ok", "last_run": "2026-10-31 03:00", "last_note": "",
                  "target_missing": False, "cloud_target_name": None,
                  "files": [{"name": f"bk7-{i:02d}.dump.gz"} for i in range(30)],
                  "files_total": 35, "files_bytes": 630, "files_omitted": 5}
        with (mock.patch.object(B, "list_backups", return_value=[capped]),
              contextlib.redirect_stdout(io.StringIO())):
            res = asyncio.run(cmd_backup(argparse.Namespace(backup_cmd="jobs")))
        self.assertEqual(res["jobs"][0]["artifacts"], 35, "JSON 里的份数是整条任务的")
        self.assertEqual(res["jobs"][0]["artifacts_omitted"], 5)


class FrontendContractTests(unittest.TestCase):
    def test_backups_table_reads_totals_and_owns_the_omission(self):
        src = (BACKEND.parent / "web" / "src" / "views" / "Backups.vue").read_text(encoding="utf-8")
        self.assertIn("r.files_total", src)
        self.assertIn("r.files_bytes", src)
        self.assertIn("r.files_omitted", src)
        self.assertNotIn("`${r.files.length} 份`", src, "份数按钮不能再读截断后的列表长度")
        self.assertNotIn("r.files.reduce", src, "合计不能再只加列出来的那些")


if __name__ == "__main__":
    unittest.main()
