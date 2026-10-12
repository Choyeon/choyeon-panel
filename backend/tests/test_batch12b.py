"""批次⑫B：存储占用对账（storage_service）+ 自检项 + `choyeonctl storage`。

这一层的风险不是"数字差一点"，而是"数字看起来准其实骗了人"，所以锁的方向是：
- 遍历必须真的只读：符号链接只按链接本身记账，跟进去就能被一条指向 / 的软链刷爆盘；
  跨挂载点的条目跳过，否则同一份数据在两个根里各算一遍，合计就是假的；
- 扫不完要如实标 partial：深度上限与 2 秒预算命中时给的是下界，不能悄悄报一个偏小的
  精确数字，也不能为了凑数字把盘刷满；
- 判词阈值锁边界：journal 2GiB / 备份 4GiB / 分区 90% / 单文件 2GiB，差一档就不该出声——
  误报三遍之后没人再看这一页；
- journalctl 读不到时报 None 而不是 0：把"没查到"写成 0 就等于宣布"日志没占地方"；
- 这一项永不 fail，也不进一键修复白名单：vacuum、删旧备份都是删文件，面板不能替管理员
  把手放在按钮上。
"""

import asyncio
import contextlib
import io
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch12b_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch12b_bak_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch12b_roots_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app import config, main, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import storage_service as ss  # noqa: E402

KIB = 1024
MIB = 1024**2
GIB = 1024**3


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


def _run(coro):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            data = asyncio.run(coro)
        except SystemExit as e:
            data, code = None, e.code or 0
    return data, out.getvalue(), err.getvalue(), code


def _ids(findings):
    return [f["id"] for f in findings]


class _St:
    def __init__(self, size=0, blocks=None, dev=None):
        self.st_size = size
        self.st_blocks = (size + 511) // 512 if blocks is None else blocks
        self.st_dev = dev


class _Entry:
    """DirEntry 替身：边界条件（跨设备、stat 失败、软链）在真文件系统上造不出来。"""

    def __init__(self, name, path, *, kind="file", size=0, blocks=None, dev=None, stat_error=None):
        self.name = name
        self.path = path
        self.kind = kind
        self.st = _St(size, blocks, dev)
        self.stat_error = stat_error

    def is_symlink(self):
        return self.kind == "link"

    def stat(self, follow_symlinks=False):  # noqa: ARG002 - 替身不区分跟随与否
        if self.stat_error:
            raise OSError(self.stat_error)
        return self.st

    def is_dir(self, follow_symlinks=False):  # noqa: ARG002
        return self.kind == "dir"

    def is_file(self, follow_symlinks=False):  # noqa: ARG002
        return self.kind == "file"


class _Iter:
    def __init__(self, it):
        self.it = it

    def __enter__(self):
        return self.it

    def __exit__(self, *_exc):
        return False

    def __iter__(self):
        return self.it


def _file(size):
    return {"size": size}


def _dir(size=None):
    return {"dir": True}


def _link(size=64):
    return {"link": True, "size": size}


def _foreign(size):
    """同名的跨设备条目：真实机器上是 bind mount / 独立分区挂进来的一层。"""
    return {"size": size, "dev": "foreign"}


def _broken():
    return {"stat_error": "stale file handle"}


def _fake_walk(spec, **kw):
    """spec: {相对目录: {名字: 描述}}，"" 是根；没列出的目录 scandir 抛 EACCES。

    root 用一个真目录（_walk 会先 stat 它拿 st_dev），里面的条目只存在于树里。
    """
    real = tempfile.mkdtemp(prefix="cp_12b_tree_")
    base_dev = os.stat(real).st_dev
    used = []

    def scandir(path):
        rel = os.path.relpath(str(path), real).replace(os.sep, "/")
        rel = "" if rel == "." else rel
        rows = spec.get(rel)
        if rows is None:
            raise PermissionError(13, "not permitted", str(path))
        prefix = [p for p in rel.split("/") if p]
        entries = []
        for name, d in rows.items():
            attrs = dict(d)
            attrs.setdefault("dev", base_dev)
            if attrs.pop("dir", False):
                attrs["kind"] = "dir"
            if attrs.pop("link", False):
                attrs["kind"] = "link"
            if attrs.get("dev") == "foreign":
                attrs["dev"] = base_dev + 1234
            entries.append(_Entry(name, os.path.join(real, *prefix, name), **attrs))
        return _Iter(iter(entries))

    try:
        with mock.patch.object(os, "scandir", scandir):
            got = ss._walk(real, **kw)
        used.append(real)
        return got
    finally:
        shutil.rmtree(real, True)


