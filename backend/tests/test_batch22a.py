"""批次㉒A：部署参数只有一个写入者，`.env` 缺失不再让自检一片绿。

这一层锁的是"改配置的正规入口"本身：

- `.env` 由 install.sh 的 render_env 与 common.sh 的 sync_env 两个写入者生成，键集还不一样
  （少 CP_BACKUP_DIR / CP_APP_ROOT / CP_FILE_ROOTS）。两个写入者迟早给出两个答案，所以只留
  sync_env 一个，示例文件到 `.env` 的播种收进 `ensure_env`。
- 播种整个 `.env.example` 必须是行为中性的：示例里每个键的值都要等于 config.py 的默认值。
  这条由测试逐键比对（`_int`/`_path`/`os.environ.get`/`_bool` 四种写法都算），改默认忘了改
  示例就会红——否则下一次 `upgrade --no-pull` 会静悄悄把生产配置换成示例值。唯一豁免是
  unit 模板写死的那几个键（CP_TRUST_PROXY）：它们靠 systemd 的 Environment= 压过 .env 才无害，
  而"模板里确实还写着这一行"本身也是一条用例，模板一改、豁免失效，测试就红。
- `.env` 过去只有 install.sh 会生成：手工克隆或旧版本升上来的机器上它不存在，正规入口指向一个
  没有的文件，面板跑在内置默认上却全绿。现在 deploy / upgrade --no-pull 会补齐，自检项
  「配置来源」负责把"没有 .env"和"`.env` 与 unit 各说一个值"两种情况都说出来。
- 对账只看那六个会同时写进 .env 与 unit 的键；别的键（CP_TOKEN_TTL_HOURS 之类）两边本来就该
  各管各的，拿它报错是假阳性。
- 探测不到就别说绿灯：读不到 unit 是 warn，不是 pass。
- 严格只读：这一项不给一键修复按钮，fix 是给人复制走的 `choyeonctl upgrade --no-pull`。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch22a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch22a_roots_"))

import unittest  # noqa: E402

from app import config  # noqa: E402
from app.routers import meta  # noqa: E402

REPO = Path(config.BASE)
COMMON = (REPO / "scripts" / "common.sh").read_text(encoding="utf-8")
INSTALL = (REPO / "scripts" / "install.sh").read_text(encoding="utf-8")
AGENTS = (REPO / "AGENTS.md").read_text(encoding="utf-8")
EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")
CONFIG_SRC = (REPO / "backend" / "app" / "config.py").read_text(encoding="utf-8")
UNIT_TEMPLATE = (REPO / "deploy" / "choyeon-panel.service").read_text(encoding="utf-8")
# 允许"示例值 ≠ 代码默认"的键：它们由 unit 模板写死，而 systemd 的 Environment= 高于 .env，
# 所以播种进 .env 的这份值改变不了任何跑起来的行为。豁免的前提是被下面那条用例逐键验证的，
# 不是"我记得无害"。
UNIT_PINNED_KEYS = {"CP_TRUST_PROXY"}
DEPLOY_KEYS = list(meta.ENV_DEPLOY_KEYS)


def code_of(text: str) -> str:
    """剥掉整行注释后的 shell 代码——否则"render_env 已经不在了"会被自己的注释命中。"""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def env_map(text: str) -> dict[str, str]:
    return config._parse_env_file(Path(_write_tmp(text)))


_TMP: list[str] = []


def _write_tmp(content: str) -> str:
    import tempfile as _tf

    fd, p = _tf.mkstemp(prefix="cp22a_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    _TMP.append(p)
    return p


def unit_text(pairs: dict[str, str]) -> str:
    lines = ["[Unit]", "Description=choyeon panel", "[Service]", "Type=simple"]
    lines += [f"Environment={k}={v}" for k, v in pairs.items()]
    lines += ["[Install]", "WantedBy=multi-user.target"]
    return "\n".join(lines) + "\n"


class TestSingleWriterInScripts(unittest.TestCase):
    def test_install_no_longer_has_its_own_writer(self):
        """install.sh 不再自己渲染 .env：那第二个写入者的键集比 sync_env 少三个。"""
        self.assertNotIn("render_env", code_of(INSTALL))
        self.assertNotIn("render_env", code_of(COMMON))

    def test_seed_and_sync_live_in_one_place(self):
        code = code_of(COMMON)
        self.assertIn("ensure_env()", code)
        body = code[code.index("env_sync_all() {"):]
        # 播种必须是 env_sync_all 的第一步，排在任何 sync_env 之前
        self.assertLess(body.index("ensure_env"), body.index("sync_env CP_HOST"))

    def test_missing_env_is_no_longer_a_silent_skip(self):
        """过去 .env 不在就直接 return：文档说的正规入口因此可以永远指向一个不存在的文件。"""
        self.assertNotIn('[ -f "$PREFIX/.env" ] || return 0', code_of(COMMON))

    def test_chmod_is_applied_once(self):
        """权限归 sync_env 自己管（它每次都重写文件），install.sh 里那份是第三处副本。"""
        self.assertNotIn('chmod 0600 "$PREFIX/.env"', code_of(INSTALL))
        self.assertIn('chmod 0600 "$PREFIX/.env"', code_of(COMMON))


class TestExampleSeedingIsBehaviorNeutral(unittest.TestCase):
    """`ensure_env` 复制的是整份示例，所以示例值必须等于代码默认值。"""

    def _defaults_from_config(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in re.finditer(r'_int\("(CP_[A-Z_]+)",\s*(\d+)', CONFIG_SRC):
            out[m.group(1)] = m.group(2)
        for m in re.finditer(r'_path\w*\("(CP_[A-Z_]+)",\s*"([^"]*)"', CONFIG_SRC):
            out[m.group(1)] = m.group(2)
        for m in re.finditer(r'os\.environ\.get\("(CP_[A-Z_]+)",\s*"([^"]*)"\)', CONFIG_SRC):
            out[m.group(1)] = m.group(2)
        # _bool 的默认值写成 False/True，不是字符串，扫描漏掉它等于给"示例值 == 代码默认"
        # 这句话开一个不会报错的后门——CP_TRUST_PROXY 正好就从这里溜过去过。
        for m in re.finditer(r'_bool\("(CP_[A-Z_]+)"(?:,\s*(True|False))?\)', CONFIG_SRC):
            out[m.group(1)] = "1" if m.group(2) == "True" else "0"
        return out

    def test_every_comparable_example_key_matches_the_code_default(self):
        want = self._defaults_from_config()
        self.assertGreaterEqual(len(want), 12, "config.py 的默认值写法变了，这个扫描要跟着改")
        got = env_map(EXAMPLE)
        bad = [(k, got[k], want[k]) for k in sorted(want)
               if k in got and k not in UNIT_PINNED_KEYS and got[k] != want[k]]
        self.assertEqual(bad, [], "示例值与代码默认不一致：" + "；".join(
            f"{k} 示例={a} 默认={b}" for k, a, b in bad))

    def test_the_exempted_keys_really_are_pinned_by_the_unit(self):
        """豁免名单不是"我记得无害"：这些键必须真的写死在 unit 模板里。

        播种整份示例会把这些键也写进 .env。它们之所以改变不了行为，唯一依据是 systemd
        注入的 Environment= 优先于 .env——一旦模板里那一行被删掉或改成渲染出来的值，
        .env 里那份就成了生效值，而它和代码默认是不一样的（CP_TRUST_PROXY 示例=1、默认=0，
        真被 .env 采信就是"没装反代的机器也开始采信 X-Real-IP"）。
        """
        self.assertIn("CP_TRUST_PROXY", self._defaults_from_config(),
                      "_bool 的扫描掉了，这个豁免就没有对象了")
        for key in sorted(UNIT_PINNED_KEYS):
            self.assertIn(f"Environment={key}=", UNIT_TEMPLATE)

    def test_deploy_keys_are_present_in_the_example(self):
        """六个部署键要在示例里，播种后 sync_env 才有行可改（sync_env 也会补写缺失键，
        但少了键就变成"追加到文件末尾"，示例里读不到它们会让运维以为改错了地方）。"""
        got = env_map(EXAMPLE)
        self.assertEqual([k for k in DEPLOY_KEYS if k not in got], [])


class TestDoctorEnvItem(unittest.TestCase):
    def _run(self, tmp: str, env_text: str | None, unit_pairs: dict[str, str] | None):
        if unit_pairs is not None:
            Path(tmp, "choyeon-panel.service").write_text(unit_text(unit_pairs), encoding="utf-8")
        if env_text is not None:
            Path(tmp, ".env").write_text(env_text, encoding="utf-8")
        with mock.patch.object(config, "ENV_FILE", str(Path(tmp, ".env"))), \
                mock.patch.object(config, "UNIT_DIR", tmp):
            return asyncio.run(meta._check_env_file())

    def test_missing_unit_is_not_reported_as_green(self):
        d = tempfile.mkdtemp(prefix="cp22a_nounit_")
        with mock.patch.object(config, "ENV_FILE", str(Path(d, ".env"))), \
                mock.patch.object(config, "UNIT_DIR", str(Path(d, "no-such-dir"))):
            res = asyncio.run(meta._check_env_file())
        self.assertEqual(res["status"], "warn")
        self.assertIn("无法与 .env 对账", res["detail"])

    def test_missing_env_file_warns_with_the_self_heal_command(self):
        d = tempfile.mkdtemp(prefix="cp22a_noenv_")
        res = self._run(d, None, {"CP_PORT": "3210"})
        self.assertEqual(res["status"], "warn")
        self.assertIn("upgrade --no-pull", res["fix"])
        self.assertIn("不存在", res["detail"])

    def test_aligned_keys_pass(self):
        d = tempfile.mkdtemp(prefix="cp22a_ok_")
        unit = {k: f"/var/x/{i}" if "DIR" in k or "ROOT" in k else f"v{i}"
                for i, k in enumerate(DEPLOY_KEYS)}
        res = self._run(d, "\n".join(f"{k}={v}" for k, v in unit.items()) + "\n", unit)
        self.assertEqual(res["status"], "pass", res)
        self.assertIn("一致", res["detail"])

    def test_port_drift_is_a_fail_naming_both_values(self):
        d = tempfile.mkdtemp(prefix="cp22a_drift_")
        unit = {"CP_PORT": "3210", "CP_HOST": "127.0.0.1"}
        res = self._run(d, "CP_PORT=3300\nCP_HOST=127.0.0.1\n", unit)
        self.assertEqual(res["status"], "fail")
        self.assertIn("CP_PORT", res["detail"])
        self.assertIn("3300", res["detail"])
        self.assertIn("3210", res["detail"])
        self.assertIn("upgrade --no-pull", res["fix"])

    def test_a_key_the_unit_does_not_carry_is_not_compared(self):
        """unit 里没有这个键就没得对——拿它报不一致是假阳性。"""
        d = tempfile.mkdtemp(prefix="cp22a_partial_")
        res = self._run(d, "CP_PORT=3210\nCP_FILE_ROOTS=/a,/b\n", {"CP_PORT": "3210"})
        self.assertEqual(res["status"], "pass", res)

    def test_a_key_the_env_does_not_carry_is_still_divergence(self):
        """反过来不行：env_sync_all 总会写齐六个键，缺一个说明有人删过，
        而 status 因此退回默认值——这正是"部署成功却验收失败"的坏法。"""
        d = tempfile.mkdtemp(prefix="cp22a_missing_")
        res = self._run(d, "CP_HOST=127.0.0.1\n", {"CP_HOST": "127.0.0.1", "CP_PORT": "3210"})
        self.assertEqual(res["status"], "fail", res)
        self.assertIn("（没有这一行）", res["detail"])

    def test_quoted_unit_values_are_unquoted_before_compare(self):
        d = tempfile.mkdtemp(prefix="cp22a_quote_")
        res = self._run(d, "CP_PORT=3210\n", {"CP_PORT": '"3210"'})
        self.assertEqual(res["status"], "pass", res)

    def test_unrelated_key_drift_does_not_trip_the_item(self):
        d = tempfile.mkdtemp(prefix="cp22a_other_")
        res = self._run(d, "CP_PORT=3210\nCP_TOKEN_TTL_HOURS=99\n", {"CP_PORT": "3210"})
        self.assertEqual(res["status"], "pass", res)

    def test_registered_and_not_one_click_fixable(self):
        ids = [c[0] for c in meta._CHECKS]
        self.assertIn("env", ids)
        self.assertNotIn("env", meta.FIXABLE)
        titled = dict((c[0], c[1]) for c in meta._CHECKS)["env"]
        self.assertEqual(titled, "配置来源")


class TestAgentsDocStillMatchesReality(unittest.TestCase):
    def test_doctor_item_count_in_docs(self):
        m = re.search(r"只读安全与健康自检（当前 (\d+) 项", AGENTS)
        self.assertIsNotNone(m, "AGENTS.md 里那句项数不见了")
        self.assertEqual(int(m.group(1)), len(meta._CHECKS),
                         f"文档写 {m.group(1)} 项，实际 {len(meta._CHECKS)} 项")

    def test_docs_say_the_env_file_gets_seeded(self):
        """§5 的正规入口在任何机器上都存在，这件事得写在文档里。"""
        self.assertIn("按 `.env.example` 生成一份", AGENTS)


if __name__ == "__main__":
    unittest.main()
