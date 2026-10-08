"""部署脚本守卫的回归测试（scripts/*.sh）。

这一层此前只能靠人工执行验证。测试全部在临时目录里跑，并且：
- 绝不真删系统路径：涉及 rm 的用例统一把 PATH 指向一个"只记录不删除"的 rm shim；
- 绝不依赖网络与真实服务：只调用 common.sh 的纯校验函数，以及 uninstall.sh
  在 require_root 之前就会退出的参数分支。
- 守卫逻辑（备份目录位置、tty 确认）放在 common.sh 里，用 source 后直接调函数的
  方式测：CI 的 runner 是普通用户，脚本入口的 require_root 会先拦住整条 E2E 路径，
  逻辑本身其实不需要权限。E2E 那两条再用 `id -u` shim 放行，并断言确实走过了
  require_root，避免"其实什么都没测"的假绿。
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO, "scripts")

# 只记录、不删除，用于安全地观察"脚本打算 rm 什么"
SHIM = tempfile.mkdtemp(prefix="cp_rm_shim_")
os.makedirs(SHIM, exist_ok=True)
with open(os.path.join(SHIM, "rm"), "w", encoding="utf8") as fh:
    fh.write("#!/bin/sh\necho \"SHIM-RM: $*\"\nexit 0\n")
os.chmod(os.path.join(SHIM, "rm"), 0o755)

# CI 的 runner 是普通用户，而 uninstall.sh 第一步就是 require_root。
# 这两条守卫（备份目录位置、tty）本身是纯 shell 判断，不需要任何权限，
# 所以给 `id -u` 一个假 root，让脚本能走到守卫那一行——rm 仍然是 shim，
# 不会真的删任何东西。用例里会断言"确实走过了 require_root"，
# 假 root 哪天失效（脚本改成读 $EUID 之类）会直接报错，不会静默跳过。
with open(os.path.join(SHIM, "id"), "w", encoding="utf8") as fh:
    fh.write(
        "#!/bin/sh\n"
        '[ "$1" = "-u" ] && { echo 0; exit 0; }\n'
        'exec /usr/bin/id "$@"\n'
    )
os.chmod(os.path.join(SHIM, "id"), 0o755)


def _source_common(env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    env["NO_COLOR"] = "1"
    return env


def _bash(snippet, env_extra=None, path_prefix=None):
    """在 source 过 common.sh 的 bash 里执行片段，返回 CompletedProcess。"""
    env = _source_common(env_extra)
    if path_prefix:
        env["PATH"] = path_prefix + os.pathsep + env["PATH"]
    full = f'source "{SCRIPTS}/common.sh" >/dev/null 2>&1\n{snippet}'
    # stdin 显式设为 DEVNULL：本机跑测试时终端是 tty，tty 相关分支
    # 就会在"人肉跑通过、CI 里失败"之间来回翻脸
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                          env=env, timeout=60, stdin=subprocess.DEVNULL)


class TestDeletableGuard(unittest.TestCase):
    """uninstall --purge 的删除目标必须过 _assert_deletable。"""

    def _check(self, path):
        return _bash(f'_assert_deletable CP_PREFIX "{path}" && echo ACCEPT')

    def test_legit_install_paths_are_allowed(self):
        """守卫不能把默认安装方式一起挡死——那是最容易测漏的一类错误。"""
        for p in ("/root/choyeon-panel", "/opt/choyeon-panel", "/data/choyeon-panel",
                  "/home/ubuntu/panel", "/srv/panel", "/mnt/nas/panel",
                  "/root/choyeon-panel/data"):
            r = self._check(p)
            self.assertIn("ACCEPT", r.stdout, f"合法安装路径被误拦：{p} -> {r.stderr.strip()}")

    def test_root_and_shallow_paths_rejected(self):
        for p in ("/", "/root", "/etc", "/var", "/usr", "/tmp", "/opt", "/home"):
            r = self._check(p)
            self.assertNotIn("ACCEPT", r.stdout, f"危险路径被放行：{p}")
            self.assertEqual(r.returncode, 1, p)

    def test_trailing_slash_cannot_dodge_depth_check(self):
        """/root// 规范化后仍是 /root；旧写法在这里会让层级判断失效。"""
        for p in ("/root/", "/root//", "/etc/"):
            r = self._check(p)
            self.assertNotIn("ACCEPT", r.stdout, f"末尾斜杠绕过了检查：{p}")

    def test_trailing_slash_cannot_dodge_protected_name_check(self):
        """这一条才是 _norm_path 真正扛活的地方：
        "/var/lib/mysql/" 与清单里的 "/var/lib/mysql" 字符串不相等，
        祖先/后代模式匹配也接不上（多了一个斜杠），不做规范化就会被放行。"""
        for p in ("/var/lib/mysql/", "/etc/nginx/", "/usr/local/", "/var/log/"):
            r = self._check(p)
            self.assertNotIn("ACCEPT", r.stdout, f"带尾斜杠的受保护目录被放行：{p}")
            self.assertEqual(r.returncode, 1, f"{p} 未触发 die（rc={r.returncode}）")
            self.assertTrue(r.stderr.strip(), f"{p} 被拒绝但没有任何说明")

    def test_root_has_its_own_clear_message(self):
        """/ 同时会被"层级太浅"拦住，所以只断言 rc 的话这条分支被删掉也测不出来。
        用户要的是"这是根目录"这句明确话，而不是"层级太浅，请把面板装在…"。"""
        r = self._check("/")
        self.assertIn("根目录", r.stderr, r.stderr)

    def test_depth_floor_blocks_unlisted_shallow_dirs(self):
        """层级下限不是"重复劳动"：/mnt /media /data 这类目录既不在 NEVER_INSIDE
        也不在 PROTECTED 清单里，一旦去掉下限判断就会被直接放行，
        而它们同样是"一次输入错误就能命中"的顶层目录。"""
        for p in ("/mnt", "/media", "/data", "/srv2"):
            r = self._check(p)
            self.assertNotIn("ACCEPT", r.stdout, f"未上榜的顶层目录被放行：{p}")
            self.assertEqual(r.returncode, 1, p)
        # 同一层级的合法判断：加一层就放行，说明拦的是"深度"而不是"名字"
        self.assertIn("ACCEPT", self._check("/mnt/nas/panel").stdout)

    def test_every_rejected_path_complains(self):
        """die 必须留下原因：只 exit 1 不写日志的话，用户只看到脚本"莫名其妙挂了"。"""
        for p in ("/", "/root", "/etc", "/etc/nginx", "/usr/local", "/var/lib/mysql",
                  "/root/choyeon-panel/../..", "/var/lib/mysql/"):
            r = self._check(p)
            self.assertNotIn("ACCEPT", r.stdout, p)
            self.assertTrue(r.stderr.strip(), f"{p} 被拒绝但没有错误信息")

    def test_system_dirs_rejected_by_name_or_descendant(self):
        for p in ("/etc/nginx", "/etc/nginx/conf.d", "/usr/local", "/usr/local/panel",
                  "/etc/systemd", "/var/lib/mysql", "/var/log"):
            r = self._check(p)
            self.assertNotIn("ACCEPT", r.stdout, f"系统目录被放行：{p}")

    def test_parent_of_system_dir_rejected(self):
        """/etc/systemd 会带走 unit 目录，即使它本身层级够。"""
        r = self._check("/etc/systemd")
        self.assertNotIn("ACCEPT", r.stdout)

    def test_parent_of_protected_data_dir_rejected(self):
        """CP_PROTECTED 的"包含"分支：/var/lib 层级为 2、又不在 NEVER_INSIDE 里，
        但它内含 /var/lib/mysql 与 /var/lib/postgresql——删它就是删数据库。
        这条用例之前缺位，导致该分支被改空也测不出来。"""
        for p in ("/var/lib", "/var/lib/"):
            r = self._check(p)
            self.assertNotIn("ACCEPT", r.stdout, f"包含数据库目录的父路径被放行：{p}")
            self.assertEqual(r.returncode, 1, p)
        # 反面确认：/var/lib 之下的独立安装目录仍要放行，别把守卫写成一刀切
        self.assertIn("ACCEPT", self._check("/var/lib/choyeon-panel").stdout)

    def test_dotdot_cannot_escape_the_guard(self):
        """字符白名单允许 `.` 和 `/`，所以 /root/x/../.. 一路通过校验，
        而 rm -rf 由内核解析成删 /。"""
        for p in ("/root/choyeon-panel/../..", "/root/x/../..", "/a/../etc"):
            r = self._check(p)
            self.assertNotIn("ACCEPT", r.stdout, f"含 .. 的路径被放行：{p}")
            self.assertIn("..", r.stderr, p)