class EntrySizeTests(unittest.TestCase):
    def test_blocks_win_over_size(self):
        self.assertEqual(ss._entry_size(_St(size=10, blocks=8)), 4096)

    def test_sparse_file_falls_back_to_size(self):
        # st_blocks=0 的稀疏文件按 block 算是 0，报 0 就等于说"这个文件没占地方"。
        self.assertEqual(ss._entry_size(_St(size=5 * GIB, blocks=0)), 5 * GIB)

    def test_missing_stat_fields(self):
        self.assertEqual(ss._entry_size(_St(size=-3, blocks=0)), 0)


class WalkRealFsTests(unittest.TestCase):
    def test_symlink_not_followed(self):
        root = tempfile.mkdtemp(prefix="cp_12b_link_")
        self.addCleanup(shutil.rmtree, root, True)
        Path(root, "big.bin").write_bytes(b"x" * (4 * MIB))
        os.symlink(str(Path(root, "big.bin")), str(Path(root, "link.bin")))
        # 指向自己的软链：跟进去就是无限递归，遍历必须只按链接本身记一笔。
        os.symlink(str(Path(root, "loop")), str(Path(root, "loop")))
        got = ss._walk(root)
        self.assertEqual(got["files"], 3)
        self.assertLess(got["bytes"], 8 * MIB)
        self.assertFalse(got["partial"])

    def test_subdir_bytes_land_in_first_level_branch(self):
        root = tempfile.mkdtemp(prefix="cp_12b_real_")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(Path(root, "sub"))
        Path(root, "sub", "a.bin").write_bytes(b"z" * (2 * MIB))
        Path(root, "b.bin").write_bytes(b"z" * MIB)
        got = ss._walk(root)
        self.assertEqual(got["files"], 2)
        self.assertEqual(got["dirs"], 1)
        self.assertEqual(got["subdirs"][0]["name"], "sub")
        # 一级榜只收进过子目录的那部分；根目录自己的散落文件不挂任何分支。
        self.assertEqual(got["subdirs"][0]["bytes"], 2 * MIB)
        self.assertEqual(got["bytes"], 3 * MIB)


class WalkTreeTests(unittest.TestCase):
    def test_cross_device_entries_skipped(self):
        spec = {"": {"mine": _file(1 * MIB), "mnt": _foreign(100 * MIB)}}
        got = _fake_walk(spec)
        self.assertEqual(got["bytes"], 1 * MIB)
        self.assertEqual(got["files"], 1)

    def test_depth_limit_marks_partial(self):
        # 第 4 层还是个目录时它进不来，必须如实标 partial，而不是报一个偏小的精确数。
        spec = {"": {"a": _dir()}, "a": {"b": _dir()}, "a/b": {"c": _dir()},
                "a/b/c": {"deep": _dir()}}
        got = _fake_walk(spec, max_depth=4)
        self.assertTrue(got["partial"])
        self.assertEqual(got["bytes"], 0)
        self.assertEqual(got["subdirs"], [])

    def test_budget_exhaustion_marks_partial(self):
        spec = {"": {f"f{i}": _file(1024) for i in range(60)}}
        got = _fake_walk(spec, budget=0.0)
        self.assertTrue(got["partial"])
        self.assertLess(got["files"], 60)

    def test_unreadable_dir_counted_as_error_not_exception(self):
        spec = {"": {"sub": _dir()}}  # sub 本身不在树里 → scandir 它时 EACCES
        got = _fake_walk(spec)
        self.assertEqual(got["errors"], 1)
        self.assertEqual(got["dirs"], 1)

    def test_stat_failure_counted_as_error(self):
        spec = {"": {"ghost": _broken(), "ok": _file(2048)}}
        got = _fake_walk(spec)
        self.assertEqual(got["errors"], 1)
        self.assertEqual(got["bytes"], 2048)

    def test_branch_ranking_and_cap(self):
        spec = {"": {"small": _dir(), "big": _dir(), "zero": _dir()},
                "small": {"f": _file(1 * MIB)},
                "big": {"f": _file(3 * MIB), "g": _file(2 * MIB)}}
        got = _fake_walk(spec)
        self.assertEqual([s["name"] for s in got["subdirs"]], ["big", "small"])
        self.assertEqual(got["subdirs"][0]["bytes"], 5 * MIB)
        # 空目录进不了榜单，也就不该被算进"未列出"的个数里。
        self.assertEqual(got["branch_count"], 2)

    def test_more_than_top_subdirs_only_lists_largest(self):
        spec = {"": {f"d{i}": _dir() for i in range(10)}}
        spec.update({f"d{i}": {"f": _file((i + 1) * MIB)} for i in range(10)})
        got = _fake_walk(spec)
        self.assertEqual(len(got["subdirs"]), ss.TOP_SUBDIRS)
        self.assertEqual(got["subdirs"][0]["name"], "d9")
        self.assertGreater(got["dirs"], ss.TOP_SUBDIRS)
        # "未列出"的个数按一级分支算：拿 dirs（全部层级）去减会把 10 个说成几十个。
        self.assertEqual(got["branch_count"], 10)

    def test_big_files_capped_and_sorted(self):
        spec = {"": {f"f{i}": _file((i + 1) * MIB) for i in range(ss.TOP_FILES + 5)}}
        got = _fake_walk(spec)
        sizes = [b["bytes"] for b in got["big_files"]]
        self.assertEqual(len(sizes), ss.TOP_FILES)
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(sizes[0], 13 * MIB)

    def test_missing_root_returns_empty_shape(self):
        got = ss._walk("/cp/definitely/not/here")
        self.assertEqual(got["bytes"], 0)
        self.assertEqual(got["errors"], 0)
        self.assertEqual(got["branch_count"], 0)
        self.assertFalse(got["partial"])


class HumanTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(ss._human(0), "0B")
        self.assertEqual(ss._human(1023), "1023B")
        self.assertEqual(ss._human(2 * KIB), "2.0KB")
        self.assertEqual(ss._human(5 * GIB), "5.0GB")

    def test_none_and_negative(self):
        self.assertEqual(ss._human(None), "0B")
        self.assertEqual(ss._human(-5), "0B")


class JournalParseTests(unittest.TestCase):
    def test_real_output_line(self):
        self.assertEqual(ss._parse_journal("Archived and active journals take up 359.6M in the file system."),
                         int(359.6 * MIB))

    def test_other_units(self):
        self.assertEqual(ss._parse_journal("foo 48.0K bar"), 48 * KIB)
        self.assertEqual(ss._parse_journal("foo 2G bar"), 2 * GIB)
        self.assertEqual(ss._parse_journal("foo 512bytes bar"), 512)
        self.assertEqual(ss._parse_journal("用量 1.5 GiB"), int(1.5 * GIB))

    def test_number_without_unit_is_not_a_size(self):
        # 少了这条约束，输出里的路径、年份数字都会被抓成"大小"。
        self.assertIsNone(ss._parse_journal("journals take up 4 of something"))

    def test_unparsable(self):
        self.assertIsNone(ss._parse_journal(""))
        self.assertIsNone(ss._parse_journal("No space left on device"))


class FindingsTests(unittest.TestCase):
    def _root(self, label, path, size, **kw):
        rec = {"label": label, "path": path, "exists": True, "bytes": size, "files": 1,
               "dirs": 0, "errors": 0, "partial": False, "subdirs": []}
        rec.update(kw)
        return rec

    def _disk(self, pct=50):
        return {"ok": True, "path": "/data", "total": 100 * GIB, "free": (100 - pct) * GIB,
                "used_pct": pct}

    def test_journal_threshold_edge(self):
        self.assertNotIn("journal", _ids(ss._findings([], {"bytes": ss.JOURNAL_WARN - 1}, self._disk(), [])))
        out = ss._findings([], {"bytes": ss.JOURNAL_WARN}, self._disk(), [])
        self.assertEqual(_ids(out), ["journal"])
        self.assertEqual(out[0]["severity"], ss.SEV_WARN)
        self.assertIn("journalctl --vacuum-size=500M", out[0]["fix"])

    def test_journal_unreadable_gives_no_verdict(self):
        # 读不到就既不报数也不假装"日志没占地方"。
        self.assertEqual(_ids(ss._findings([], {"bytes": None}, self._disk(), [])), [])

    def test_backup_threshold_and_counts(self):
        roots = [self._root("面板备份", "/bak", ss.BACKUP_WARN,
                            backups={"count": 12, "oldest": "2026-05-01", "newest": "2026-10-01"})]
        out = ss._findings(roots, {"bytes": None}, self._disk(), [])
        self.assertIn("backups", _ids(out))
        self.assertIn("12 份", out[0]["detail"])
        self.assertIn("2026-05-01", out[0]["detail"])
        self.assertEqual(out[0]["fix"], "choyeonctl backup list")
        light = ss._findings([self._root("面板备份", "/bak", ss.BACKUP_WARN - 1)],
                             {"bytes": None}, self._disk(), [])
        self.assertNotIn("backups", _ids(light))

    def test_missing_backup_dir_is_quiet(self):
        roots = [self._root("面板备份", "/bak", 0, exists=False)]
        self.assertNotIn("backups", _ids(ss._findings(roots, {"bytes": None}, self._disk(), [])))

    def test_tight_partition_is_the_only_risk(self):
        out = ss._findings([], {"bytes": None}, self._disk(90), [])
        self.assertEqual([f["severity"] for f in out], [ss.SEV_RISK])
        self.assertEqual(out[0]["id"], "tight")
        self.assertEqual(ss._findings([], {"bytes": None}, self._disk(89), []), [])

    def test_disk_unreadable_is_not_called_tight(self):
        self.assertEqual(ss._findings([], {"bytes": None}, {"ok": False, "path": "/x"}, []), [])

    def test_partial_reported_as_lower_bound(self):
        roots = [self._root("应用", "/a", 10, partial=True), self._root("应用2", "/b", 10, partial=True)]
        out = ss._findings(roots, {"bytes": None}, self._disk(), [])
        part = next(f for f in out if f["id"] == "partial")
        self.assertEqual(part["severity"], ss.SEV_INFO)
        self.assertEqual(part["title"], "2 个目录没扫完")
        self.assertIn("/a", part["detail"])
        self.assertEqual(part["fix"], "")

    def test_big_files_named_but_capped_at_three(self):
        big = [{"path": f"/d/f{i}", "bytes": ss.BIG_FILE_WARN + i} for i in range(5)]
        out = ss._findings([], {"bytes": None}, self._disk(), big)
        named = [f for f in out if f["id"].startswith("bigfile:")]
        self.assertEqual(len(named), 3)
        self.assertIn("只点出来", named[0]["detail"])
        small = ss._findings([], {"bytes": None}, self._disk(),
                             [{"path": "/d/x", "bytes": ss.BIG_FILE_WARN - 1}])
        self.assertEqual(_ids(small), [])

    def test_big_file_advice_never_tells_anyone_to_rm(self):
        out = ss._findings([], {"bytes": None}, self._disk(),
                           [{"path": "/root/backups/panel/a.sql", "bytes": 3 * GIB}])
        f = next(x for x in out if x["id"].startswith("bigfile:"))
        self.assertIn("不要直接 rm", f["detail"])


