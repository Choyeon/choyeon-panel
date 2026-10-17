"""批次㊼A：说明句里点名的接口，必须真的注册在路由表上。

概览页的告警卡片曾经写着「告警历史接口未接入：后端暂未提供可查询的告警列表接口」，而写这句话的
时候 `/api/alerts/history` 与 `/api/alerts/open` 都已注册——句子是接口还没有时留下的，接口补上后
没人回头改它。这类漂移不会让任何东西报错，却比读不到数据更糟：读页面（或读这段注释）的人照着
一句过期说明做判断。㊿B 收的是「键名」的抄本，这一批收的是「接口存在性」的抄本。

守卫按注册表反查而不是按句子清单反查：`/api/...` 写在文档与前端的哪一行都不重要，重要的是它
能在 OpenAPI 的 116 条路径里对上号；对不上就是缺陷。WebSocket 路由不在 OpenAPI 里，所以那一条
例外由代码里的 `@router.websocket(...)` 装饰器给出，而不是在测试里点一个名字。
"""

import ast
import re
import unittest
from pathlib import Path

from app import main

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
APP = BACKEND / "app"

# 正文里写出来的接口路径。字符类里不含引号、括号、反引号，所以 markdown 与注释里的
# 句子天然在标点处断开，不需要再手工剥尾标点。
API_TOKEN = re.compile(r"/api/[A-Za-z0-9_./{}-]+")
BRACED = re.compile(r"\{[^{}]*\}")
SAMPLE_ID = re.compile(r"/\d+(?=/|$)")


def normalize(path: str) -> str:
    """`/api/apps/12/deploy` 与 `/api/apps/{id}/deploy` 归成同一个形状，好跟注册表比。"""
    return BRACED.sub("{}", SAMPLE_ID.sub("/{id}", path))


def matches(registered: str, want: str) -> bool:
    """逐段比对：注册路径里的 `{参数}` 段能吃掉正文写的任意一段。"""
    got, need = registered.strip("/").split("/"), want.strip("/").split("/")
    return len(got) == len(need) and all(
        g == w or g.startswith("{") for g, w in zip(got, need, strict=True))


def http_paths() -> set[str]:
    return set(main.app.openapi()["paths"])


def websocket_paths() -> set[str]:
    """从源码的 `@router.websocket("...")` 装饰器取，OpenAPI 不登记 WebSocket。"""
    out: set[str] = set()
    for p in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                f = dec.func
                if (isinstance(f, ast.Attribute) and f.attr == "websocket"
                        and dec.args and isinstance(dec.args[0], ast.Constant)
                        and isinstance(dec.args[0].value, str)):
                    out.add(dec.args[0].value)
    return out


def references() -> list[tuple[str, str]]:
    """(归一化后的路径, 出处) —— 覆盖前端与仓库根的文档，一处不落。"""
    files: list[Path] = sorted((REPO / "web" / "src").rglob("*"))
    files += [REPO / name for name in ("README.md", "RUNBOOK.md", "AGENTS.md",
                                       "SECURITY.md", "CONTRIBUTING.md")]
    docs = REPO / "docs"
    if docs.is_dir():
        files += sorted(docs.rglob("*.md"))
    out: list[tuple[str, str]] = []
    for f in files:
        if not f.is_file() or f.suffix not in (".vue", ".ts", ".js", ".mjs", ".md"):
            continue
        for m in API_TOKEN.findall(f.read_text(encoding="utf-8", errors="replace")):
            out.append((normalize(m), str(f.relative_to(REPO))))
    return out


class ApiPathReferenceTests(unittest.TestCase):
    """凡是写给人看的 `/api/...`，都得是面板真提供的接口。"""

    @classmethod
    def setUpClass(cls):
        cls.http = http_paths()
        cls.ws = websocket_paths()
        cls.refs = references()

    def test_对照表不是空的(self):
        """扫描一旦退化成「什么都没扫到」，下面那条守卫会安静地永远绿。"""
        self.assertGreaterEqual(len(self.refs), 10,
                                f"只找到 {len(self.refs)} 处接口引用，多半是扫描路径写错了")
        self.assertGreaterEqual(len(self.http), 50, "OpenAPI 路径表读空了")

    def test_正文点名的接口都注册在路由表上(self):
        bad = [(p, src) for p, src in self.refs
               if not any(matches(r, p) for r in self.http | self.ws)]
        self.assertEqual(bad, [], f"这些写在文档/页面里的接口并不存在：{sorted(set(bad))}")

    def test_不在openapi里的例外确实是注册过的websocket路由(self):
        """例外由装饰器给，不由测试点名：接口改名而装饰器还在，例外仍然成立。"""
        self.assertIn("/api/terminal", self.ws)
        only_ws = {p for p, _ in self.refs
                   if not any(matches(r, p) for r in self.http)}
        self.assertTrue(only_ws <= self.ws, f"这些借口走 WebSocket 的豁免其实没有注册：{only_ws}")