class TestValidateConfig(unittest.TestCase):
    def test_dotdot_rejected_at_entry(self):
        r = subprocess.run(["bash", "-c", f'source "{SCRIPTS}/common.sh"'],
                           capture_output=True, text=True, timeout=60,
                           env=_source_common({"CP_PREFIX": "/root/choyeon-panel/../.."}))
        self.assertNotEqual(r.returncode, 0, "含 .. 的 CP_PREFIX 在 source 时就被放行了")
        self.assertIn("..", r.stderr)

    def test_normal_prefix_still_loads(self):
        for var in ("CP_PREFIX", "CP_DATA_DIR", "CP_BACKUP_DIR"):
            r = subprocess.run(["bash", "-c", f'source "{SCRIPTS}/common.sh"; echo LOADED'],
                               capture_output=True, text=True, timeout=60,
                               env=_source_common({var: "/tmp/cp_scripts_ut/x"}))
            self.assertIn("LOADED", r.stdout, f"{var} 的合法值被 validate_config 拒了：{r.stderr}")

    def test_shell_metachars_rejected(self):
        for bad in ("/root/it's-panel", "/root/a;reboot", "/root/a b", "/root/a\tb"):
            r = subprocess.run(["bash", "-c", f'source "{SCRIPTS}/common.sh"; echo LOADED'],
                               capture_output=True, text=True, timeout=60,
                               env=_source_common({"CP_PREFIX": bad}))
            self.assertNotIn("LOADED", r.stdout, f"不安全字符被放行：{bad!r}")