class RootsTests(unittest.TestCase):
    def test_file_roots_deduped_against_panel_dirs(self):
        with mock.patch.object(config, "DATA_DIR", "/srv/data"), \
             mock.patch.object(config, "BACKUP_DIR", "/srv/bak"), \
             mock.patch.object(config, "FILE_ROOTS", ["/srv/data", "/srv/www", "/srv/www/", "", None]):
            paths = [r["path"] for r in ss._roots()]
        # 同一个目录登记两次就会把它的占用算两遍，合计随之翻倍。
        self.assertEqual(paths.count("/srv/data"), 1)
        self.assertEqual(len(paths), len(set(os.path.normpath(p) for p in paths)))
        self.assertEqual(paths.count("/srv/www"), 1)

    def test_journal_dir_is_in_scope(self):
        self.assertIn("/var/log/journal", [r["path"] for r in ss._roots()])

    def test_labels_cover_every_root(self):
        for r in ss._roots():
            self.assertTrue(r["label"], r)


class CollectTests(unittest.TestCase):
    def setUp(self):
        # 复刻线上形态：数据目录就住在 FILE_ROOTS 登记的目录里面（/root/www 装着面板自己）。
        self.top = tempfile.mkdtemp(prefix="cp_12b_top_")
        self.data = os.path.join(self.top, "data")
        self.bak = tempfile.mkdtemp(prefix="cp_12b_bak_")
        self.apps = tempfile.mkdtemp(prefix="cp_12b_apps_")
        os.makedirs(self.data)
        for d in (self.top, self.bak, self.apps):
            self.addCleanup(shutil.rmtree, d, True)
        Path(self.data, "panel.db").write_bytes(b"z" * (3 * MIB))
        os.makedirs(Path(self.bak, "manual"))
        for name, day in [("b1.tar.gz", 0), ("b2.tar.gz", 5)]:
            p = Path(self.bak, "manual", name)
            p.write_bytes(b"z" * MIB)
            stamp = 1_700_000_000 + day * 86400
            os.utime(p, (stamp, stamp))
        patcher = [
            mock.patch.object(config, "DATA_DIR", self.data),
            mock.patch.object(config, "BACKUP_DIR", self.bak),
            mock.patch.object(config, "FILE_ROOTS", [self.top]),
        ]
        for p in patcher:
            p.start()
            self.addCleanup(p.stop)

    def test_shape_humans_and_backup_stats(self):
        picked = ss._collect()
        by_label = {r["label"]: r for r in picked["roots"]}
        # 根与根必须同键（backups 只有备份那一行有）：网页是按同一套字段渲染一张表的，
        # 少一个键就要在前端补一句 if，缺的那个数也就静默没了。
        keysets = {frozenset(r) for r in picked["roots"] if "backups" not in r}
        self.assertEqual(len(keysets), 1, "根记录字段不齐")
        self.assertIn("note", keysets.pop())
        self.assertTrue(by_label["面板数据"]["exists"])
        self.assertGreaterEqual(by_label["面板数据"]["bytes"], 3 * MIB)
        baks = by_label["面板备份"]
        self.assertTrue(baks["exists"])
        self.assertEqual(baks["backups"]["count"], 2)
        self.assertNotEqual(baks["backups"]["oldest"], baks["backups"]["newest"])
        self.assertEqual(baks["subdirs"][0]["name"], "manual")
        # 每个根、每个一级子目录、分区都自带 human 值：CLI 与网页都只读这些字段，不各自换算。
        for r in picked["roots"]:
            self.assertEqual(r["bytes_human"], ss._human(r["bytes"]))
            for sd in r["subdirs"]:
                self.assertEqual(sd["bytes_human"], ss._human(sd["bytes"]))
        self.assertTrue(picked["disk"]["ok"])
        self.assertEqual(picked["disk"]["total_human"], ss._human(picked["disk"]["total"]))
        self.assertEqual(picked["disk"]["free_human"], ss._human(picked["disk"]["free"]))

    def test_big_files_from_the_same_path_are_listed_once(self):
        """线上真实形态：DATA_DIR 就落在 FILE_ROOTS 的 /root/www 里面。

        同一个文件会在两个根的榜单里各出现一次，合计也会把它算两遍——大文件榜去重，
        "合计"只算最外层那一份，被包含的那一行自己说明这件事。
        """
        picked = ss._collect()
        paths = [b["path"] for b in picked["big_files"]]
        self.assertEqual(len(paths), len(set(paths)))
        inner = next(r for r in picked["roots"] if r["path"] == self.data)
        outer = next(r for r in picked["roots"] if r["path"] == self.top)
        self.assertEqual(inner["nested_in"], self.top)
        self.assertIsNone(outer["nested_in"])
        self.assertFalse(outer["bytes"] < inner["bytes"])

    def test_missing_dir_is_reported_not_guessed(self):
        gone = str(Path(self.apps, "nope"))
        with mock.patch.object(config, "FILE_ROOTS", [gone]):
            rec = next(r for r in ss._collect()["roots"] if r["path"] == gone)
        self.assertFalse(rec["exists"])
        self.assertEqual(rec["bytes"], 0)
        self.assertIn("不存在", rec["note"])


