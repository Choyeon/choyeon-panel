"""批次 62B2：CI 的门不能自己把自己按住。

- **systemd 那条门是永久空转的**：它校验的是模板原文 `deploy/choyeon-panel.service`，里面
  写死 `/root/choyeon-panel/backend/.venv/bin/python`。任何 runner 上都没有这个路径，
  verify 必退 1，于是当初补了 `|| true` —— 从那一刻起 unit 写成什么样都是绿的。
- **verify 的退出码本身也不够**：2026-10-20 在生产机上实测，`MemoryMax=512Q` 与
  `Restart=maybe` 只打印一句 "ignoring" 然后退 0。写错的 cgroup 上限照样"通过"。
- **没有 job 级超时的 job 卡住时不失败**，只烧满 GitHub 的 6 小时默认值，再被下一次 push
  静默取消（后端 verdict 就是这么消失过一次的，见 hygiene 里那条单元测试超时门）。
- **mypy 在 continue-on-error 之上又 `|| true`**：降级一层是"提示项"，再降一层是
  "连没通过这件事都不留痕"。
- **CI 内联 `pip install ruff mypy`**：与 requirements-dev.txt 各装一套，
  "本地复现 CI"这句话没有落点。
- **backend/uv.lock 停在 Node→Python 重写那一刻**：6 个运行时依赖只登记了 4 个
  （psutil、python-multipart 整份缺席），版本号还写着 1.0.0。它不进任何安装路径，
  却进 GitHub 的依赖图与 Dependabot —— 那两个包的 CVE 从此看不见，
  "没登记"被念成"没有漏洞"（AGENTS.md §7.8 禁止伪造自检结果，同一句话适用于门禁）。
- **`make lint` 把 ruff 也 `|| true` 掉了**：ruff 在 CI 是阻断项，本地这条却是永绿的，
  而 PR 模板把它列为提交前勾选项——勾上它的人等于什么都没核对。
- **requirements-dev.txt 的注释承诺"保证服务器 venv 也有"**：与同一文件开头"装测试包会
  污染线上环境"自相矛盾，且与实测不符（生产 venv 只有 ruff）。按它排查的人会在机器上
  找不到 mypy。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
WORKFLOWS = REPO / ".github" / "workflows"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

CI = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
RELEASE = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


def _step_body(text: str, name: str) -> str:
    """取某个 `- name: X` 步骤自己的正文（到下一个同缩进的 `- ` 为止），去掉整行注释。

    注释必须剥掉：这些步骤的正文里如实写着「旧写法是 `... || true`」这类说明，留着它们，
    「这个字面量不许再出现」的断言就会打到自己的解释上——而那正是这一步修掉的东西。
    """
    lines = text.splitlines()
    idx = [k for k, ln in enumerate(lines) if ln.strip() == f"- name: {name}"]
    if not idx:
        # 步骤名带中文括号时按前缀再找一次，避免调用方抄错全名却只收到"没找到"
        idx = [k for k, ln in enumerate(lines) if ln.strip().startswith(f"- name: {name.split('（')[0]}")]
    if not idx:
        raise AssertionError(f"ci.yml 里没有步骤「{name}」")
    base = len(lines[idx[0]]) - len(lines[idx[0]].lstrip(" "))
    out = []
    for ln in lines[idx[0] + 1:]:
        if not ln.strip():
            out.append(ln)
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent <= base:
            break
        if ln.strip().startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def _job_body(text: str, job: str) -> str:
    """取 `jobs:` 下某个 job 的整块正文（到下一个 2 缩进的 job 名为止）。"""
    lines = text.splitlines()
    idx = [k for k, ln in enumerate(lines) if ln == f"  {job}:"]
    if not idx:
        raise AssertionError(f"workflow 里没有 job「{job}」")
    out = []
    for ln in lines[idx[0] + 1:]:
        if re.match(r"^  [A-Za-z_][A-Za-z0-9_-]*:\s*$", ln):
            break
        out.append(ln)
    return "\n".join(out)


def _job_names(text: str) -> list[str]:
    """jobs: 段里的那些 2 缩进键名。"""
    m = re.search(r"^jobs:\s*$", text, re.M)
    assert m, "workflow 里没有 jobs: 段"
    return [ln[:-1].strip() for ln in text[m.end():].splitlines()
            if re.match(r"^  [A-Za-z_][A-Za-z0-9_-]*:\s*$", ln)]


def _real_warnings(text: str) -> list[str]:
    """verify 输出里真正说明问题的行。

    "Unknown key" 单独排除：runner 的 systemd 常比生产旧，生产合法的新指令在旧版本上会被
    报成不认识——那是 runner 的版本差异，不是这份 unit 的错，把它算成红只会逼人来一句 `|| true`。
    """
    return [ln for ln in text.splitlines() if ln.strip() and "Unknown key" not in ln]


class SystemdGateIsReal(unittest.TestCase):
    """那条门必须校验真正会装上去的那份 unit，并且失败真的会红。"""

    def test_step_verifies_the_rendered_unit_not_the_template(self):
        body = _step_body(CI, "systemd-analyze verify（校验真正会装上去的那份 unit）")
        self.assertIn("render_unit", body, "还在校验模板原文：那个路径在 runner 上永远不存在")
        self.assertNotIn("verify ./deploy/choyeon-panel.service", body,
                         "校验对象必须换成 render_unit 渲染出来的那份")
        self.assertIn("$UNIT_OUT", body)

    def test_step_has_no_silencer(self):
        body = _step_body(CI, "systemd-analyze verify（校验真正会装上去的那份 unit）")
        self.assertNotIn("|| true", body,
                         "`|| true` 就是这条门空转的原因：它把必然的 rc=1 按住，"
                         "顺便把以后任何真实的 unit 错误也一起按掉了")
        self.assertNotIn("continue-on-error", body)

    def test_step_fails_on_verify_output_not_only_rc(self):
        """退出码 0 + 一句 "ignoring" 是 systemd 对坏值的真实反应，必须算失败。"""
        body = _step_body(CI, "systemd-analyze verify（校验真正会装上去的那份 unit）")
        self.assertIn('OUT="$(systemd-analyze verify "$UNIT_OUT" 2>&1)" || RC=$?', body,
                      "没接住退出码，也没接住 stderr —— 坏值退 0 时这条门照样看不见")
        self.assertIn('[ -n "$OUT" ]', body)

    @unittest.skipIf(sys.platform == "win32", "需要 systemd")
    @unittest.skipUnless(shutil.which("systemd-analyze"), "本机没有 systemd-analyze")
    def test_the_gate_actually_bites(self):
        """A/B 反证：好 unit 空输出退 0；坏值与非门只是"退出码 0"，必须被第二条判据抓住。"""
        root = Path(tempfile.mkdtemp(prefix="cp_batch62b2_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        prefix = root / "repo"
        (prefix / "deploy").mkdir(parents=True)
        (prefix / "backend" / ".venv" / "bin").mkdir(parents=True)
        shutil.copy(REPO / "deploy" / "choyeon-panel.service", prefix / "deploy")
        py = prefix / "backend" / ".venv" / "bin" / "python"
        py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        py.chmod(0o755)
        out = root / "panel.service"
        env = dict(os.environ, CP_PREFIX=str(prefix))
        r = subprocess.run(
            ["bash", "-c", 'source scripts/common.sh >/dev/null 2>&1; UNIT_FILE="$1"; render_unit >/dev/null',
             "bash", str(out)],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        good = out.read_text(encoding="utf-8")
        self.assertIn(f"ExecStart={prefix}/backend/.venv/bin/python", good,
                      "render_unit 没把模板前缀换成 $PREFIX，校验的就不是装上去那份")

        g = self._verify(good)
        self.assertEqual(g["rc"], 0, f"当前这份 unit 本该干净通过：{g['out']!r}")
        self.assertEqual(_real_warnings(g["out"]), [],
                         "干净通过不该有任何非版本差异的输出——有的话这条门在 runner 上就是假红")

        # 1) 坏值：systemd 打印一句 "ignoring" 就可能退 0 —— 只有"输出也算失败"这条判据抓得到
        for frag in ("MemoryMax=512Q", "Restart=maybe"):
            key, val = frag.split("=")
            text = good.replace(f"{key}=512M" if key == "MemoryMax" else f"{key}=always", frag)
            self.assertNotEqual(text, good, f"替身没换上 {frag}，这条反证等于没跑")
            v = self._verify(text)
            caught = [ln for ln in _real_warnings(v["out"]) if val in ln]
            self.assertTrue(caught,
                            f"{frag} 既没被退出码也没被输出抓住（rc={v['rc']}，输出={v['out']!r}）："
                            "这条门只会看退出码的话，写错的资源上限照样算通过")

        # 2) ExecStart 指向不存在的路径：这条才是真的退非 0
        moved = good.replace(str(prefix), str(root / "nope"))
        v = self._verify(moved)
        self.assertNotEqual(v["rc"], 0, "解释器都不存在了还退 0，这条门没有意义")

        # 3) 模板原文（旧门的校验对象）：它写死 /root/choyeon-panel，在没装过面板的机器上
        #    必然退 1 —— 那正是 `|| true` 当年的由来，也是它后来一直空转的原因。
        template = (REPO / "deploy" / "choyeon-panel.service").read_text(encoding="utf-8")
        if not Path("/root/choyeon-panel/backend/.venv/bin/python").exists():
            v = self._verify(template)
            self.assertNotEqual(v["rc"], 0,
                                "模板原文在这台机器上也能过，说明校验对象选错了这件事"
                                "还没被这条反证钉住")

    def _verify(self, text: str) -> dict:
        d = Path(tempfile.mkdtemp(prefix="cp_b62b2_unit_"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        f = d / "u.service"
        f.write_text(text, encoding="utf-8")
        r = subprocess.run(["systemd-analyze", "verify", str(f)],
                           capture_output=True, text=True, timeout=60)
        return {"rc": r.returncode, "out": (r.stdout or "") + (r.stderr or "")}


class EveryJobHasATimeout(unittest.TestCase):
    """没有上限的 job 卡住时不失败，只会被下一次 push 静默取消。"""

    def test_ci_jobs_all_have_a_cap(self):
        for job in _job_names(CI):
            body = _job_body(CI, job)
            m = re.search(r"^\s{4}timeout-minutes:\s*(\d+)\s*$", body, re.M)
            self.assertIsNotNone(m, f"ci.yml 的 job「{job}」没有 timeout-minutes")
            cap = int(m.group(1))
            self.assertLessEqual(cap, 90, f"job「{job}」上限 {cap} 分钟：超过 90 分钟就是卡住")

    def test_release_job_has_a_cap(self):
        for job in _job_names(RELEASE):
            body = _job_body(RELEASE, job)
            m = re.search(r"^\s{4}timeout-minutes:\s*(\d+)\s*$", body, re.M)
            self.assertIsNotNone(m, f"release.yml 的 job「{job}」没有 timeout-minutes："
                                    "tag 触发的那次发布卡住时既不出产物也不报错")


class DegradedStepsStayVisible(unittest.TestCase):
    """降级要有降级一层就够了；降两层等于把"没通过"这件事本身抹掉。"""

    def test_mypy_step_is_advisory_once(self):
        body = _step_body(CI, "Mypy 类型检查（提示性，不阻断）")
        self.assertIn("continue-on-error: true", body)
        self.assertNotIn("|| true", body,
                         "continue-on-error 已经让它不阻断，再抹一次退出码就只剩"
                         "「这一步好像过了」")
        self.assertIn("run: mypy app", body)

    def test_failure_summary_shows_the_traceback(self):
        """失败摘要只有测试名字 = 还是要靠猜。

        2026-10-20 run 39398186981：后端 job 红了，注解里只有一句
        `ERROR: test_the_gate_actually_bites`——unittest 的 traceback 在那行往下十几行，
        而摘要取的是 `tail -80`，尾部已被后面的 CLI 冒烟输出占满，永远够不着。
        """
        body = _step_body(CI, "单元测试失败摘要")
        self.assertIn("grep -A 22 -E '^(FAIL|ERROR): '", body,
                      "必须按名字往下把 traceback 取出来，只 grep 标题等于没摘要")

    def test_audit_steps_are_blocking(self):
        for name in ("依赖漏洞审计（后端）", "依赖漏洞审计（前端）"):
            body = _step_body(CI, name)
            self.assertNotIn("continue-on-error", body, f"「{name}」不许是提示项")
            self.assertNotIn("|| true", body)
        self.assertIn("pip-audit -r requirements.txt", _step_body(CI, "依赖漏洞审计（后端）"))
        front = _step_body(CI, "依赖漏洞审计（前端）")
        self.assertIn("npm audit --audit-level=high", front,
                      "阈值必须是 high：当前唯一已知项是 echarts 的 moderate，"
                      "把阈值定成它拦不住任何真东西，而全量报 high 会立刻红到被人加 `|| true`")


class DevToolchainHasOneSource(unittest.TestCase):
    def test_requirements_dev_declares_the_tools_ci_uses(self):
        text = (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8")
        for pkg in ("ruff", "mypy", "pip-audit", "httpx2"):
            self.assertRegex(text, rf"(?m)^{re.escape(pkg)}[><=~]",
                             f"requirements-dev.txt 少了 {pkg}：CI 只能内联安装，两边就此分家")

    def test_ci_does_not_install_tools_inline(self):
        body = _step_body(CI, "安装依赖")
        self.assertNotIn("install -q ruff", body, "工具集必须只来自 requirements-dev.txt")
        self.assertNotRegex(body, r"pip install -q (ruff|mypy)")
        self.assertIn("-r requirements-dev.txt", body)

    def test_it_does_not_claim_the_production_venv_has_them(self):
        """本文件开头自己就写着「装测试包会污染线上环境」，注释却不能反过来承诺线上有。

        2026-10-20 在 207.57.127.6 实测：生产 venv 里只有 ruff，mypy 与 pip_audit 都 import 不到。
        上一版注释写的是"这里保证服务器 venv 也有"——那是一句做不到的承诺，
        照着它排查的人会在机器上找不到 mypy 然后怀疑接口坏了。
        """
        text = (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertNotIn("保证服务器 venv 也有", text)
        self.assertRegex(text, r"(生产 venv|venv_create)",
                         "要说清楚这些工具在哪儿跑：CI 装全量，生产 venv 故意不装")

    def test_venv_create_installs_only_production_requirements(self):
        """上一条承诺的根据：装机脚本只装 requirements.txt。若哪天有人把 dev 清单塞进去，
        pip-audit 连带的那三十个包就进了 root 进程的 import 路径——那句话随之变成假话。"""
        text = (REPO / "scripts" / "common.sh").read_text(encoding="utf-8")
        m = re.search(r"^venv_create\(\)\s*\{(.*?)^\}", text, re.S | re.M)
        assert m, "scripts/common.sh 里找不到 venv_create()"
        self.assertNotIn("requirements-dev", m.group(1),
                         "生产安装路径不能带上开发工具集（见 requirements-dev.txt 顶部说明）")


class LocalGateMirrorsCi(unittest.TestCase):
    """make lint 是「本地复现 CI」的落点：每一步的阻断性必须和 CI 里那一步一样。"""

    def _lint_recipe(self) -> str:
        text = (REPO / "Makefile").read_text(encoding="utf-8")
        m = re.search(r"^lint:.*?\n((?:\t.*\n)+)", text, re.M)
        assert m, "Makefile 里没有 lint 目标"
        return m.group(1)

    def test_ruff_is_blocking_locally(self):
        recipe = self._lint_recipe()
        self.assertNotRegex(recipe, r"ruff check[^\n]*\|\| true",
                            "ruff 在 CI 是阻断项，本地却 `|| true`：勾过 PR 模板那条复选框的"
                            "人其实什么都没核对，直到 CI 红才发现")
        self.assertIn("ruff check .", recipe)

    def test_mypy_stays_advisory_and_says_so(self):
        recipe = self._lint_recipe()
        m = re.search(r"mypy app[^\n]*", recipe)
        assert m, "make lint 里没有 mypy"
        self.assertIn("提示", m.group(0), "mypy 不阻断这件事要当场说出来，别让人以为它过了")

    def test_docs_describe_the_same_blockingness(self):
        for rel in ("README.md", "CONTRIBUTING.md", ".github/PULL_REQUEST_TEMPLATE.md"):
            text = (REPO / rel).read_text(encoding="utf-8")
            hits = [ln for ln in text.splitlines() if "make lint" in ln]
            self.assertTrue(hits, f"{rel} 里提不到 make lint")
            # 说明行不必每处都写全（正文里可能只是顺带一提），但必须至少有一处讲清
            # 这一步会不会阻断，否则勾选项读起来像"跑过就算核对过"。
            self.assertTrue(any("阻断" in ln for ln in hits),
                            f"{rel}：{len(hits)} 处提到 make lint 却没有一处说明谁会阻断——"
                            "说明句必须和 Makefile/CI 的实际阻断性同源")


class DependencyManifestsAgree(unittest.TestCase):
    """清单少写一个包 = 那个包的 CVE 从此不在任何人的视野里。"""

    def _pyproject_deps(self) -> set[str]:
        text = (BACKEND / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r"dependencies\s*=\s*\[(.*?)\n\]", text, re.S)
        assert m, "pyproject.toml 没有 [project].dependencies"
        return {n.lower() for n in re.findall(r'"([A-Za-z0-9_.-]+)', m.group(1))}

    def _requirements_deps(self) -> set[str]:
        text = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
        out = set()
        for ln in text.splitlines():
            ln = ln.split("#")[0].strip()
            if ln:
                out.add(re.split(r"[<>=!~\[]", ln)[0].strip().lower())
        return out

    def test_pyproject_and_requirements_name_the_same_packages(self):
        """两份清单互相抄，注释写着"保持一致"，但此前没有任何东西守着这句话。"""
        p, q = self._pyproject_deps(), self._requirements_deps()
        self.assertEqual(p - q, set(), f"pyproject 有而 requirements.txt 没有：{p - q}")
        self.assertEqual(q - p, set(), f"requirements.txt 有而 pyproject 没有：{q - p}")

    def test_uv_lock_declares_every_runtime_dependency(self):
        lock = (BACKEND / "uv.lock").read_text(encoding="utf-8")
        root = re.search(r'name = "choyeon-panel-backend".*?\n\n', lock, re.S)
        assert root, "uv.lock 里没有 choyeon-panel-backend 这一段"
        declared = {n.lower() for n in re.findall(r'\{ name = "([A-Za-z0-9_.-]+)"', root.group(0))}
        # PyPI 名与 import 名在 requires-dist 里按原样写；比对做大小写与连字符归一
        norm = lambda s: s.replace("_", "-").lower()  # noqa: E731
        want = {norm(d) for d in self._pyproject_deps()}
        have = {norm(d) for d in declared}
        missing = want - have
        self.assertEqual(missing, set(),
                         f"uv.lock 少登记了 {missing}：它不进安装路径，却进 GitHub 依赖图与 "
                         "Dependabot——少一个包就等于那个包的 CVE 永久不可见（本批次实测缺 psutil、"
                         "python-multipart，锁还停在 v1.0.0）")

    def test_uv_lock_version_tracks_the_project(self):
        lock = (BACKEND / "uv.lock").read_text(encoding="utf-8")
        py = (BACKEND / "pyproject.toml").read_text(encoding="utf-8")
        mine = re.search(r'^version\s*=\s*"([^"]+)"', py, re.M).group(1)
        theirs = re.search(r'name = "choyeon-panel-backend"\nversion = "([^"]+)"', lock).group(1)
        self.assertEqual(theirs, mine,
                         f"锁里写的还是 {theirs}，项目已经是 {mine}：读锁的人看到的版本是假的")


if __name__ == "__main__":
    unittest.main()
