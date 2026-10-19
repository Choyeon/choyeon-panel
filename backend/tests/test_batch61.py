"""批次 61A：五个「接口自己知道自己说了假话」的后端缺陷。

这一批的动机不是新功能，而是上一轮逐条点验时确认的五个诚实性问题——它们的共同点是
**返回值在正常情况下恰好说不出自己不可信**，所以只有出事的那一刻用户才被误导：

- **日志尾读把过滤排在截尾之后**：先取最后 N 行再按关键词筛，"最近 200 行里只有 2 行命中"
  会被说成"这个关键词总共命中 2 次"——第 201 行往前的命中永远看不见，而这正是排障时
  最想知道的那一条。现在过滤排在截尾前，`matched/dropped/window_truncated` 三个数分开
  说清"给了几行、窗口里命中几行、被行数上限裁掉几行、字节窗口读到底没有"。
- **异地剪枝照着截断的清单删**：`list_keys` 翻不完（LIST_PAGES×1000）时清单是不完整的，
  而 `while len(mine) > keep` 删的是"看得见的最旧"——前缀下堆了几万个对象时，一次剪枝
  会把翻到的近两万份一路删到只剩 keep 份。误删不可逆，现在清单没翻完就一份都不删，
  并把「为什么没剪」沿 push_pending → runner/路由/CLI 一路带出去（少剪只是多占桶）。
- **巡检没有重入闸**：后台每 30 分钟一轮 + 网页两处手动按钮，一轮里十几个 await，
  连点就是两轮交错；`sent:` 去重是先读后写，两拨都读到"今天没发过"就发两遍通知，
  `streak:<code>` 被虚增、去抖提前到位。第二轮跳过而不是排队——第一轮刚把每项读过。
- **恢复失败路径不遮凭据**：成功分支 `redact_creds(log)` 遮了，失败分支把原始 log
  原样拼进 RuntimeError，而这条消息会被路由原样写进审计、原样回给浏览器——泄露恰好
  只在出事的时候发生，那时 log 里最容易躺着带连接串的命令行。
- **目录列表把「读不到」印成「空文件」**：stat 失败的条目 size 回 0，前端渲染成 "0 B"，
  与权限列同一个病（0000 那个已经修了）：现在回 None，前端印「—」。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch61_data_"))

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

from app import backup_runner as R  # noqa: E402
from app import config, main, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.services import alerts_service as ALS  # noqa: E402
from app.services import backup_service as B  # noqa: E402
from app.services import cloud_backup as C  # noqa: E402
from app.services import files_service  # noqa: E402
from app.services import web_logs as WL  # noqa: E402

_seq = iter(range(1000, 999999))


def _uid(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _write(path: str, lines: list[str]) -> None:
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestTailFilterBeforeCut(unittest.TestCase):
    """过滤排在截尾之前，且四个数各说各的账。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp61_tail_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.p = f"{self.dir}/access.log"

    def test_match_beyond_the_line_cap_is_visible(self):
        """旧写法：命中行在第 1、2 行，lines=2 只看最后两行 → 报"没有命中"。"""
        _write(self.p, ["needleA hit", "needleB hit"] + [f"filler {i}" for i in range(98)])
        out = WL.tail_file(self.p, 2, "needle", 1500)
        self.assertEqual(out["matched"], 2)
        self.assertEqual([x for x in out["items"] if "needleA" in x], [out["items"][0]])
        self.assertIn("needleB", out["items"][1])
        self.assertEqual(out["dropped"], 0)
        self.assertFalse(out["window_truncated"])

    def test_dropped_names_the_lines_the_cap_cut(self):
        _write(self.p, [f"filler {i}" for i in range(100)])
        out = WL.tail_file(self.p, 5, "filler", 1500)
        self.assertEqual((out["matched"], out["dropped"], len(out["items"])), (100, 95, 5))

    def test_window_truncated_is_the_second_knife(self):
        _write(self.p, [f"row {i} " + "x" * 40 for i in range(200)])
        with mock.patch.object(WL, "TAIL_BYTES", 100):
            out = WL.tail_file(self.p, 5, "", 1500)
        self.assertTrue(out["window_truncated"], "文件比单次读量大，连窗口都没读到底必须说出来")
        with mock.patch.object(WL, "TAIL_BYTES", 10 * 1024 * 1024):
            self.assertFalse(WL.tail_file(self.p, 5, "", 1500)["window_truncated"])

    def test_redaction_and_char_cap_unchanged(self):
        _write(self.p, ['x - - [16/Oct/2026:09:00:00 +0000] "GET https://bob:S3cr3tT0ken@h/ HTTP/1.1" 200 1'])
        self.assertNotIn("S3cr3tT0ken", WL.tail_file(self.p, 5, "", 1500)["items"][0])
        _write(self.p, ["y" * 500])
        self.assertTrue(all(len(x) <= 40 for x in WL.tail_file(self.p, 5, "", 40)["items"]))