class _AuditCase(unittest.TestCase):
    """audit() 的夹具：造一份可控的 _collect 结果，不真走盘也不跑 journalctl。"""

    def setUp(self):
        ss.reset_cache()
        self.addCleanup(ss.reset_cache)
        self.calls = {"collect": 0, "journal": 0}
        self.roots = [
            {"label": "面板数据", "path": "/data", "exists": True, "bytes": 2 * GIB,
             "bytes_human": "2.0GB", "files": 3, "dirs": 0, "errors": 0, "partial": False,
             "subdirs": []},
            {"label": "面板备份", "path": "/bak", "exists": True, "bytes": 6 * GIB,
             "bytes_human": "6.0GB", "files": 9, "dirs": 1, "errors": 0, "partial": False,
             "subdirs": [{"name": "manual", "bytes": 6 * GIB, "bytes_human": "6.0GB"}],
             "backups": {"count": 30, "oldest": "2026-01-01", "newest": "2026-10-01"}},
        ]
        self.disk = {"ok": True, "path": "/data", "total": 40 * GIB, "total_human": "40.0GB",
                     "free": 20 * GIB, "free_human": "20.0GB", "used_pct": 50}
        self.big = [{"path": "/data/dump.sql", "bytes": 3 * GIB, "bytes_human": "3.0GB"}]

    def _patch(self, journal_bytes=1 * GIB):
        async def fake_journal():
            self.calls["journal"] += 1
            return journal_bytes, "Archived and active journals take up 1.0G in the file system."

        def fake_collect():
            self.calls["collect"] += 1
            return {"roots": [dict(r) for r in self.roots], "disk": dict(self.disk),
                    "big_files": [dict(b) for b in self.big]}

        for target, fn in (("_journal_bytes", fake_journal), ("_collect", fake_collect)):
            p = mock.patch.object(ss, target, fn)
            p.start()
            self.addCleanup(p.stop)