class TestUninstallArgParsing(unittest.TestCase):
    """未知参数必须报错——旧写法把 --purge=1 静默当成"不 purge"。"""

    def _run(self, args, env_extra=None, stdin="", path_prefix=None):
        env = _source_common(env_extra)
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + env["PATH"]
        # input=... 让 stdin 变成管道而非 tty：正好覆盖"无 tty"这条分支
        return subprocess.run(["bash", os.path.join(SCRIPTS, "uninstall.sh"), *args],
                              capture_output=True, text=True, env=env, input=stdin,
                              timeout=120)

    def test_unknown_option_is_rejected(self):
        for a in ("--purge=1", "-purge", "--purge2", "--force"):
            r = self._run([a])
            self.assertEqual(r.returncode, 1, f"{a} 未被拒绝，输出：{r.stdout}{r.stderr}")
            self.assertIn("未知参数", r.stderr + r.stdout)

    def test_help_exits_zero(self):
        r = self._run(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("--purge", r.stdout)

    def test_purge_without_tty_refuses(self):
        """无 tty 时不能只说"未确认"——那会让人以为脚本坏了，进而绕过确认。"""
        d = tempfile.mkdtemp(prefix="cp_un_")
        self.addCleanup(shutil.rmtree, d, True)
        r = self._run(["--purge"], env_extra={"CP_PREFIX": f"{d}/panel",
                                              "CP_DATA_DIR": f"{d}/data",
                                              "CP_BACKUP_DIR": f"{d}/bk"}, stdin="purge\n",
                      path_prefix=SHIM)
        combined = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, combined)
        self.assertIn("tty", combined)
        # 必须真的走过了 require_root：否则"看不到 tty 那句"可能只是因为
        # 脚本在第一行就以"需要 root"退出了，守卫本身根本没被执行到
        self.assertNotIn("需要 root", combined, f"仍然卡在 root 检查上，守卫没被执行：{combined}")
        self.assertTrue(os.path.isdir(d))

    def test_backup_dir_inside_prefix_is_refused(self):
        """否则"卸载前备份"刚写完就被同一条 rm -rf 删掉，脚本还打印备份已保留。"""
        for suffix in ("/bk", "", "/"):
            with self.subTest(backup_suffix=suffix):
                d = tempfile.mkdtemp(prefix="cp_un2_")
                self.addCleanup(shutil.rmtree, d, True)
                os.makedirs(f"{d}/panel/bin")
                args = {"CP_PREFIX": f"{d}/panel", "CP_DATA_DIR": f"{d}/data",
                        "CP_BACKUP_DIR": f"{d}/panel{suffix}"}
                r = self._run(["--purge"], env_extra=args, stdin="purge\n", path_prefix=SHIM)
                combined = r.stdout + r.stderr
                self.assertNotIn("需要 root", combined, f"仍然卡在 root 检查上：{combined}")
                self.assertIn("CP_BACKUP_DIR", combined,
                              f"备份目录在 PREFIX 内时没有拦截：{combined}")
                self.assertNotIn("SHIM-RM", combined, f"仍然执行了删除动作：{combined}")


