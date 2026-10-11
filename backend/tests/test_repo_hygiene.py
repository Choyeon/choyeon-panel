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


def _tag(el):
    return el.tag.rsplit("}", 1)[-1]


def _translate(el):
    m = re.search(r"translate\(([^)]*)\)", el.get("transform") or "")
    if not m:
        return 0.0, 0.0
    nums = [float(v) for v in re.split(r"[,\s]+", m.group(1).strip()) if v]
    return nums[0], (nums[1] if len(nums) > 1 else 0.0)


def _text_advance(text, size):
    """无字体环境下的保守估算：CJK 按 1em，数字/大写 0.62em，其余拉丁 0.55em，再留 10% 渲染器余量。"""
    units = 0.0
    for ch in text:
        if ord(ch) >= 0x2E80:
            units += 1.0
        elif ch == " ":
            units += 0.28
        elif ch.isupper() or ch.isdigit():
            units += 0.62
        else:
            units += 0.55
    return units * size * 1.1


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

    def test_banner_版式不重叠不溢出(self):
        """banner.svg 的三条几何契约：chips 行不压右侧卡片、标签不顶破胶囊、网格不贴画布边缘。

        对应 GitHub 首页真实出现过的"排版错乱"。这里只做坐标断言（不依赖字体光栅化），
        像素级复验配方见 CONTRIBUTING.md 的门面资产一节。
        """
        root = ET.fromstring((REPO / "assets/banner.svg").read_text(encoding="utf-8"))
        vb = [float(v) for v in root.get("viewBox").split()]

        chips = []
        for band in root:
            # chips 由一个不带 transform、统一设 font-size 的父组管理；带 transform 的是插图区块。
            if _tag(band) != "g" or band.get("transform") or not band.get("font-size"):
                continue
            size = float(band.get("font-size"))
            for chip in band:
                if _tag(chip) != "g":
                    continue
                rect = next((c for c in chip if _tag(c) == "rect"), None)
                text = next((c for c in chip if _tag(c) == "text"), None)
                if rect is None or text is None or not (text.text or "").strip():
                    continue
                cx, cy = _translate(chip)
                chips.append(
                    {
                        "label": text.text.strip(),
                        "x": cx,
                        "y": cy,
                        "w": float(rect.get("width")),
                        "h": float(rect.get("height")),
                        "text_x": cx + float(text.get("x") or 0),
                        "size": size,
                    }
                )
        self.assertGreaterEqual(len(chips), 4, f"没抓到 banner 的 chips 行：{chips}")

        cards = []
        for group in root:
            if _tag(group) != "g":
                continue
            gx, gy = _translate(group)
            for rect in (c for c in group if _tag(c) == "rect"):
                if float(rect.get("width") or 0) >= 300 and float(rect.get("height") or 0) >= 150:
                    cards.append({"x": gx, "y": gy, "w": float(rect.get("width")), "h": float(rect.get("height"))})
        self.assertTrue(cards, "banner 右侧卡片消失，版式已重构，请同步本测试")

        for card in cards:
            for ch in chips:
                vertical = ch["y"] < card["y"] + card["h"] and card["y"] < ch["y"] + ch["h"]
                if not vertical:
                    continue
                gap = card["x"] - (ch["x"] + ch["w"])
                self.assertGreater(
                    gap, 40, f"chip「{ch['label']}」右端距卡片左缘仅 {gap:.0f}px，GitHub 缩放后会被卡片压住"
                )

        for ch in chips:
            self.assertGreaterEqual(ch["text_x"] - ch["x"], 8, f"chip「{ch['label']}」文字贴住胶囊左缘")
            need = _text_advance(ch["label"], ch["size"])
            slack = ch["x"] + ch["w"] - (ch["text_x"] + need)
            self.assertGreaterEqual(slack, 8, f"chip「{ch['label']}」估算字宽 {need:.0f} 溢出胶囊（余量 {slack:.0f}）")

        full_canvas = {
            (r.get("fill") or "")[5:-1]
            for r in root.iter()
            if _tag(r) == "rect"
            and (r.get("fill") or "").startswith("url(#")
            and float(r.get("x") or 0) == 0.0
            and float(r.get("y") or 0) == 0.0
            and float(r.get("width") or 0) >= vb[2]
            and float(r.get("height") or 0) >= vb[3]
        }
        for pat in (p for p in root.iter() if _tag(p) == "pattern"):
            stroked_tile = any(_tag(c) == "path" and c.get("stroke") for c in pat)
            if stroked_tile and not pat.get("patternTransform") and pat.get("id") in full_canvas:
                self.fail(
                    f"网格 pattern #{pat.get('id')} 从 (0,0) 平铺满画布且无 patternTransform，"
                    "会在 banner 顶边/左边留下一条贴边亮线；请给平铺加半格偏移"
                )

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

    def test_测试命令统一走_run_tests(self):
        """Makefile 与 CI 必须用 backend/run_tests.py 跑单测。

        裸 `unittest discover` 不读 conftest.py，缺"忽略外部 CP_DATA_DIR"那道保护，
        在生产机上带着 .env 里的变量跑一次就会写坏线上库。
        """
        for rel in ("Makefile", ".github/workflows/ci.yml"):
            text = (REPO / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertIn("run_tests.py", text, f"{rel} 没有通过 backend/run_tests.py 跑测试")
                self.assertNotIn("-m unittest discover", text,
                                 f"{rel} 仍在直接调用 unittest discover，会绕过数据目录隔离")

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
