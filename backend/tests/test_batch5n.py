"""批次⑤N：回滚必须**往回**走，不能把用户推向最新发布版。

线上端到端跑应用生命周期（建应用 → 部署 → 改端口/环境变量 → 再部署 → 一键同步 → 回滚 →
启停 → 删除）时，唯一没跑通的一步是"连着点两次回滚"：

第一次正常（v3 → v2），第二次面板返回 `{"commit": v3}` 并把服务切回 v3。
原因是候选筛选只比"和当前不同"，没比"比当前更早"：回滚不落新的 deployments 行，
所以退回 v2 之后，记录列表仍然是 [v3, v2, v1]，而按 id 倒序取"第一个不同的"就是 v3——
最新的发布版。用户越想往回退，面板越把他往前推，而且每一步都报"回滚成功"。

顺带在这条路径上补了第二个洞（同一处代码，同一批实测）：部署记录里的 commit 可能已经
不在仓库里了。`clone --depth 1` 的浅克隆本就没有老提交，历史被 force-push 改写并 gc 之后
也会没有；旧代码照样把它当目标，`git reset --hard` 只能回 "bad object"，于是这个应用
**永远**退不回仍然存在的更早版本，报错文案还是一句看不懂的 fatal。

判定用的 git 语义是实测的（git 2.47）：`merge-base --is-ancestor A B` 返回 0=A 是 B 的祖先、
1=不是、128=对象不存在；`cat-file -e <sha>^{commit}` 用返回码表达"这个 commit 还在不在"。
git 判不出先后时（128 之外的意外）沿用旧口径挑第一条，宁可多退一步，也不让回滚整个失效。
"""

import itertools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch5n_"))

import unittest  # noqa: E402

from app import database as dbm  # noqa: E402
from app.services import apps_service  # noqa: E402

_seq = itertools.count()
_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: str, *argv: str) -> str:
    p = subprocess.run(["git", *argv], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)
    assert p.returncode == 0, f"{argv} -> {p.returncode}\n{p.stdout}{p.stderr}"
    return p.stdout.strip()


class _RollbackCase(unittest.IsolatedAsyncioTestCase):
    """真仓库、真 git：这条路径的正确性全在 git 的返回码上，mock 出来的判断说明不了任何事。"""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="cp_rb5n_")
        self.addCleanup(shutil.rmtree, self.repo, True)
        _git(self.repo, "init", "-q", "-b", "main")
        name = f"rb5n-{os.getpid()}-{next(_seq)}"[:39]
        self.app_id = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,start_cmd) VALUES(?,'python',?,'main',?,'python3 x.py')",
            (name, "https://example.com/x.git", self.repo),
        )
        self.addCleanup(dbm.execute, "DELETE FROM deployments WHERE app_id=?", (self.app_id,))
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (self.app_id,))

    def commit(self, msg: str) -> str:
        Path(self.repo, "VERSION").write_text(msg)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", msg)
        return _git(self.repo, "rev-parse", "--short", "HEAD")

    def record(self, *shas: str):
        for s in shas:
            dbm.execute("INSERT INTO deployments(app_id,status,commit_sha) VALUES(?,'success',?)",
                        (self.app_id, s))

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "--short", "HEAD")

    async def rollback(self) -> dict:
        with mock.patch.object(apps_service, "service_action", new=mock.AsyncMock()), \
             mock.patch.object(apps_service, "is_active", new=mock.AsyncMock(return_value=True)), \
             mock.patch.object(apps_service, "_live_pid", new=mock.AsyncMock(return_value=4321)), \
             mock.patch.object(apps_service, "UNIT_SAMPLE_SEC", 0.01):
            return await apps_service.rollback_app(self.app_id)

    async def workspace_text(self) -> str:
        return Path(self.repo, "VERSION").read_text()


