"""仓库门面一致性：README 引用的资产、版本号、社区健康文件。

这些检查只在 CI/本地测试里跑，目的是让"门面"退化（README 里图片路径写错、
版本号两处不同步、误删 LICENSE）立刻失败，而不是等到 GitHub 页面渲染出问题。
"""

import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", str(Path(tempfile.gettempdir()) / "cp-hygiene-data"))
os.environ.setdefault("CP_FILE_ROOTS", "/tmp/cp-hygiene-roots")

REPO = Path(__file__).resolve().parents[2]


def _md_files():
    return [p for p in REPO.glob("*.md")]


class TestRepoHygiene(unittest.TestCase):
    maxDiff = None

    def test_社区健康文件存在(self):
        for name in (
            "LICENSE",
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "AGENTS.md",
            "RUNBOOK.md",
            ".github/dependabot.yml",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            "assets/logo.svg",
        ):
            with self.subTest(name=name):
                self.assertTrue((REPO / name).is_file(), f"缺少 {name}")

    def test_readme_引用的本地文件存在(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        refs = re.findall(r"""(?:\]\(|src=")(?!https?:)([^)"'\s#]+)""", text)
        checked = 0
        for ref in refs:
            ref = ref.strip("<>\"'")
            ref = ref[2:] if ref.startswith("./") else ref
            if not ref or ref.startswith(("`", "$")):
                continue
            with self.subTest(ref=ref):
                self.assertTrue((REPO / ref).exists(), f"README 引用了不存在的文件：{ref}")
            checked += 1
        self.assertGreaterEqual(checked, 10, "README 本地引用过少，正则可能已失配")

    def test_版本号三处一致(self):
        sys.path.insert(0, str(REPO / "backend"))
        try:
            from app import config
        finally:
            sys.path.pop(0)
        pkg = re.search(r'"version"\s*:\s*"([^"]+)"', (REPO / "web/package.json").read_text(encoding="utf-8"))
        proj = re.search(r'^version\s*=\s*"([^"]+)"', (REPO / "backend/pyproject.toml").read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(pkg, "web/package.json 没有 version 字段")
        self.assertIsNotNone(proj, "backend/pyproject.toml 没有 [project].version")
        versions = {config.VERSION, pkg.group(1), proj.group(1)}
        self.assertEqual(len(versions), 1, f"版本号不同步：{versions}")

    def test_资产均被文档引用且为合法svg(self):
        corpus = "\n".join(p.read_text(encoding="utf-8") for p in _md_files())
        svgs = sorted((REPO / "assets").glob("*.svg"))
        self.assertGreaterEqual(len(svgs), 5, "assets/ 下的门面资产数量异常")
        for svg in svgs:
            with self.subTest(asset=svg.name):
                root = ET.fromstring(svg.read_text(encoding="utf-8"))
                self.assertTrue(root.tag.endswith("svg"), "根元素不是 <svg>")
                self.assertIn("viewBox", root.attrib, "SVG 缺少 viewBox，GitHub 上会按原始尺寸裁切")
                self.assertIn(f"assets/{svg.name}", corpus, f"{svg.name} 未被任何 markdown 文档引用（孤儿资产）")

    def test_doctor_项数与文档口径一致(self):
        corpus = "\n".join(p.read_text(encoding="utf-8") for p in _md_files())
        claimed = set(re.findall(r"(\d+)\s*项(?:只读)?检查", corpus))
        claimed |= set(re.findall(r"自检\s*(\d+)\s*项", corpus))
        self.assertTrue(claimed, "文档里找不到自检项数的表述")
        self.assertEqual(len(claimed), 1, f"文档对自检项数的说法不一致：{claimed}")

    def test_env_example_覆盖全部配置键(self):
        cfg = (REPO / "backend/app/config.py").read_text(encoding="utf-8")
        sample = (REPO / ".env.example").read_text(encoding="utf-8")
        in_config = set(re.findall(r"\bCP_[A-Z0-9_]+\b", cfg))
        in_sample = set(re.findall(r"\bCP_[A-Z0-9_]+\b", sample))
        self.assertGreaterEqual(len(in_config), 20, "config.py 里没抓到预期数量的环境变量")
        with self.subTest(direction="config → .env.example"):
            self.assertFalse(in_config - in_sample,
                             f"这些配置键在 .env.example 里没有说明：{sorted(in_config - in_sample)}")
        with self.subTest(direction=".env.example → config"):
            self.assertFalse(in_sample - in_config,
                             f".env.example 写明了 config.py 不认识的键：{sorted(in_sample - in_config)}")

    def test_issue_模板字段完整(self):
        for path in sorted((REPO / ".github/ISSUE_TEMPLATE").glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(template=path.name):
                self.assertNotIn("\r\n", text, "模板含 CRLF，GitHub 表单解析会失败")
                if path.name == "config.yml":
                    continue
                self.assertIn("name:", text)
                self.assertIn("body:", text)
                self.assertRegex(text, r"type:\s*(textarea|input|dropdown|checkboxes|markdown)")


if __name__ == "__main__":
    unittest.main()