class AuditTests(_AuditCase):
    def test_findings_counts_and_totals(self):
        self._patch()
        d = asyncio.run(ss.audit())
        self.assertFalse(d["cached"])
        self.assertEqual(d["counts"]["roots"], 2)
        self.assertEqual(d["counts"]["warn"], 1)  # 备份 6GiB 过线，journal 1GiB 没过
        self.assertEqual(d["counts"]["risk"], 0)
        self.assertEqual(d["counts"]["bytes_total"], 8 * GIB)
        self.assertEqual(d["counts"]["bytes_total_human"], "8.0GB")
        self.assertEqual(d["journal"]["bytes_human"], "1.0GB")
        self.assertEqual(d["notes"], list(ss.NOTES))
        self.assertIn("+00:00", d["as_of"])

    def test_sync_walk_runs_off_the_event_loop(self):
        self._patch()
        seen = {"loop": "未调用"}

        def fake_collect():
            try:
                seen["loop"] = asyncio.get_running_loop()
            except RuntimeError:  # 工作线程里没有 running loop，这正是我们要的样子
                seen["loop"] = None
            return {"roots": [], "disk": {"ok": False}, "big_files": []}

        with mock.patch.object(ss, "_collect", fake_collect):
            asyncio.run(ss.audit())
        self.assertIsNone(seen["loop"], "_collect 被直接在事件循环上调用了：走盘会卡住整个面板")

    def test_ttl_cache_hit_and_refresh_bypass(self):
        self._patch()
        asyncio.run(ss.audit())
        second = asyncio.run(ss.audit())
        self.assertTrue(second["cached"])
        self.assertEqual(self.calls["collect"], 1)
        self.assertEqual(self.calls["journal"], 1)
        third = asyncio.run(ss.audit(refresh=True))
        self.assertFalse(third["cached"])
        self.assertEqual(self.calls["collect"], 2)

    def test_expired_cache_recomputes(self):
        self._patch()
        asyncio.run(ss.audit())
        ss._cache["at"] -= ss.CACHE_TTL + 1
        asyncio.run(ss.audit())
        self.assertEqual(self.calls["collect"], 2)

    def test_journal_failure_reports_none(self):
        async def failing():
            return None, "Failed to open system journal: No space left on device"

        def fake_collect():
            return {"roots": [dict(r) for r in self.roots], "disk": dict(self.disk), "big_files": []}

        with mock.patch.object(ss, "_journal_bytes", failing), mock.patch.object(ss, "_collect", fake_collect):
            d = asyncio.run(ss.audit())
        self.assertIsNone(d["journal"]["bytes"])
        self.assertIsNone(d["journal"]["bytes_human"])
        self.assertNotIn("journal", _ids(d["findings"]))

    def test_total_excludes_nested_roots(self):
        # 合计是这一页的标题数字：外层根已经覆盖的那一份不能再加一遍。
        self.roots[0]["nested_in"] = "/root/www"
        self._patch()
        d = asyncio.run(ss.audit())
        self.assertEqual(d["counts"]["bytes_total"], 6 * GIB)
        self.assertEqual(d["counts"]["roots"], 2)

    def test_storage_is_not_one_click_fixable(self):
        # 清理动作全是删文件，白名单里出现 storage 就是把 rm 交给一个按钮。
        self.assertNotIn("storage", meta.FIXABLE)


class DoctorItemTests(unittest.TestCase):
    def test_clean_is_pass(self):
        item = ss.doctor_item({"counts": {"roots": 3, "bytes_total": 5 * GIB, "info": 0,
                                          "warn": 0, "risk": 0},
                               "findings": [], "disk": {"ok": True, "used_pct": 42}})
        self.assertEqual(item["status"], "pass")
        self.assertIn("3 个目录", item["detail"])
        self.assertIn("5.0GB", item["detail"])
        self.assertIn("42%", item["detail"])

    def test_info_only_still_pass_but_says_so(self):
        item = ss.doctor_item({"counts": {"roots": 1, "bytes_total": 1, "info": 2, "warn": 0, "risk": 0},
                               "findings": [{"severity": ss.SEV_INFO, "title": "没扫完"}],
                               "disk": {"ok": False}})
        self.assertEqual(item["status"], "pass")
        self.assertIn("只是让你知道", item["detail"])

    def test_risk_or_warn_is_warn_with_fix(self):
        item = ss.doctor_item({"counts": {"roots": 2, "bytes_total": 1, "info": 0, "warn": 2, "risk": 1},
                               "findings": [{"severity": ss.SEV_RISK, "title": "分区已用 93%"},
                                            {"severity": ss.SEV_WARN, "title": "日志太大"},
                                            {"severity": ss.SEV_WARN, "title": "备份太多"}],
                               "disk": {"ok": True, "used_pct": 93}})
        self.assertEqual(item["status"], "warn")
        self.assertEqual(item["fix"], "choyeonctl storage")
        self.assertIn("分区已用 93%", item["detail"])
        self.assertIn("另有 2 项", item["detail"])

    def test_nothing_readable_is_warn(self):
        item = ss.doctor_item({"counts": {"roots": 0}, "findings": [], "disk": {"ok": False}})
        self.assertEqual(item["status"], "warn")
        self.assertIn("一个目录都没读到", item["detail"])

    def test_never_fails_even_on_garbage(self):
        for data in [{}, {"counts": None}, {"counts": {"roots": 1}, "findings": None, "disk": None},
                     {"counts": {"roots": 1, "bytes_total": None}, "findings": [], "disk": {}}]:
            self.assertNotEqual(ss.doctor_item(data)["status"], "fail", data)


