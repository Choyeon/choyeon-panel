"""批次㊼C：死代码清掉之后，把这次审计发现的两个守卫盲区钉住。

原有守卫只查「代码里用了、CSS 里没定义」这一头，于是 `style.css` 里六条
`.tbl-*` 的 `--chrome-extra` 可以在模板早已改走 `useTableHeight()` 之后继续躺着——
它不报错、不影响构建，只让下一个人以为改数值有效果。`api.ts` 的包装更没人查：
端点活着不等于包装活着，`overview` 就是这么死的。

四类断言：
1) style.css 里每条 `.tbl-*` 必须真被某个模板/渲染函数挂上（CSS → 代码方向）；
2) `api.ts` 每个导出的包装都必须有调用者；
3) 同一份字面量只许有一个出处（备份产物后缀）；
4) 已经删掉的死代码不许被重新引用（`sys_helper` / `config.as_dict` / `CP_PYTHON` /
   `util.is_ident`）。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"
# 扫仓库时必须躲开这两棵树：venv 里几万个 .py 会让守卫跑到天荒地老，
# 而且第三方库里本来就有 as_dict()/CP_ 之类的字符串。
SKIP_DIRNAME = {".venv", "__pycache__", "node_modules", "dist"}


def _py_texts(*roots):
    for rel in roots:
        p = ROOT / rel
        if p.is_file():
            yield rel, p.read_text(encoding="utf-8")
            continue
        for f in sorted(p.rglob("*.py")):
            if any(part in SKIP_DIRNAME for part in f.parts):
                continue
            # 本文件把后缀字面量抄在常量里当被检对象，不能把自己算成第二份出处
            if f.name == Path(__file__).name:
                continue
            # as_posix()：Windows 上 str(Path) 会给反斜杠，和下面断言里的正斜杠口径对不上
            yield f.relative_to(ROOT).as_posix(), f.read_text(encoding="utf-8")


def web_sources():
    """所有可能挂 class 或调 api 的文件（api.ts 自己除外）。"""
    out = []
    for p in list(WEB.rglob("*.vue")) + list(WEB.rglob("*.ts")):
        if p.name == "api.ts":
            continue
        out.append((p, p.read_text(encoding="utf-8")))
    return out


class TableChromeHasAUser(unittest.TestCase):
    def test_every_tbl_rule_is_attached(self):
        css = (WEB / "style.css").read_text(encoding="utf-8")
        defined = re.findall(r"^\.(tbl-[a-z0-9-]+) \{", css, re.M)
        self.assertTrue(defined, "style.css 里一条 .tbl-* 都没有，说明这段样式被整体挪走了，"
                                 "请连同本守卫一起改掉")
        srcs = web_sources()
        orphans = [name for name in defined if not any(name in text for _p, text in srcs)]
        self.assertEqual(orphans, [],
                         f"这些 .tbl-* 只写在 style.css 里、没有任何模板挂它（死样式，"
                         f"改数值不会有任何效果）：{orphans}")

    def test_single_class_helpers_stay_gone(self):
        """`.mono-1l`/`.stat-num` 是零引用的单类名规则，删掉后不许再长回来。"""
        css = (WEB / "style.css").read_text(encoding="utf-8")
        for dead in (".stat-num", ".mono-1l"):
            with self.subTest(rule=dead):
                self.assertNotIn(f"{dead} {{", css)


class ApiWrappersAreUsed(unittest.TestCase):
    def test_no_dead_wrapper(self):
        api_text = (WEB / "api.ts").read_text(encoding="utf-8")
        keys = re.findall(r"^  ([A-Za-z][A-Za-z0-9]*): ", api_text, re.M)
        self.assertGreater(len(keys), 50, "api.ts 的键没解析出来（写法变了？先读文件再改守卫）")
        srcs = web_sources()
        dead = []
        for k in keys:
            called = any(re.search(r"\bapi\." + re.escape(k) + r"\b", t) for _p, t in srcs)
            destructured = any(
                re.search(r"\{[^{}]*\b" + re.escape(k) + r"\b[^{}]*\}\s*=\s*api", t) for _p, t in srcs
            )
            if not (called or destructured):
                dead.append(k)
        self.assertEqual(dead, [], f"这些 api 包装没有任何调用者（端点可能还活着，包装是死的）：{dead}")


class BackupSuffixHasOneOwner(unittest.TestCase):
    LITERAL = '(".tar.gz", ".sql.gz", ".dump.gz", ".sql", ".dump", ".gz")'

    def test_literal_appears_once(self):
        hits = [rel for rel, text in _py_texts("backend/app", "backend/tests",
                                               "backend/cli.py", "backend/backup_runner.py")
                if self.LITERAL in text]
        self.assertEqual(hits, ["backend/app/services/backup_service.py"],
                         "备份后缀又出现第二份字面量：CLI 与自检项会各自漂走，"
                         "定时跑出来的 PG 备份会在 `backup list` 里像不存在一样")

    def test_readers_use_the_constant(self):
        for rel in ("backend/cli.py", "backend/app/routers/meta.py"):
            with self.subTest(file=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn("backup_service.ARTIFACT_SUFFIXES", text)


class DeadCodeStaysDead(unittest.TestCase):
    """生产代码 + 入口 + 部署件 + 文档里不许再引用这批已经删掉的东西。

    标记都取得很具体：`sys_helper` 这个名字留在 `system_metrics.py` 的开头当教训
    （「旧实现每 3 秒轮询就 fork 一个 python3 跑 sys_helper.py」），那是文档不是引用；
    `as_dict` 更是 psutil 的公开方法名，满仓库都是，只有 `config.as_dict` 才是本次删掉的那个。
    """

    SCOPE = ("backend/app", "backend/tests", "backend/cli.py", "backend/backup_runner.py",
             "bin", "scripts", "deploy", ".env.example", "README.md", "AGENTS.md",
             "SECURITY.md", "RUNBOOK.md", "web/src")
    MARKERS = ("CP_PYTHON", "SYS_HELPER", "config.as_dict", "def as_dict",
               "import sys_helper", "from sys_helper", "backend/python",
               "export type CachedFetch<", "function cacheSize",
               "function progressState", "function liveResizeObservers")

    def _texts(self):
        for rel in self.SCOPE:
            p = ROOT / rel
            if p.is_file():
                yield rel, p.read_text(encoding="utf-8")
            elif p.is_dir():
                for f in sorted(p.rglob("*")):
                    if any(part in SKIP_DIRNAME for part in f.parts):
                        continue
                    # 本文件为了守卫这些符号，正文里必须点名它们，不能把自己算成复活
                    if f.name == Path(__file__).name:
                        continue
                    if f.is_file() and f.suffix in (".py", ".sh", ".ts", ".vue", ".css",
                                                    ".service", ".example", ".md", ""):
                        yield str(f.relative_to(ROOT)), f.read_text(encoding="utf-8")

    def test_markers_absent(self):
        for rel, text in self._texts():
            for marker in self.MARKERS:
                with self.subTest(file=rel, marker=marker):
                    self.assertNotIn(marker, text)

    def test_util_ident_rule_is_gone(self):
        """标识符规则只留 pg_service 那一份（util 那份不收大写，与生产口径不一致）。"""
        util = (ROOT / "backend" / "app" / "util.py").read_text(encoding="utf-8")
        self.assertNotIn("IDENT_RE", util)
        self.assertNotIn("def is_ident", util)
        pg = (ROOT / "backend" / "app" / "services" / "pg_service.py").read_text(encoding="utf-8")
        self.assertIn("def is_ident", pg)

    def test_sys_helper_file_is_gone(self):
        self.assertFalse((ROOT / "backend" / "python").exists(),
                         "backend/python 里那个第二套 /proc 解析器已经没人调用了")


class WebNamedExportsAreUsed(unittest.TestCase):
    """web/src 的具名 export 必须在 web/ 里有第二个词边界出现。

    批次115 留下三个"探针用"的一行 getter，注释说有 jsdom 整页探针在读，仓库里却一个
    读者都没有；`CachedFetch<T>` 类型同理——`useCachedFetch()` 的返回体一直是推断的。
    这四类东西 vue-tsc / vite build 全绿也照不出来。Vue SFC 的 default export 按路径被
    router 引，不在这条口径里；`web/tests` 算读者，注释里写"探针用"不算。
    """

    def _corpus(self):
        out = []
        for p in (ROOT / "web").rglob("*"):
            if not p.is_file() or any(part in SKIP_DIRNAME for part in p.parts):
                continue
            if p.suffix in (".ts", ".vue", ".mjs", ".js", ".html", ".css", ".json"):
                out.append(p.read_text(encoding="utf-8", errors="ignore"))
        return "\n".join(out)

    def test_no_zero_reader_export(self):
        corpus = self._corpus()
        self.assertGreater(len(corpus), 10000, "web/ 语料没读到，先看 _corpus 的过滤条件")
        dead = []
        for p in sorted(WEB.rglob("*.ts")):
            text = p.read_text(encoding="utf-8")
            names = re.findall(
                r"^export\s+(?:default\s+)?(?:async\s+)?"
                r"(?:const|let|function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
                text, re.M,
            )
            for name in names:
                hits = len(re.findall(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", corpus))
                if hits < 2:
                    dead.append(f"{p.relative_to(ROOT)}: {name}")
        self.assertEqual(dead, [], "这些具名 export 除了定义行没有任何读者（死代码）：" + "、".join(dead))


if __name__ == "__main__":
    unittest.main()
