"""回归：common.sh 的 PREFIX 必须能从脚本自身所在仓库推出来。

装在非默认目录（如 /root/www/choyeon-panel）的实例，此前跑 update.sh 会在
第一行 git 仓库校验上就 [fail] 退出：代码拉了但没重建没重启，面板仍在跑旧构建，
而 choyeonctl upgrade 报同一个错。
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent  # choyeon-panel/backend
REPO = BACKEND.parent  # choyeon-panel/
SCRIPTS = REPO / "scripts"


class TestPrefixDerivation(unittest.TestCase):
    def _prefix(self, cwd, extra_env=None):
        env = dict(os.environ)
        env.pop("CP_PREFIX", None)
        env.update(extra_env or {})
        p = subprocess.run(
            ["bash", "-lc", f'source "{SCRIPTS}/common.sh"; printf "%s" "$PREFIX"'],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(cwd),
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def test_derives_repo_root(self):
        self.assertEqual(self._prefix(REPO), str(REPO))

    def test_other_cwd_still_derives_same_repo(self):
        # 脚本是从别的目录被调用的（systemd hook、cron 等），仍应认出自己所在的仓库
        self.assertEqual(self._prefix(SCRIPTS.parent / "backend"), str(REPO))

    def test_explicit_env_still_wins(self):
        self.assertEqual(self._prefix(REPO, {"CP_PREFIX": "/tmp/somewhere"}), "/tmp/somewhere")

    def test_fallback_when_not_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            # 把 common.sh 拷到一个既没有 .git 也没有 backend/app/main.py 的目录，
            # 推导必须失败并回落到历史默认值，而不是拿到一个含糊的空路径
            subprocess.run(["cp", str(SCRIPTS / "common.sh"), d], check=True)
            env = dict(os.environ)
            env.pop("CP_PREFIX", None)
            p = subprocess.run(
                ["bash", "-lc", f'source "{d}/common.sh"; printf "%s" "$PREFIX"'],
                capture_output=True, text=True, timeout=60, env=env, cwd=d,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), "/root/choyeon-panel")


class TestGitFileIsStillARepo(unittest.TestCase):
    """`.git` 是**文件**的安装（linked worktree / submodule）也算仓库。

    旧写法判 `-d "$src/.git"`：这种安装结构完整、CLI 侧（`_default_prefix` 用
    `.exists()`）认得它，shell 侧却认不出，于是同一个事实两个读者给相反答案——
    PREFIX 悄悄回落到 /root/choyeon-panel，update.sh 第一行就 die，
    而 `choyeonctl status` 仍报告服务健康：拉了代码、没重建没重启，面板跑的还是旧构建。
    """

    def _prefix_of(self, root: Path) -> str:
        env = dict(os.environ)
        env.pop("CP_PREFIX", None)
        p = subprocess.run(
            ["bash", "-lc", f'source "{root}/scripts/common.sh"; printf "%s" "$PREFIX"'],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(root),
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def _fake_repo(self, tmp: str, git_kind: str) -> Path:
        root = Path(tmp)
        (root / "scripts").mkdir(parents=True)
        (root / "backend" / "app").mkdir(parents=True)
        (root / "backend" / "app" / "main.py").write_text("", encoding="utf-8")
        shutil.copy(str(SCRIPTS / "common.sh"), str(root / "scripts" / "common.sh"))
        if git_kind == "file":
            (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
        elif git_kind == "dir":
            (root / ".git").mkdir()
        return root

    def test_git_file_derives_its_own_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._fake_repo(d, "file")
            self.assertEqual(self._prefix_of(root), str(root.resolve()))

    def test_real_worktree_derives_its_own_directory(self):
        # 用真 git 造一个 linked worktree：确认它的 `.git` 确实是文件，且推导跟着这个文件走
        src = str(self._fake_repo(tempfile.mkdtemp(prefix="cp_prefix_src_"), "dir"))
        self.addCleanup(shutil.rmtree, src, True)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "seed"]):
            r = _git(src, *args)
            self.assertEqual(r.returncode, 0, r.stderr)
        link = tempfile.mkdtemp(prefix="cp_prefix_wt_")
        self.addCleanup(shutil.rmtree, link, True)
        wt = Path(link, "wt")
        r = _git(src, "worktree", "add", "-q", "--detach", str(wt))
        if r.returncode != 0:
            self.skipTest(f"git worktree 不可用：{r.stderr.strip()[:160]}")
        self.assertTrue(wt.joinpath(".git").is_file(), "linked worktree 的 .git 应当是文件")
        self.assertEqual(self._prefix_of(wt), str(wt.resolve()))

    def test_scripts_use_the_same_existence_test_as_the_cli(self):
        for name in ("common.sh", "update.sh"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            for probe in ('-d "$src/.git"', '-d "$PREFIX/.git"'):
                self.assertNotIn(probe, text, f"{name} 还在用目录判定认仓库：{probe}")
            for line in text.splitlines():
                if ".git\"" in line and "[ -" in line:
                    self.assertIn("[ -e ", line, f"`.git` 的判定必须认文件：{line}")
        cli = (REPO / "backend" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('(ROOT / ".git").exists()', cli, "CLI 侧同一条判定的口径也得跟着核")


def _git(cwd: str, *args: str):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