class TestNginxLogRouteHonesty(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.admin = _uid("b61a")
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (cls.admin, security.hash_pass("Password123"), "admin"))
        cls.hdr = {"Authorization": f"Bearer {security.sign_token(cls.admin, 'admin')}"}

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username=?", (cls.admin,))

    def setUp(self):
        self.logs = os.path.realpath(tempfile.mkdtemp(prefix="cp61_api_logs_"))
        self.addCleanup(shutil.rmtree, self.logs, True)
        for p in (mock.patch.object(config, "LOG_ROOTS", [self.logs]),
                  mock.patch.object(config, "FILE_ROOTS", [])):
            p.start()
            self.addCleanup(p.stop)
        _write(f"{self.logs}/access.log",
               ["hit early 1", "hit early 2"] + [f"filler {i}" for i in range(98)] + ["hit last"])

    def test_filter_hits_from_the_front_of_the_window_come_back(self):
        r = self.client.get("/api/system/nginx-log?lines=2&filter=hit", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["matched"], 3)
        self.assertEqual(len(d["items"]), 2)
        self.assertIn("hit last", d["items"][-1])

    def test_truncated_follows_dropped_not_page_size(self):
        """旧口径 len(items) >= lines 会把"恰好给满一页的完整答案"也说成截断，
        把"命中被行数上限裁掉"和"字节窗口没到底"分开以后，两把刀都要进 truncated。"""
        d = self.client.get("/api/system/nginx-log?lines=3&filter=hit", headers=self.hdr).json()
        self.assertEqual((d["matched"], d["dropped"], d["truncated"]), (3, 0, False))
        d = self.client.get("/api/system/nginx-log?lines=2&filter=hit", headers=self.hdr).json()
        self.assertTrue(d["truncated"])
        self.assertEqual(d["count"], 2)


class TestPruneRefusesTruncatedListing(unittest.TestCase):
    def _rows(self, n: int) -> list[dict]:
        return [{"key": f"bk7-2026-01-{i:02d}-00-00-00-app.tar.gz", "size": 1, "modified": ""}
                for i in range(1, n + 1)]

    def test_nothing_is_deleted_when_the_roster_is_incomplete(self):
        bk = {"id": 7, "keep": 2}
        with mock.patch.object(C, "get_secret", return_value={"prefix": "", "id": 3}), \
             mock.patch.object(C, "list_keys", lambda t, pr: (self._rows(9), True)), \
             mock.patch.object(C, "delete_object",
                               lambda t, k: self.fail("清单没翻完就删，删的是看得见的最旧")) as _d:
            res = C.prune_remote(3, bk)
        self.assertEqual(res["deleted"], [])
        self.assertTrue(res["skipped"])
        self.assertIn("不删", res["skipped"])

    def test_complete_listing_still_prunes(self):
        bk = {"id": 7, "keep": 7}
        with mock.patch.object(C, "get_secret", return_value={"prefix": "", "id": 3}), \
             mock.patch.object(C, "list_keys", lambda t, pr: (self._rows(3), False)), \
             mock.patch.object(C, "delete_object", lambda t, k: None):
            res = C.prune_remote(3, bk)
        self.assertTrue(res["ok"])
        self.assertNotIn("skipped", res)


class TestPruneSkipReachesEveryConsumer(unittest.TestCase):
    """「远端没剪枝」必须一路带到 runner 的 journal、路由的响应和 CLI 的输出，
    否则管理员看到的还是那句"已推送 N 份"。"""

    def _bk(self):
        return {"id": 7, "cloud_target_id": 3, "keep": 2}

    def test_push_pending_carries_the_reason(self):
        skipped = {"ok": False, "deleted": [], "remote_count": 9, "truncated": True,
                   "skipped": "远端清单没翻完"}
        with mock.patch.object(C.dbm, "query_one",
                               lambda sql, *a: {"id": 3, "name": "t", "enabled": 1}), \
             mock.patch.object(R, "auto_files", lambda bid: ["bk7-x.tar.gz"]), \
             mock.patch.object(C, "already_pushed", lambda tid, n: False), \
             mock.patch.object(C, "push_file", lambda tid, bid, n: {"ok": True, "key": n}), \
             mock.patch.object(C, "prune_remote", return_value=skipped):
            out = C.push_pending(self._bk())
        self.assertEqual(out["prune_skipped"], "远端清单没翻完")
        self.assertTrue(out["ok"], "推送本身是成功的：没剪枝是另一件事，要分开说")

    def test_push_pending_clean_prune_adds_nothing(self):
        clean = {"ok": True, "deleted": [], "remote_count": 1, "truncated": False}
        with mock.patch.object(C.dbm, "query_one",
                               lambda sql, *a: {"id": 3, "name": "t", "enabled": 1}), \
             mock.patch.object(R, "auto_files", lambda bid: ["bk7-x.tar.gz"]), \
             mock.patch.object(C, "already_pushed", lambda tid, n: False), \
             mock.patch.object(C, "push_file", lambda tid, bid, n: {"ok": True, "key": n}), \
             mock.patch.object(C, "prune_remote", return_value=clean):
            out = C.push_pending(self._bk())
        self.assertNotIn("prune_skipped", out)

    def test_runner_prints_and_audits(self):
        res = {"ok": True, "pushed": [{"key": "bk7-x.tar.gz"}], "pruned": [],
               "remote_count": 9, "prune_skipped": "远端清单没翻完"}
        audited = []
        err = io.StringIO()
        with mock.patch.object(C, "push_pending", return_value=res), \
             mock.patch.object(R.dbm, "audit",
                               lambda *a, **kw: audited.append(a)), contextlib.redirect_stderr(err):
            R._push_offsite(self._bk())
        self.assertIn("远端没有剪枝", err.getvalue())
        codes = [a[1] for a in audited]
        self.assertIn("backup:push-prune-skipped", codes, f"跳过剪枝没进审计：{audited}")


