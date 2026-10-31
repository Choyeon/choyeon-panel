"""批次96：`choyeonctl backup list` 把「最近 50 份」当成「本机全部备份产物」。

这条平铺跨任务的 glob 一直 `files[:50]` 截断，但人读文本只贴前 50 行、`--json`
载荷更是一份裸数组——运维拿到 50 条就以为盘上只有 50 份，keep 攒到 100 的任务
第 51 份起在 CLI 里静默消失。批次87 修的是网页按任务算的份数（那个读者已给
`files_total`/`files_omitted`），这条 CLI 全局列表是另一个读者，同样不能拿截断值
冒充总量：现在截断前算出 `total`，人读与 JSON 都自报「共 N 份，另 M 份未列出」。

附带一个真实崩溃：旧代码对每个文件 `f.stat()` 三次（排序键、size、mtime 各一次），
glob 与建行之间那份文件被备份轮替/清理删掉时，`f.stat()` 抛未处理的
FileNotFoundError，整条 `backup list` 直接崩。改成一次 stat 并跳过消失的文件。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
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

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch96_data_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch96_bak_"))

import cli  # noqa: E402
from app import config  # noqa: E402

CAP = 50  # 与 cli 里 max_shown 同值；改上限时这条测试会指出漂移


def _run(backup_dir: str) -> tuple[dict, str]:
    """跑一次 `backup list`，返回 (payload, 人读文本)。"""
    buf = io.StringIO()
    with mock.patch.object(config, "BACKUP_DIR", backup_dir), contextlib.redirect_stdout(buf):
        payload = asyncio.run(cli.cmd_backup(argparse.Namespace(backup_cmd="list")))
    return payload, buf.getvalue()


def _touch(d: str, i: int, *, age_s: int = 0) -> str:
    """按 mtime 可区分地写一份 .dump.gz（后缀在 ARTIFACT_SUFFIXES 里），返回文件名。"""
    name = f"bk1-2026{i:04d}-prod.dump.gz"
    p = Path(d, name)
    p.write_bytes(b"x" * (i + 1))
    m = 1_800_000_000 + i * 60 - age_s  # 名字里 i 越大越新
    os.utime(p, (m, m))
    return name


class TruncationTests(unittest.TestCase):
    def test_over_cap_declares_true_total(self):
        d = tempfile.mkdtemp(prefix="cp_b96_")
        self.addCleanup(shutil.rmtree, d, True)
        for i in range(CAP + 10):
            _touch(d, i)
        payload, text = _run(d)
        self.assertEqual(payload["total"], CAP + 10, "真实份数在截断前算")
        self.assertEqual(len(payload["backups"]), CAP, "列表本身仍只端最近 50 份")
        self.assertEqual(payload["omitted"], 10, "没列出来的要能被读者说出来")
        self.assertIn(f"共 {CAP + 10} 份", text)
        self.assertIn("另 10 份未列出", text)
        self.assertIn(f"只列最近 {CAP} 份", text)

    def test_newest_first_survives_the_refactor(self):
        d = tempfile.mkdtemp(prefix="cp_b96_")
        self.addCleanup(shutil.rmtree, d, True)
        names = [_touch(d, i) for i in range(5)]
        payload, _ = _run(d)
        files = payload["backups"]
        self.assertEqual(files[0]["file"], str(Path(d, names[-1])), "最新的一份排最前")
        self.assertEqual([f["mtime"] for f in files], sorted((f["mtime"] for f in files), reverse=True),
                         "mtime 从新到旧")
        self.assertEqual([f["size"] for f in files], [5, 4, 3, 2, 1], "size 跟着名字里的序号，逐行对上")

    def test_under_cap_declares_nothing_omitted(self):
        d = tempfile.mkdtemp(prefix="cp_b96_")
        self.addCleanup(shutil.rmtree, d, True)
        for i in range(3):
            _touch(d, i)
        payload, text = _run(d)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["omitted"], 0)
        self.assertNotIn("未列出", text, "没截断就不要多念一句")

    def test_empty_dir_says_so(self):
        d = tempfile.mkdtemp(prefix="cp_b96_")
        self.addCleanup(shutil.rmtree, d, True)
        payload, text = _run(d)
        self.assertEqual(payload, {"backups": [], "total": 0, "omitted": 0})
        self.assertIn("(无备份)", text)


class SourceContractTests(unittest.TestCase):
    """锁住「stat 一次且带守卫、上限只有一个出处」，防止退回三份抄本或裸崩溃。"""

    def _block(self) -> str:
        src = (BACKEND / "cli.py").read_text(encoding="utf-8")
        start = src.index('a.backup_cmd == "list"')
        end = src.index('a.backup_cmd == "jobs"')
        return src[start:end]

    def test_cap_is_a_single_named_source(self):
        block = self._block()
        self.assertIn("max_shown", block, "上限应 named 一次")
        self.assertIn("stamped[:max_shown]", block, "切片读那个变量")
        self.assertIn("只列最近 {max_shown} 份", block, "措辞也读同一个变量，不另抄一个 50")
        self.assertNotIn("files[:50]", block, "旧的字面量切片已移除")

    def test_stat_happens_once_and_is_guarded(self):
        block = self._block()
        self.assertEqual(block.count("f.stat()"), 1, "每份文件只 stat 一次，不是排序键/size/mtime 各一次")
        self.assertIn("except OSError:", block, "消失的文件跳过而不是让整条命令崩")
        self.assertNotIn("key=lambda f: f.stat()", block, "不再用未守卫的 stat 当排序键")


if __name__ == "__main__":
    unittest.main()