class TestRollbackDirection(_RollbackCase):
    async def test_consecutive_rollbacks_keep_going_back(self):
        """⑤N 实测缺陷：第二次回滚必须继续退回 v1，而不是跳回最新的 v3。"""
        v1, v2, v3 = (self.commit(m) for m in ("v1", "v2", "v3"))
        self.record(v1, v2, v3)
        self.assertEqual(self.head(), v3)

        r1 = await self.rollback()
        self.assertEqual(r1["commit"], v2, "第一次退回上一个成功版本")
        self.assertEqual(self.head(), v2)
        self.assertEqual(await self.workspace_text(), "v2", "工作区必须真的换掉，不能只改返回值")

        r2 = await self.rollback()
        self.assertEqual(r2["commit"], v1, f"连着点第二次要更早的版本，收到 {r2['commit']}（最新是 {v3}）")
        self.assertEqual(self.head(), v1)
        self.assertEqual(await self.workspace_text(), "v1")

    async def test_third_rollback_refuses_to_go_forward(self):
        """已经退到最旧的版本后再点回滚：必须如实报错，不能"成功"地把服务推到最新发布版。"""
        v1, v2, v3 = (self.commit(m) for m in ("v1", "v2", "v3"))
        self.record(v1, v2, v3)
        await self.rollback()
        await self.rollback()
        self.assertEqual(self.head(), v1)
        with self.assertRaises(RuntimeError) as ctx:
            await self.rollback()
        self.assertIn("没有可回滚的版本", str(ctx.exception))
        self.assertEqual(self.head(), v1, "报错不能顺手把工作区改到别处")

    async def test_missing_commit_is_skipped_for_one_that_exists(self):
        """记录里最新那条的 commit 已不在仓库（浅克隆/历史被改写）：跳过它，退到仍然存在的更早版本。"""
        v1 = self.commit("v1")
        other = tempfile.mkdtemp(prefix="cp_rb5n_other_")
        self.addCleanup(shutil.rmtree, other, True)
        _git(other, "init", "-q", "-b", "main")
        Path(other, "X").write_text("x")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "ghost")
        ghost = _git(other, "rev-parse", "--short", "HEAD")
        self.assertNotEqual(ghost, v1)

        v2 = self.commit("v2")
        self.record(v1, ghost)
        self.assertEqual(self.head(), v2)
        r = await self.rollback()
        self.assertEqual(r["commit"], v1, f"仓库里没有的 {ghost} 不能当目标")
        self.assertEqual(self.head(), v1)

    async def test_all_candidates_ahead_of_current_reports_no_rollback(self):
        """工作区被手工切到更老的版本后再点回滚：记录里全是更新的版本，只能报错。"""
        v1, v2, v3 = (self.commit(m) for m in ("v1", "v2", "v3"))
        self.record(v1, v2, v3)
        _git(self.repo, "reset", "--hard", v1)
        with self.assertRaises(RuntimeError) as ctx:
            await self.rollback()
        self.assertIn("没有可回滚的版本", str(ctx.exception))
        self.assertEqual(self.head(), v1)

    async def test_unknown_ancestry_degrades_instead_of_failing(self):
        """git 判不出先后（这里用认不出的当前 commit 造成 128）时沿用旧口径，不能把回滚堵死。"""
        v1, v2 = (self.commit(m) for m in ("v1", "v2"))
        self.record(v1, v2)
        with mock.patch.object(apps_service, "_current_sha",
                               new=mock.AsyncMock(return_value="ffff000")):
            r = await self.rollback()
        self.assertTrue(r["ok"])
        self.assertIn(r["commit"], (v1, v2))


class TestGitHelpers(_RollbackCase):
    async def test_commit_present(self):
        v1 = self.commit("v1")
        self.assertTrue(await apps_service._commit_present(self.repo, v1))
        self.assertFalse(await apps_service._commit_present(self.repo, "dead000"))

    async def test_is_newer_than_matches_git_return_codes(self):
        v1 = self.commit("v1")
        v3 = self.commit("v3")
        self.assertIs(await apps_service._is_newer_than(self.repo, v1, v3), True, "v3 是 v1 的后代")
        self.assertIs(await apps_service._is_newer_than(self.repo, v3, v1), False, "v1 不比 v3 新")
        self.assertIs(await apps_service._is_newer_than(self.repo, v3, v3), True, "自己算自己的后代（先被 sha==current 挡掉）")
        self.assertIs(await apps_service._is_newer_than(self.repo, "ffff000", v3), None, "对象认不出时必须回 None，不能当成'更早'")
        self.assertIs(await apps_service._is_newer_than(self.repo, None, v3), None)


if __name__ == "__main__":
    unittest.main()