class TestPurgeGuards(unittest.TestCase):
    """purge 的守卫逻辑本身：纯路径判断，不需要 root，也不需要跑完整脚本。

    上面两条 E2E 用例依赖假 root 的 `id` shim；这里直接 source common.sh 调函数，
    保证即使脚本结构变了（比如 require_root 挪位置、改成读 $EUID），守卫逻辑仍有覆盖。
    """

    def _bk(self, backup, *targets):
        return _bash(f'_assert_backup_outside "{backup}" {" ".join(targets)} && echo ACCEPT')

    def test_backup_inside_prefix_or_data_is_rejected(self):
        for suffix in ("/bk", "", "/"):
            with self.subTest(backup_suffix=suffix):
                r = self._bk(f"/opt/choyeon-panel{suffix}", "/opt/choyeon-panel", "/var/lib/panel")
                self.assertNotIn("ACCEPT", r.stdout, f"备份目录在待删目录内被放行：{r.stdout}")
                self.assertEqual(r.returncode, 1)
                self.assertIn("CP_BACKUP_DIR", r.stderr, r.stderr)

    def test_backup_outside_targets_is_allowed(self):
        """非空验证：守卫不能把合法配置一起挡死。"""
        r = self._bk("/srv/backup/panel", "/opt/choyeon-panel", "/var/lib/panel")
        self.assertIn("ACCEPT", r.stdout, r.stdout + r.stderr)

    def test_sibling_prefix_with_common_string_is_allowed(self):
        """`/opt/choyeon-panel-backup` 是兄弟目录，不是子目录——
        旧写法如果只用字符串前缀（不带斜杠）判断，这里会被误杀。"""
        r = self._bk("/opt/choyeon-panel-backup", "/opt/choyeon-panel")
        self.assertIn("ACCEPT", r.stdout, r.stdout + r.stderr)

    def test_require_tty_message(self):
        """_bash 的 stdin 是 DEVNULL（非 tty）→ 必须以"tty"字样失败退出。"""
        r = _bash('_require_tty "--purge" && echo ACCEPT')
        self.assertNotIn("ACCEPT", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 1)
        self.assertIn("tty", r.stderr + r.stdout)


