"""回归：common.sh 的 PREFIX 必须能从脚本自身所在仓库推出来。

装在非默认目录（如 /root/www/choyeon-panel）的实例，此前跑 update.sh 会在
第一行 git 仓库校验上就 [fail] 退出：代码拉了但没重建没重启，面板仍在跑旧构建，
而 choyeonctl upgrade 报同一个错。
"""

import os
import subprocess
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
REPO = SCRIPTS.parent


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
        import tempfile

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


if __name__ == "__main__":
    unittest.main()
