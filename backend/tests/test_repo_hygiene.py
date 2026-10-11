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

    def test_readme_徽章链接形态(self):
        """README 的 shields.io 徽章必须带 cacheSeconds，且查询串只能用 `?` 起头。

        两个缺陷都在 GitHub 页面上才看得见：不带 `cacheSeconds` 时 camo 冷缓存会 504，
        徽章整块裂图；把 `&cacheSeconds=…` 直接拼到 `/badge/label-message-color` 后面
        （没有 `?`）时 shields 仍然回 200，但 `&…` 被当成颜色值的一部分解析失败，
        徽章退化成默认灰——自定义色和缓存时长一起丢掉。
        """
        text = (REPO / "README.md").read_text(encoding="utf-8")
        urls = re.findall(r"https://img\.shields\.io/[^\s\")]+", text)
        self.assertGreaterEqual(len(urls), 7, f"README 徽章少于 7 个（读到 {len(urls)} 个），疑似徽章行被删")
        for url in urls:
            with self.subTest(url=url):
                self.assertIn("cacheSeconds=", url, "徽章没设 cacheSeconds，camo 冷缓存会 504 破图")
                self.assertLessEqual(url.count("?"), 1, f"查询串里出现多个 `?`：{url}")
                if "&" in url:
                    self.assertIn("?", url, f"`&` 之前没有 `?`，{url} 的参数全部不会被解析")
                    self.assertLess(url.index("?"), url.index("&"), "`&` 出现在第一个 `?` 之前，参数不会被解析")

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

    def test_品牌标记三处同源且不依赖字体(self):
        """标签页图标、README logo、banner 里的品牌标记必须是同一套几何。

        三处各画一份的结局是"标签页一个 logo、README 另一个"；而图标一旦用 <text>
        画字母，没有相应字体的渲染器（GitHub 的图片代理、精简过的 Linux 系统）会把
        字形整个丢掉，图标退化成一块纯色方块。
        """
        def sig(el, scale):
            plate = next(c for c in el if _tag(c) == "rect" and c.get("fill-opacity"))
            poly = next(c for c in el if _tag(c) == "polyline")
            dot = [c for c in el if _tag(c) == "circle"][-1]
            n = lambda vals: [round(float(v) / scale, 1) for v in vals]  # noqa: E731
            return (
                n([plate.get(k) for k in ("x", "y", "width", "height", "rx")]),
                n(re.split(r"[,\s]+", poly.get("points").strip())),
                n([dot.get(k) for k in ("cx", "cy", "r")]),
            )

        fav = ET.fromstring((REPO / "web/public/favicon.svg").read_text(encoding="utf-8"))
        logo = ET.fromstring((REPO / "assets/logo.svg").read_text(encoding="utf-8"))
        banner = ET.fromstring((REPO / "assets/banner.svg").read_text(encoding="utf-8"))
        mark = next(
            g for g in banner
            if _tag(g) == "g" and "translate(80,74)" in (g.get("transform") or "")
        )

        base = sig(fav, 1)
        self.assertEqual(sig(logo, 1), base, "assets/logo.svg 与 favicon 的几何已经分叉")
        self.assertEqual(sig(mark, 2), base, "banner 里的品牌标记不是 favicon 的 2 倍等比")

        for path in ("web/public/favicon.svg", "assets/logo.svg"):
            root = ET.fromstring((REPO / path).read_text(encoding="utf-8"))
            with self.subTest(asset=path):
                self.assertFalse([e for e in root.iter() if _tag(e) == "text"],
                                 f"{path} 用 <text> 画字形，缺字体的渲染器会画空")

    def test_界面品牌位一律走_LogoMark(self):
        """web/src 里不许再出现手写的 `.logo-mark` / `.brand-mark` 字母标记。

        品牌标记收进 components/LogoMark.vue 后，样式也只在组件里定义。漏一处（侧栏改了、
        抽屉没改）不会让构建变红，只会在线上渲染成一个没样式的裸字母。
        """
        offenders = []
        for src in (REPO / "web/src").rglob("*"):
            if not src.is_file() or src.suffix not in {".vue", ".ts", ".css"}:
                continue
            if src.name == "LogoMark.vue":
                continue
            for line_no, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r'class="(logo|brand)-mark"', line):
                    offenders.append(f"{src.relative_to(REPO)}:{line_no}: {line.strip()}")
        self.assertFalse(offenders, "品牌位应改用 <LogoMark />：\n" + "\n".join(offenders))

    def test_入口引用的图标资源存在(self):
        """index.html 里每个 <link> 指向的本地图标都必须真在 web/public 下。

        漏一个就是线上 404，而浏览器对 favicon 的 404 静默处理，只有开 DevTools 才看得到。
        """
        html = (REPO / "web/index.html").read_text(encoding="utf-8")
        hrefs = re.findall(r'<link\b[^>]*\bhref="(/[^"]+)"', html)
        self.assertGreaterEqual(len(hrefs), 5, f"入口只声明了 {len(hrefs)} 个图标链接，疑似被删")
        for href in hrefs:
            with self.subTest(href=href):
                self.assertTrue((REPO / "web/public" / href.lstrip("/")).is_file(),
                                f"{href} 在 index.html 里声明，但 web/public 下没有这个文件")

    def test_theme_color_与背景token一致(self):
        """首屏那一帧的 theme-color 取值要跟 style.css 的 --cp-bg 同值。

        对不上时浏览器工具栏/手机状态栏会先闪一个别的颜色，再在 JS 落地后跳回页面底色。
        CSP 收紧到 script-src 'self' 之后，切主题那段从 index.html 的内联脚本挪进了
        web/public/theme-boot.js，所以"首屏那一帧"= 入口 HTML 的初值 + 那份外链脚本，
        只查 index.html 会把亮色值判成丢失。
        """
        css = (REPO / "web/src/style.css").read_text(encoding="utf-8")
        html = (REPO / "web/index.html").read_text(encoding="utf-8")
        boot = (REPO / "web/public/theme-boot.js").read_text(encoding="utf-8")
        bgs = re.findall(r"--cp-bg:\s*(#[0-9a-fA-F]{6})", css)
        self.assertEqual(len(bgs), 2, f"--cp-bg 应当暗/亮各一处，实际 {bgs}")
        for value in bgs:
            with self.subTest(color=value):
                self.assertIn(value, html + boot, f"{value} 未出现在入口 HTML 的 theme-color 初值或首屏主题脚本里")
        initial = re.search(r'<meta name="theme-color" content="(#[0-9a-fA-F]{6})"', html)
        self.assertIsNotNone(initial, "入口 HTML 必须带 theme-color 初值，JS 落地前那一帧靠它")
        self.assertIn(initial.group(1), bgs, f"theme-color 初值 {initial.group(1)} 不等于任何一档 --cp-bg")


    def test_social_版式不重叠不溢出(self):
        """assets/social.svg 是 GitHub「Social preview」的图源，会被缩到 640×320 显示。

        断言三件事：胶囊行内文字不溢出、胶囊之间不互相压叠、主标题组（字标/kicker/
        两行标语/页脚）两两不重叠——上一轮 banner 的"排版错乱"就是这几条没把住。
        """
        root = ET.fromstring((REPO / "assets/social.svg").read_text(encoding="utf-8"))
        vb = [float(v) for v in root.get("viewBox").split()]

        band = next(g for g in root if _tag(g) == "g" and g.get("font-size"))
        size = float(band.get("font-size"))
        bx, by = _translate(band)
        chips = []
        for chip in band:
            if _tag(chip) != "g":
                continue
            rect = next((c for c in chip if _tag(c) == "rect"), None)
            text = next((c for c in chip if _tag(c) == "text"), None)
            if rect is None or text is None:
                continue
            cx, cy = _translate(chip)
            chips.append({
                "label": (text.text or "").strip(),
                "x": bx + cx,
                "y": by + cy,
                "w": float(rect.get("width")),
                "h": float(rect.get("height")),
                "text_x": bx + cx + float(text.get("x") or 0),
            })
        self.assertGreaterEqual(len(chips), 4, f"没抓到 social 卡的 chips 行：{chips}")
        for ch in chips:
            need = _text_advance(ch["label"], size)
            slack = ch["x"] + ch["w"] - (ch["text_x"] + need)
            self.assertGreaterEqual(slack, 8, f"胶囊「{ch['label']}」字宽溢出（余量 {slack:.0f}）")
            self.assertLess(ch["y"] + ch["h"], vb[3], f"胶囊「{ch['label']}」掉出画布下沿")
        chips.sort(key=lambda c: c["x"])
        for a, b in zip(chips, chips[1:], strict=False):
            self.assertGreaterEqual(b["x"] - (a["x"] + a["w"]), 8,
                                    f"胶囊「{a['label']}」与「{b['label']}」互相压叠")
        self.assertLessEqual(chips[-1]["x"] + chips[-1]["w"], vb[2] - 40, "胶囊行溢出画布右缘")

        def box(el):
            s = float(el.get("font-size"))
            ls = float(el.get("letter-spacing") or 0)
            label = (el.text or "").strip()
            advance = _text_advance(label, s) + ls * max(len(label) - 1, 0)
            return float(el.get("x")), float(el.get("y")), advance, s

        heads = [box(t) for t in root if _tag(t) == "text" and (t.text or "").strip()]
        self.assertGreaterEqual(len(heads), 4, "字标/标语/页脚文本缺失")
        for i, (x1, y1, w1, s1) in enumerate(heads):
            for x2, y2, w2, s2 in heads[i + 1:]:
                top1, bot1 = y1 - s1 * 0.75, y1 + s1 * 0.25
                top2, bot2 = y2 - s2 * 0.75, y2 + s2 * 0.25
                overlap_y = top1 < bot2 and top2 < bot1
                overlap_x = x1 < x2 + w2 and x2 < x1 + w1
                self.assertFalse(overlap_x and overlap_y,
                                 f"文本块 {heads[i][:2]!r} 与 {heads[i + 1][:2]!r} 在版面上重叠")

        for pat in (p for p in root.iter() if _tag(p) == "pattern"):
            if any(_tag(c) == "path" and c.get("stroke") for c in pat):
                self.assertTrue(pat.get("patternTransform"),
                                "网格 pattern 缺少 patternTransform，会在卡片边缘留下贴边亮线")


if __name__ == "__main__":
    unittest.main()