class TestChecksReentryGate(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, ALS, "_checks_running", False)

    def test_second_round_is_skipped_while_first_runs(self):
        calls: list[int] = []
        release = asyncio.Event()

        async def slow():
            calls.append(1)
            await release.wait()

        async def go():
            with mock.patch.object(ALS, "_run_checks", slow):
                a = asyncio.ensure_future(ALS.run_checks())
                await asyncio.sleep(0)
                b = await ALS.run_checks()
                release.set()
                return await a, b

        first, second = asyncio.run(go())
        self.assertEqual(len(calls), 1, "第二轮要么跳过，要么排队再回一次源——排队等于白跑一轮")
        self.assertNotIn("skipped", first)
        self.assertIn("skipped", second)

    def test_gate_opens_after_success_and_after_crash(self):
        ran = []

        async def ok():
            ran.append(1)

        with mock.patch.object(ALS, "_run_checks", ok):
            asyncio.run(ALS.run_checks())
            asyncio.run(ALS.run_checks())
        self.assertEqual(len(ran), 2)

        async def boom():
            ran.append(1)
            raise RuntimeError("certbot 挂了")

        with mock.patch.object(ALS, "_run_checks", boom), self.assertRaises(RuntimeError):
            asyncio.run(ALS.run_checks())
        self.assertFalse(ALS._checks_running, "异常后闸不复位，之后每一轮巡检都会被永久跳过")


class TestRestoreFailureRedaction(unittest.TestCase):
    def setUp(self):
        self.bkdir = tempfile.mkdtemp(prefix="cp61_restore_")
        self.addCleanup(shutil.rmtree, self.bkdir, True)
        for mod in (B,):
            p = mock.patch.object(mod, "BACKUP_DIR", self.bkdir)
            p.start()
            self.addCleanup(p.stop)

    def test_credentials_in_the_failure_log_never_leave_the_process(self):
        """成功分支遮了、失败分支不遮，泄露就只在出事的时候发生——
        而 pg_restore/git 那几步失败时最容易把带连接串的命令行留在 log 里。"""
        name = "bk7-2026-01-01-00-00-00.tar.gz"
        _write(os.path.join(self.bkdir, name), ["x"])
        bk = {"id": 7, "kind": "app", "target": "b61", "schedule": "manual", "enabled": 1, "keep": 7}

        async def fake(_bk, _src, log):
            log.append("!! 解包中断：目录可能处于半恢复状态，服务保持停止")
            log.append("!! 命令失败：pg_dump " + "postgres://bob:S3cr3tT0ken@db/internal")
            raise RuntimeError("tar 解压失败")

        with mock.patch.object(dbm, "query_one",
                               lambda sql, *a: bk if "FROM backups" in str(sql) else None), \
                mock.patch.object(B, "_restore_app", fake), \
                mock.patch.object(dbm, "audit", lambda *a, **kw: None), \
                self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.restore_backup(bk["id"], name))
        msg = str(cm.exception)
        self.assertNotIn("S3cr3tT0ken", msg, f"失败消息原样带出凭据：{msg}")
        self.assertIn("***", msg)
        self.assertIn("服务保持停止", msg, "遮凭据不能把现场说明一起遮掉")


class TestListDirSizeUnknown(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cp61_files_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.old_roots = files_service.ROOTS
        files_service.ROOTS = [self.root]
        self.addCleanup(setattr, files_service, "ROOTS", self.old_roots)

    def test_unstatable_entry_reports_none_not_zero(self):
        """`0` 是"确认这是空文件"的结论；正在被删除/权限拒绝的条目要回 None，
        前端才印得出「—」，而不是把"不知道"显示成"0 B"。"""
        with open(os.path.join(self.root, "normal.txt"), "w") as f:
            f.write("hello")
        with open(os.path.join(self.root, "vanishing.txt"), "w"):
            pass
        real_stat = os.stat

        def spy(path, *a, **kw):
            if os.path.basename(str(path)) == "vanishing.txt":
                raise PermissionError(13, "denied")
            return real_stat(path, *a, **kw)

        with mock.patch.object(files_service.os, "stat", spy):
            r = files_service.list_dir(self.root)
        by = {e["name"]: e for e in r["entries"]}
        self.assertIsNone(by["vanishing.txt"]["size"])
        # perm_fields 是摊平进条目的（"perm"/"octal" 直接在 entry 上）
        self.assertEqual(by["vanishing.txt"]["octal"], "")
        self.assertEqual(by["normal.txt"]["size"], 5)


if __name__ == "__main__":
    unittest.main()