class MetaWiringTests(unittest.TestCase):
    def test_storage_is_a_registered_doctor_item(self):
        ids = [c[0] for c in meta._CHECKS]
        self.assertIn("storage", ids)
        self.assertEqual(meta._CHECKS[ids.index("storage")][1], "存储占用")
        self.assertEqual(len(ids), len(set(ids)), "自检项 id 不能重复")

    def test_storage_route_is_registered(self):
        self.assertIn("/api/storage", [r.path for r in meta.router.routes])

    def test_route_defaults_to_cache_and_forwards_refresh(self):
        seen = []

        async def audit(refresh=False):
            seen.append(refresh)
            return {"ok": True}

        with mock.patch.object(meta.storage_service, "audit", audit):
            asyncio.run(meta.storage())
            asyncio.run(meta.storage(refresh=1))
        self.assertEqual(seen, [False, True])

    def test_check_delegates_to_service_doctor_item(self):
        async def audit():
            return {"counts": {"roots": 1, "bytes_total": 1, "info": 0, "warn": 1, "risk": 0},
                    "findings": [{"severity": ss.SEV_WARN, "title": "备份目录累计 6.0GB"}],
                    "disk": {"ok": True, "used_pct": 55}}

        with mock.patch.object(meta.storage_service, "audit", audit):
            item = asyncio.run(meta._check_storage())
        self.assertEqual(item["status"], "warn")
        self.assertIn("备份目录累计", item["detail"])


class CliTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(cli.Out, "json_mode", False)
        p.start()
        self.addCleanup(p.stop)

    def test_parser_accepts_storage_with_refresh(self):
        opts = set()
        for action in cli.build_parser()._subparsers._group_actions[0].choices["storage"]._actions:
            opts |= {o for o in action.option_strings if o.startswith("--") and o != "--help"}
        self.assertIn("--refresh", opts)

    def test_schema_documents_storage(self):
        data, *_ = _run(cli.cmd_schema(_args("schema")))
        entry = next(c for c in data["commands"] if c["name"] == "storage")
        self.assertIn("--refresh", entry["args"])
        self.assertIn("只读", entry["desc"])
        self.assertTrue(any("下界" in n for n in entry["notes"]), entry["notes"])
        self.assertTrue(any("不删文件" in n or "只给可复制" in entry["desc"] for n in entry["notes"]),
                        entry["notes"])

    def test_precondition_when_nothing_readable(self):
        async def audit(refresh=False):
            return {"roots": [{"label": "面板数据", "path": "/d", "exists": False}],
                    "counts": {"roots": 0}, "disk": {"ok": False}, "findings": [],
                    "big_files": [], "notes": [], "journal": {"bytes": None}}

        with mock.patch.object(cli.storage_service, "audit", audit):
            data, _out, err, code = _run(cli.cmd_storage(_args("storage")))
        self.assertIsNone(data)
        self.assertEqual(code, 3)
        self.assertIn("一个都没读到", err)

    def test_json_output_is_the_service_payload(self):
        payload = {"as_of": "2026-10-12T10:00:00+00:00", "cached": False,
                   "disk": {"ok": True, "path": "/d", "total": 1, "total_human": "1B",
                            "free": 1, "free_human": "1B", "used_pct": 10},
                   "journal": {"bytes": None, "bytes_human": None, "raw": "x"},
                   "roots": [{"label": "面板数据", "path": "/d", "exists": True, "bytes": 1,
                              "bytes_human": "1B", "files": 1, "dirs": 0, "errors": 0,
                              "partial": False, "subdirs": []}],
                   "big_files": [], "findings": [],
                   "counts": {"roots": 1, "risk": 0, "warn": 0, "info": 0,
                              "bytes_total": 1, "bytes_total_human": "1B"}, "notes": []}

        async def audit(refresh=False):
            return payload

        with mock.patch.object(cli.storage_service, "audit", audit):
            data, out, _err, code = _run(cli.cmd_storage(_args("storage")))
        self.assertEqual(code, 0)
        self.assertIs(data, payload)
        self.assertIn("1B", out)

    def test_text_renders_the_honest_markers(self):
        d = {
            "as_of": "2026-10-12T10:00:00+00:00",
            "cached": True,
            "disk": {"ok": True, "path": "/data", "total": 40 * GIB, "total_human": "40.0GB",
                     "free": 4 * GIB, "free_human": "4.0GB", "used_pct": 90},
            "journal": {"bytes": 3 * GIB, "bytes_human": "3.0GB", "raw": "x"},
            "roots": [
                {"label": "面板数据", "path": "/data", "exists": True, "bytes": 2 * GIB,
                 "bytes_human": "2.0GB", "files": 3, "dirs": 1, "errors": 0, "partial": False,
                 "branch_count": 1,
                 "subdirs": [{"name": "avatars", "bytes": 1 * GIB, "bytes_human": "1.0GB"}]},
                {"label": "面板备份", "path": "/bak", "exists": True, "bytes": 6 * GIB,
                 "bytes_human": "6.0GB", "files": 30, "dirs": 400, "errors": 2, "partial": True,
                 "branch_count": 40,
                 "subdirs": [{"name": f"d{i}", "bytes": 1, "bytes_human": "1B"}
                             for i in range(ss.TOP_SUBDIRS)],
                 "backups": {"count": 30, "oldest": "2026-01-01", "newest": "2026-10-01"}},
                {"label": "应用与配置目录", "path": "/gone", "exists": False, "bytes": 0,
                 "bytes_human": "0B", "files": 0, "dirs": 0, "errors": 0, "partial": False,
                 "branch_count": 0, "subdirs": [], "note": "路径不存在或不是目录"},
            ],
            "big_files": [{"path": "/bak/dump", "bytes": 3 * GIB, "bytes_human": "3.0GB"}],
            "findings": [{"severity": ss.SEV_RISK, "title": "分区已用 90%，只剩 4.0GB",
                          "detail": "备份与构建缓存在同一块盘", "fix": "choyeonctl storage"}],
            "counts": {"risk": 1, "warn": 1, "info": 1, "bytes_total": 8 * GIB,
                       "bytes_total_human": "8.0GB", "roots": 2},
            "notes": list(ss.NOTES),
        }
        text = cli._storage_text(d)
        self.assertIn("分区（数据目录所在）共 40.0GB", text)
        self.assertIn("没扫完，数字是下界", text)
        self.assertNotIn("（数字是下界））", text)  # 括号套括号，终端上读起来像坏了
        self.assertIn("另有 34 个一级子目录未列出", text)
        self.assertNotIn("另有 394", text)  # 拿全部层级的目录数去减，会把 34 说成 394
        self.assertIn("2 处读不到", text)
        self.assertIn("目录不存在", text)
        self.assertIn("[要紧] 分区已用 90%", text)
        self.assertIn("处置：choyeonctl storage", text)
        self.assertIn("备份产物 30 份", text)
        self.assertIn("（命中缓存）", text)
        self.assertNotIn("rm -rf", text)
        self.assertNotIn("None", text)
        self.assertIn(ss.doctor_item(d)["status"], ("pass", "warn"))


class RouteAccessTests(unittest.TestCase):
    """/api/storage 是只读探测：viewer 能看，匿名不能看。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        async def audit(refresh=False):
            return {"roots": [], "counts": {"roots": 0}, "findings": [], "notes": [],
                    "disk": {"ok": False}, "journal": {"bytes": None}, "big_files": [],
                    "cached": True, "as_of": "x"}

        p = mock.patch.object(meta.storage_service, "audit", audit)
        p.start()
        self.addCleanup(p.stop)
        self.user = "p12b" + uuid.uuid4().hex[:10]
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (self.user, security.hash_pass("Password123"), "viewer"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.headers = {"Authorization": f"Bearer {security.sign_token(self.user, 'viewer')}"}

    def test_viewer_can_read(self):
        r = self.client.get("/api/storage", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("roots", r.json())

    def test_anonymous_denied(self):
        self.assertIn(self.client.get("/api/storage").status_code, (401, 403))


class DocsTests(unittest.TestCase):
    def test_agents_doc_documents_the_command(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("`storage`", text)
        self.assertTrue(f"当前 {len(meta._CHECKS)} 项" in text, "AGENTS.md 的自检项数与代码不一致")

    def test_readme_documents_the_command(self):
        text = (Path(cli.ROOT) / "README.md").read_text(encoding="utf-8")
        self.assertIn("choyeonctl storage", text)


if __name__ == "__main__":
    unittest.main()
