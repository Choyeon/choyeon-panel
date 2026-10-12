"""批次⑦A：单体仓库里的子目录组件必须能部署、也能回滚。

线上 Rosetta 是一个 Git 仓库挂两个应用：backend 在仓库根，frontend 在 `frontend/`
子目录，而子目录没有自己的 `.git`。旧代码在三处按 `{path}/.git` 判"是不是仓库"：

- `_run_deployment`：判 False 就转去 `git clone`，clone 拒绝写入已存在且非空的目录，
  于是前端这个应用**永远**部署不了，界面上只剩一句 git fatal。
- `rollback_app`：入口守卫直接拒绝——"应用目录不是 Git 仓库，无法回滚"。
- `sync_app`：恰好自己内联了一段 rev-parse 兜底，所以同步一直是好的，反而掩盖了前两条。

现在三处共用 `resolve_repo_dir`：git 操作落到真正的工作树根，装依赖/构建/启动仍在应用
自己的目录（unit 的 WorkingDirectory 就是它）。测试继续用真仓库真 git，理由同 ⑤N——
这条路径的成败全在 git 的返回码与工作树状态上，mock 出来只能证明代码按我想的写。
"""

import itertools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch7a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch7a_roots_"))

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


class _MonorepoCase(unittest.IsolatedAsyncioTestCase):
    """seed 负责往 origin 推提交，work 是"已部署"的那份工作树，两者故意不同步。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="cp_mono7a_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.origin = str(Path(self.base, "origin.git"))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin],
                       env=_GIT_ENV, check=True, capture_output=True, text=True)

        self.seed = str(Path(self.base, "seed"))
        Path(self.seed).mkdir()
        _git(self.seed, "init", "-q", "-b", "main")
        _git(self.seed, "remote", "add", "origin", self.origin)
        self.v1 = self.publish("v1")
        _git(self.seed, "push", "-q", "-u", "origin", "main")

        self.work = str(Path(self.base, "work"))
        _git(self.base, "clone", "-q", "-b", "main", self.origin, self.work)
        self.frontend = str(Path(self.work, "frontend"))

        self.app_id = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,domain,install_cmd,start_cmd)"
            " VALUES(?,'node',?,'main',?,NULL,'printf built > BUILD_MARKER','node server.mjs')",
            (f"mono7a-{next(_seq)}", self.origin, self.frontend),
        )
        self.addCleanup(dbm.execute, "DELETE FROM deployments WHERE app_id=?", (self.app_id,))
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (self.app_id,))

    def publish(self, tag: str) -> str:
        Path(self.seed, "frontend").mkdir(parents=True, exist_ok=True)
        Path(self.seed, "VERSION").write_text(tag)
        Path(self.seed, "frontend", "PAGE").write_text(tag)
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-qm", tag)
        sha = _git(self.seed, "rev-parse", "--short", "HEAD")
        _git(self.seed, "push", "-q", "origin", "main")
        return sha

    def move_app(self, **kw):
        cols = ",".join(f"{k}=?" for k in kw)
        dbm.execute(f"UPDATE apps SET {cols} WHERE id=?", (*kw.values(), self.app_id))

    async def deploy(self) -> tuple[dict, int, mock.AsyncMock]:
        """跑真实的 _run_deployment，只把 systemd 那一层换掉。"""
        app = apps_service.get_app(self.app_id)
        dep_id = int(dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')",
                                 (self.app_id,)))
        rav = mock.AsyncMock()
        with mock.patch.object(apps_service, "write_env_file"), \
             mock.patch.object(apps_service, "ensure_unit", new=mock.AsyncMock()), \
             mock.patch.object(apps_service, "restart_and_verify", new=rav):
            await apps_service._run_deployment(app, dep_id)
        row = dbm.query_one("SELECT status,commit_sha,log FROM deployments WHERE id=?", (dep_id,))
        return row, dep_id, rav


class TestComponentDeploy(_MonorepoCase):
    async def test_subdir_component_deploy_resets_repo_and_builds_in_component_dir(self):
        """缺陷本体：子目录组件部署时旧代码去 clone 非空目录，直接失败。

        断言分三层，缺一层都可能是"看起来过了"：仓库根的文件被更新（git 操作确实落在
        工作树根）、组件目录里的文件同步更新、构建命令仍在组件目录执行（unit 的
        WorkingDirectory 指向的就是它）。
        """
        sha = self.publish("v2")
        row, _dep, rav = await self.deploy()

        self.assertEqual(row["status"], "success", f"部署失败：{row['log'][-600:]}")
        self.assertEqual(Path(self.work, "VERSION").read_text(), "v2", "仓库根跟着更新")
        self.assertEqual(Path(self.frontend, "PAGE").read_text(), "v2", "组件目录跟着更新")
        self.assertEqual(Path(self.frontend, "BUILD_MARKER").read_text(), "built",
                         "安装/构建必须跑在组件目录，不是仓库根")
        self.assertEqual(row["commit_sha"], sha, "部署记录里的 commit 要能对上远端最新")
        self.assertIn("仓库根", row["log"], "日志必须说明本次更新的是整个仓库")
        rav.assert_awaited_once()
        unit = f"panel-{apps_service.get_app(self.app_id)['name']}.service"
        self.assertEqual(rav.await_args.args[0], unit, "重启的是这个应用自己的 unit")

    async def test_missing_dir_still_clones(self):
        """目录还不存在时保持原语义：clone 到应用目录，而不是报"没有仓库"。"""
        fresh = str(Path(self.base, "fresh"))
        self.move_app(path=fresh)
        row, _dep, _rav = await self.deploy()
        self.assertEqual(row["status"], "success", f"部署失败：{row['log'][-600:]}")
        self.assertTrue(Path(fresh, ".git").is_dir())
        self.assertEqual(Path(fresh, "frontend", "PAGE").read_text(), "v1")

    async def test_existing_dir_without_repo_fails_loudly(self):
        """目录已存在、又不在任何仓库里：不能假装成功，也不能把已存在的目录覆盖掉。"""
        orphan = Path(self.base, "orphan")
        orphan.mkdir()
        Path(orphan, "KEEP").write_text("local")
        self.move_app(path=str(orphan))
        row, _dep, rav = await self.deploy()
        self.assertEqual(row["status"], "failed", "clone 进非空目录必须如实失败")
        self.assertEqual(Path(orphan, "KEEP").read_text(), "local", "失败不能把用户的目录动过")
        rav.assert_not_awaited()

    async def test_root_level_app_keeps_working(self):
        """仓库根本身的 app（backend）行为不变，别把修子目录变成换回归。"""
        self.publish("v3")
        self.move_app(path=self.work)
        row, _dep, _rav = await self.deploy()
        self.assertEqual(row["status"], "success", f"部署失败：{row['log'][-600:]}")
        self.assertEqual(Path(self.work, "VERSION").read_text(), "v3")
        self.assertEqual(Path(self.work, "BUILD_MARKER").read_text(), "built")
        self.assertNotIn("仓库根", row["log"], "path 就是仓库根时不必多打一行")

    async def test_failed_component_deploy_still_reports_divergence(self):
        """安装失败时，组件也要说清"盘上已是新代码、服务还在跑旧代码"。

        旧写法取 commit 前先问 `{path}/.git` 在不在，组件目录永远答"不在"，于是
        before/after 全为 None，这条提示对子目录组件从来没出现过——而"git 已经 reset 到
        新版本、构建失败"恰恰是组件最常踩的失败形态。工作区被面板改过却不吭声，比构建
        本身失败更难查。
        """
        v2 = self.publish("v2")
        self.move_app(install_cmd="exit 7")
        row, _dep, rav = await self.deploy()
        self.assertEqual(row["status"], "failed")
        rav.assert_not_awaited()
        self.assertIn("工作区已从", row["log"])
        self.assertIn(v2, row["log"], "要写明切到了哪个 commit")
        self.assertIn(self.v1, row["log"], "部署前的版本也要在")
        self.assertEqual(Path(self.frontend, "PAGE").read_text(), "v2",
                         "失败时不静默把工作区 reset 回去")

class TestComponentRollback(_MonorepoCase):
    async def test_component_can_rollback(self):
        """旧守卫在这里直接抛"应用目录不是 Git 仓库，无法回滚"。"""
        v2 = self.publish("v2")
        row, _dep, _rav = await self.deploy()          # 工作区到 v2，并落一条 success
        self.assertEqual(row["commit_sha"], v2)
        dbm.execute("INSERT INTO deployments(app_id,status,commit_sha) VALUES(?,'success',?)",
                    (self.app_id, self.v1))
        with mock.patch.object(apps_service, "restart_and_verify", new=mock.AsyncMock()):
            out = await apps_service.rollback_app(self.app_id)
        self.assertEqual(out["commit"], self.v1)
        self.assertEqual(Path(self.frontend, "PAGE").read_text(), "v1", "组件目录真的退回去了")
        self.assertEqual(Path(self.work, "VERSION").read_text(), "v1", "整个工作树一起退")

    async def test_rollback_without_any_repo_still_refuses(self):
        orphan = Path(self.base, "nbgit")
        orphan.mkdir()
        dbm.execute("INSERT INTO deployments(app_id,status,commit_sha) VALUES(?,'success','deadbe')",
                    (self.app_id,))
        self.move_app(path=str(orphan))
        with self.assertRaises(RuntimeError) as cm:
            await apps_service.rollback_app(self.app_id)
        self.assertIn("不在任何 Git 仓库", str(cm.exception))


class TestResolveRepoDirEdgeCases(_MonorepoCase):
    """resolve_repo_dir 现在是部署/回滚/同步三条路径共用的入口，它的返回值判错，
    三条路径一起判错，所以边界单独钉住。
    """

    async def test_success_with_empty_stdout_is_not_a_repo(self):
        """code=0 但 stdout 为空：旧写法 `splitlines()[-1]` 当场 IndexError，
        而调用方拿到的是"部署失败: list index out of range"——一句和 git 毫无关系的报错。
        （test_apps_service 里 mock run 就是这么返回的，全量跑直接暴露了它。）
        """
        empty = mock.AsyncMock(return_value={"code": 0, "out": "", "err": ""})
        with mock.patch.object(apps_service, "run", new=empty):
            self.assertIsNone(await apps_service.resolve_repo_dir(str(Path(self.base, "nope"))))

    async def test_last_line_of_noisy_output_is_the_root(self):
        """git 在 hook/配置提示里往 stderr 之外掺行时，工作树根是最后一行。"""
        noisy = mock.AsyncMock(return_value={"code": 0, "out": f"hint\n{self.work}", "err": ""})
        with mock.patch.object(apps_service, "run", new=noisy):
            self.assertEqual(await apps_service.resolve_repo_dir(self.frontend), self.work)

    async def test_git_failure_is_not_a_repo(self):
        failed = mock.AsyncMock(return_value={"code": 128, "out": "", "err": "not a git repository"})
        with mock.patch.object(apps_service, "run", new=failed):
            self.assertIsNone(await apps_service.resolve_repo_dir(self.frontend))


class TestComponentSync(_MonorepoCase):
    async def test_sync_still_updates_the_component(self):
        """sync 改成共用 resolve_repo_dir，行为必须和原来一致。"""
        self.publish("v2")
        with mock.patch.object(apps_service, "restart_and_verify", new=mock.AsyncMock()):
            out = await apps_service.sync_app(self.app_id)
        self.assertTrue(out["ok"])
        self.assertEqual(Path(self.frontend, "PAGE").read_text(), "v2")
        self.assertEqual(Path(self.work, "VERSION").read_text(), "v2")