class TestUnitNameDetection(unittest.TestCase):
    """服务名里的 `.` 是正则元字符：旧写法 `grep -q "^$SERVICE_NAME"` 会误匹配。"""

    def _detect(self, service_name, listing):
        """复刻 uninstall.sh 的检测链：pattern 参数 + awk 首列 + grep -qxF。"""
        probe = subprocess.run(
            ["bash", "-c",
             'printf "%s\\n" "$1" | awk \'{print $1}\' | grep -qxF -- "$2.service" && echo MATCH || echo NO',
             "sh", listing, service_name],
            capture_output=True, text=True, timeout=30)
        return "MATCH" in probe.stdout

    def test_dot_in_service_name_does_not_match_lookalike(self):
        # 模拟：真实存在的单元叫 choyeonXpanel.service，SERVICE_NAME=choyeon.panel
        self.assertFalse(self._detect("choyeon.panel", "choyeonXpanel.service enabled -"))
        # 旧写法确实是误匹配的（证明这条测试不是空跑）
        old = subprocess.run(["bash", "-c",
                              'grep -q "^$1" <<<"$2" && echo MATCH || echo NO',
                              "sh", "choyeon.panel", "choyeonXpanel.service enabled -"],
                             capture_output=True, text=True, timeout=30)
        self.assertIn("MATCH", old.stdout, "旧写法的误匹配未被复现，测试意义需要重新评估")

    def test_exact_name_still_matches(self):
        self.assertTrue(self._detect("choyeon-panel", "choyeon-panel.service enabled -"))


class TestCliEntryThroughSymlink(unittest.TestCase):
    """回归：install.sh 把 bin/choyeonctl 软链到 /usr/local/bin，入口却按 dirname 找仓库。

    旧写法解析到 /usr/local/bin/../backend/cli.py，报"仓库结构不完整"退出，
    于是 AGENTS.md 里每条 `choyeonctl ...` 都用不了，只有 ./bin/choyeonctl 能跑。
    """

    def _invoke_via_link(self, link_dir: str, link_name: str):
        entry = os.path.join(REPO, "bin", "choyeonctl")
        link = os.path.join(link_dir, link_name)
        os.symlink(entry, link)
        # 用一个只回显参数的解释器桩：这里要验证的是"入口找到了哪个 cli.py"，
        # 不需要真的跑 CLI（那会连带依赖 fastapi 与数据库）。
        stub = os.path.join(link_dir, "py-stub.sh")
        with open(stub, "w", encoding="utf8") as fh:
            fh.write('#!/bin/sh\necho "CLI=[$1]"\n')
        os.chmod(stub, 0o755)
        env = dict(os.environ)
        env["CP_CLI_PYTHON"] = stub
        env["NO_COLOR"] = "1"
        return subprocess.run([link, "doctor"], capture_output=True, text=True,
                              env=env, timeout=60, stdin=subprocess.DEVNULL)

    def test_symlinked_entry_resolves_repo_cli(self):
        d = tempfile.mkdtemp(prefix="cp_cli_link_")
        self.addCleanup(shutil.rmtree, d, True)
        r = self._invoke_via_link(d, "choyeonctl")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # 入口打印的是拼接出来的路径（含 ../），比较前先规范化
        m = re.search(r"CLI=\[(.*)\]", r.stdout)
        self.assertTrue(m, r.stdout)
        self.assertEqual(
            os.path.realpath(m.group(1)),
            os.path.realpath(os.path.join(REPO, "backend", "cli.py")),
            "软链接调用必须解析回仓库里的 cli.py",
        )

    def test_direct_invocation_still_works(self):
        d = tempfile.mkdtemp(prefix="cp_cli_direct_")
        self.addCleanup(shutil.rmtree, d, True)
        stub = os.path.join(d, "py-stub.sh")
        with open(stub, "w", encoding="utf8") as fh:
            fh.write('#!/bin/sh\necho "CLI=[$1]"\n')
        os.chmod(stub, 0o755)
        env = dict(os.environ)
        env.update({"CP_CLI_PYTHON": stub, "NO_COLOR": "1"})
        r = subprocess.run([os.path.join(REPO, "bin", "choyeonctl"), "doctor"],
                           capture_output=True, text=True, env=env, timeout=60,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cli.py", r.stdout, "./bin/choyeonctl 这条原有路径不能被改坏")


if __name__ == "__main__":
    unittest.main()