class DashboardAlertCardCopyTests(unittest.TestCase):
    """概览页那张告警卡片：话要说它实际读的那条路由，不许对后端的能力下断言。"""

    PATH = (REPO / "web" / "src" / "views" / "Dashboard.vue").read_text(encoding="utf-8")

    def test_卡片读的路由确实注册着(self):
        """这句话曾经断言「后端没有可查询的告警列表接口」——先证明它是假的，再锁住文案。"""
        want = self._requested_path()
        self.assertTrue(any(matches(r, want) for r in http_paths()),
                        f"卡片请求 {want}，但路由表里没有它（那才是该改代码而不是改文案）")

    def test_过期说明句不再出现(self):
        for stale in ("接口未接入", "暂未提供", "已接入", "未接入"):
            self.assertNotIn(stale, self.PATH, f"概览页还写着「{stale}」这类对后端能力的断言")

    def test_读不到的时候点名具体路径(self):
        """三态文案要各自说清是哪一步失败；「未接入」把探测失败说成后端没这个能力，
        管理员会去翻代码，而真正的问题可能只是这一轮请求没发出去。"""
        want = self._requested_path()
        rows = [line for line in self.PATH.splitlines()
                if "alertsState === 'absent'" in line and "text=" in line]
        self.assertEqual(len(rows), 1, f"absent 分支的说明句应当只有一条，读到 {len(rows)} 条")
        text = re.search(r'text="([^"]+)"', rows[0])
        self.assertIsNotNone(text, f"absent 分支没有 text：{rows[0].strip()[:80]}")
        self.assertIn(want, text.group(1),
                      f"404 的说明要点名 {want}，而不是泛泛说「接口未接入」")

    def test_三档判词点名的是这张卡实际读的数据(self):
        """这张卡读的是 `/alerts/open`（未确认告警），而「告警历史」是另一条路由的名字：
        混着写会让管理员去查历史接口，而真正失败的只是这一跳的未确认列表。"""
        rows = [line for line in self.PATH.splitlines()
                if "EmptyBox" in line and re.search(r"alertsState === '(absent|error|loading)'", line)]
        self.assertEqual(len(rows), 3, f"三档判词应当各一条，读到 {len(rows)} 条")
        for line in rows:
            self.assertIn("未确认告警", line, f"这一档没说清它读的是哪份数据：{line.strip()[:70]}")
        self.assertNotIn("告警历史", self.PATH, "概览页把「未确认告警」这张卡写成另一条路由的名字")

    def test_状态标签说的是数据可不可信而不是接口接没接(self):
        """标签是给人判断「这张卡现在能不能信」用的，不是给实现进度打卡的。"""
        tag = re.search(r"\{\{ alertsState === 'ready' \? '([^']*)'", self.PATH)
        self.assertIsNotNone(tag, "ready 档的标签文案找不到了")
        self.assertNotIn("接入", tag.group(1), "绿色标签在讲实现进度，不是在讲这份数据")

    @staticmethod
    def _requested_path() -> str:
        """卡片实际请求的那条路径 = api.ts 的 base + `req('...')` 的参数（query 不算路径），
        从源码里读，不写死：文案与调用处必须同源，改一处就能被这里抓到。"""
        api = (REPO / "web" / "src" / "api.ts").read_text(encoding="utf-8")
        base = re.search(r"const base = '([^']*)'", api).group(1)
        call = re.search(r"await req\('(/alerts/[^'?]+)", DashboardAlertCardCopyTests.PATH)
        assert call, "概览页的告警请求调用处变了，这个守卫要看住的就是它"
        return normalize(base + call.group(1))
