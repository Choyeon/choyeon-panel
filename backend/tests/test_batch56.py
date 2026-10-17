"""批次56A：异地桶里的产物要看得见、也拉得回来。

㊸A/㊸B 把"往外推"这条路修好了，但**往里取**一格都没有：机器重装之后本机备份目录是空的，
管理员站在异地目标前只能看见 `probe` 那句"前缀下有 7 个对象"——不知道是哪 7 份、哪一份
最新，也没有任何入口把其中一份搬回本机。而恢复只读本机的文件，所以"有异地副本"在这个
场景里的实际价值是零：唯一的副本在桶里，界面上却处处暗示它可用。

这一批按危险面排：

1. **拉回的字节流不许进内存，也不许留下半份文件。** `_request` 把响应整段读进来
   （而且只读 `MAX_LIST_BYTES`），拿它下载归档要么撑爆 root 进程，要么把一份几百 MB
   的备份截成"看着完整"的半成品。这里按块落盘，声明大小与实读字节各判一道，
   中途任何失败都删掉临时文件——落在临时名上，所以列表与恢复永远认不到它。
2. **归属判据只有一个出处。** 本机列表、云端列表、拉回校验问的是同一句
   「这个名字属于哪条任务」（`backup_service.name_owner`）。各写一份的结果是
   桶里那份 `bk3-manual-dump.sql` 本机列得出来、云端却被当别人的对象藏起来。
   prune 用的是另一条更窄的判据（runner 的 `auto_pattern`），**这是有意的**，
   所以这里把两边都钉住：手工产物能看能拉，但永远不该被轮替删掉。
3. **拉回来的位置就是恢复的输入。** 名字带目录、属于别的任务、本机已存在同名、
   本机同名是符号链接——四种都在动手之前拒绝；落盘余量走 `assert_write_budget`
   （设置页那条 alert_disk_pct，与上传/解包同一个读者），不写完才发现盘满。
4. **CLI、schema、AGENTS.md 三处同源。** 契约里承诺而 argparse 没实现的子命令，
   智能体照着装上去只会拿到退出码 2。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch56_data_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch56_bak_"))

from app import config  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import backups as RB  # noqa: E402
from app.services import backup_service as B  # noqa: E402
from app.services import cloud_backup as C  # noqa: E402
from app.util import human_bytes  # noqa: E402
from cli import EXIT_USAGE, build_parser, cmd_backup, cmd_schema  # noqa: E402

AK = "AKIAIOSFODNN7EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
ROOT = Path(config.BASE)
_seq = itertools.count(1)
_JOBS: list[int] = []
_TGTS: list[int] = []


def _bak_dir() -> str:
    """本条用例私有的备份目录（见 `_Case`）。

    读的是 `config.BACKUP_DIR` 的当前值而不是本文件 setdefault 的那个路径：整进程只有
    先 import 的那份 env 说了算，写死就等于往别的模块的目录里丢文件。
    """
    return config.BACKUP_DIR


def _touch(name: str, data: bytes = b"x") -> str:
    os.makedirs(_bak_dir(), exist_ok=True)
    p = os.path.join(_bak_dir(), name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def _mkjob(kind: str = "app", **cols) -> int:
    fields = {"kind": kind, "target": f"ut56-{os.getpid()}-{next(_seq)}",
              "schedule": "daily", "enabled": 1, "keep": 7}
    fields.update(cols)
    marks = ", ".join("?" for _ in fields)
    bid = dbm.execute(f"INSERT INTO backups({', '.join(fields)}) VALUES({marks})",
                      tuple(fields.values()))
    _JOBS.append(int(bid))
    return int(bid)


def _mktarget(**kw) -> int:
    body = {"name": f"tg56{os.getpid() % 10000}{next(_seq)}",
            "endpoint": "https://minio.example.com:9000", "bucket": "offsite",
            "region": "us-east-1", "prefix": "panel/", "path_style": True,
            "access_key": AK, "secret_key": SK}
    body.update(kw)
    _TGTS.append(int(C.create_target(body)["id"]))
    return _TGTS[-1]


class _Case(unittest.TestCase):
    """每条用例一个私有备份目录。

    共享目录 + `backups.id` 会被 sqlite 回收复用（tearDown 删掉本模块的行后，下一次 INSERT
    拿回同一个 id）是两件单独都合理、叠起来要命的事：别的模块留在目录里的那份 `bk107-…`
    会把这里同名的拉回判成「本机已有同名产物」，于是一条用例绿不绿取决于别的文件先跑没跑完
    ——批次㊿D 那类「测试继承宿主事实」的病，只是这次继承的是磁盘上的残留。
    """

    def setUp(self):
        self.bak = tempfile.mkdtemp(prefix="cp56_case_bak_")
        # config 与 backup_service 各存一份 BACKUP_DIR（后者是模块级常量），两处都要按住，
        # 不然 list_files 读的还是全进程那份共享目录。
        self._patches = [mock.patch.object(config, "BACKUP_DIR", self.bak),
                         mock.patch.object(B, "BACKUP_DIR", self.bak)]
        for p in self._patches:
            p.start()
        os.makedirs(_bak_dir(), exist_ok=True)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.bak, ignore_errors=True)
        for bid in _JOBS:
            dbm.execute("DELETE FROM backups WHERE id=?", (bid,))
        del _JOBS[:]
        for tid in _TGTS:
            dbm.execute("DELETE FROM backup_pushes WHERE target_id=?", (tid,))
            dbm.execute("DELETE FROM cloud_targets WHERE id=?", (tid,))
        del _TGTS[:]


# ------------------------------------------------ 1. 归属判据只有一个出处

class TestNameOwner(_Case):
    def test_flat_artifact_names_map_to_their_job(self):
        self.assertEqual(B.name_owner("bk1-a.tar.gz"), 1)
        self.assertEqual(B.name_owner("bk12-2026-10-16-03-41-00-app.sqlite.gz"), 12)
        self.assertEqual(B.name_owner("bk3-manual-dump.sql"), 3)

    def test_everything_else_is_not_owned(self):
        for n in ("", None, "panel-2026.tar.gz", "bkx-a.gz", "bk-1.gz", "manual/bk1-a.gz",
                  "bk1-a/b.gz", "/bk1-a.gz", "../bk1-a.gz", "bk1-"):
            if n == "bk1-":
                self.assertEqual(B.name_owner(n), 1, "前缀本身就是判据，尾巴空不算另一种形状")
                continue
            self.assertIsNone(B.name_owner(n), f"{n!r} 不该认成任何人的产物")

    def test_nested_key_is_none_even_when_the_leaf_looks_owned(self):
        """远端的键带目录时必须先剥前缀再判：`panel/bk1-x` 剥完才是 `bk1-x`。
        没剥就判，等于把整键当文件名写进本机目录。"""
        self.assertIsNone(B.name_owner("panel/bk1-x.tar.gz"))

    def test_local_list_uses_the_same_reader(self):
        bid = _mkjob()
        auto = f"bk{bid}-2026-10-16-03-41-00-app.tar.gz"
        manual = f"bk{bid}-manual-dump.sql"
        _touch(auto)
        _touch(manual)
        _touch("unrelated-note.txt")
        names = {f["name"] for f in B.list_files()["files"]}
        self.assertIn(auto, names)
        self.assertIn(manual, names)
        self.assertNotIn("unrelated-note.txt", names)

    def test_cloud_list_owns_manual_artifacts_that_prune_must_not_touch(self):
        """列表/拉回认手工产物，prune 不认——两条判据不同是有意的，这里把两边都钉住。"""
        bid = _mkjob()
        name = f"bk{bid}-manual-dump.sql"
        self.assertEqual(B.name_owner(name), bid)
        self.assertIsNone(C._auto_re(bid).match(name), "手工产物落进 auto_pattern 就会被轮替删掉")


# ------------------------------------------------ 假连接：录制真实发出的 GET

_SENT: list[dict] = []


class _Stream:
    """假响应：read(n) 真的按块给，能验「流式」而不是「一次读完」。"""

    def __init__(self, status=200, chunks=(b"",), headers=None):
        self.status = status
        self._chunks = list(chunks)
        self._headers = headers or {}
        self.reads: list[int] = []

    def read(self, n=-1):
        self.reads.append(n)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def getheaders(self):
        return list(self._headers.items())


class _Conn:
    responder = None

    def __init__(self, host, port=None, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context

    def request(self, method, url, body=None, headers=None):
        _SENT.append({"method": method, "url": url, "body": body,
                      "headers": {k.lower(): str(v) for k, v in (headers or {}).items()}})

    def getresponse(self):
        return type(self).responder(**_SENT[-1])

    def close(self):
        pass


def _tgt(**kw) -> dict:
    t = {"endpoint": "https://minio.example.com:9000", "bucket": "offsite",
         "region": "us-east-1", "prefix": "panel/", "path_style": True,
         "access_key": AK, "secret_key": SK, "name": "wire"}
    t.update(kw)
    return t


class TestGetObject(_Case):
    """`get_object` 是这一批里唯一会写本机备份目录的下载路径。"""

    def setUp(self):
        super().setUp()
        del _SENT[:]
        self.t = _tgt()
        self.dest = os.path.join(_bak_dir(), f"bk56-{os.getpid()}-{next(_seq)}.tar.gz")

    def _get(self, chunks, headers=None, resp_cls=None, **kw):
        made = []

        def responder(**_kw):
            s = (resp_cls or _Stream)(200, chunks, headers or {})
            made.append(s)
            return s

        _Conn.responder = responder
        with mock.patch.object(C, "HTTPSConnection", _Conn):
            out = C.get_object(self.t, "panel/bk56.tar.gz", self.dest, **kw)
        return out, made[0]

    def test_streams_in_chunks_and_lands_every_byte(self):
        body = (b"a" * C.GET_CHUNK) + (b"b" * 1024)
        res, stream = self._get([body[:C.GET_CHUNK], body[C.GET_CHUNK:], b""],
                                {"Content-Length": str(len(body))})
        self.assertEqual(res["bytes"], len(body))
        self.assertEqual(res["declared"], len(body))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), body)
        self.assertIn(C.GET_CHUNK, stream.reads, "必须按块读：read() 不带参数就是整段进内存")

    def test_download_is_a_signed_get_over_the_exact_wire_request(self):
        """签名覆盖的必须是线上那份：方法、路径（含寻址风格）与空载荷哈希三样任何一处
        对不上，对端只会回一句没有信息量的 403。"""
        self._get([b"hello", b""], {"Content-Length": "5"})
        sent = _SENT[-1]
        self.assertEqual(sent["method"], "GET")
        self.assertEqual(sent["url"], "/offsite/panel/bk56.tar.gz", "path-style 下桶名在 path 里")
        self.assertEqual(sent["headers"]["x-amz-content-sha256"], C.EMPTY_SHA256)
        self.assertIsNone(sent["body"], "GET 不带请求体")
        unsigned = {k: v for k, v in sent["headers"].items() if k != "authorization"}
        when = datetime.strptime(sent["headers"]["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        auth, _ = C.authorization(method=sent["method"], path=sent["url"], query="",
                                  headers=unsigned, payload_hash=C.EMPTY_SHA256,
                                  access_key=AK, secret=SK, region=self.t["region"], when=when)
        self.assertEqual(sent["headers"]["authorization"], auth)
        other = C.authorization(method="GET", path="/offsite/other.gz", query="", headers=unsigned,
                                payload_hash=C.EMPTY_SHA256, access_key=AK, secret=SK,
                                region=self.t["region"], when=when)[0]
        self.assertNotEqual(auth, other, "负向对照：签名确实覆盖路径，不是恒等式")

    def test_virtual_host_addressing_moves_the_bucket_into_host(self):
        self.t["path_style"] = False
        self._get([b"x", b""], {"Content-Length": "1"})
        sent = _SENT[-1]
        self.assertEqual(sent["headers"]["host"], "offsite.minio.example.com")
        self.assertEqual(sent["url"], "/panel/bk56.tar.gz")

    def test_declared_size_over_the_limit_refuses_before_the_first_byte(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._get([b"z" * 4096, b""], {"Content-Length": str(C.MAX_SINGLE_GET + 1)}, limit=4096)
        self.assertIn("超过单文件拉回上限", str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest), "拒绝必须发生在写第一个字节之前")

    def test_more_bytes_than_declared_are_refused_and_removed(self):
        """对端把 Content-Length 报小、实际发得更多：实读字节这一道才是真的闸。"""
        with self.assertRaises(RuntimeError) as ctx:
            self._get([b"z" * 5000, b"z" * 5000, b""], {"Content-Length": "10"}, limit=8000)
        self.assertIn("实际已传", str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest), "半成品不能躺在备份目录里等着被恢复")

    def test_truncated_transfer_is_removed(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._get([b"z" * 100, b""], {"Content-Length": "500"})
        self.assertIn("传输被截断", str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_empty_object_is_not_a_backup(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._get([b""], {"Content-Length": "0"})
        self.assertIn("空对象", str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_error_status_is_reported_without_leaving_a_file(self):
        _Conn.responder = lambda **_kw: _Stream(
            403, [b"<Error><Code>AccessDenied</Code></Error>"], {"x-amz-request-id": "R"})
        with mock.patch.object(C, "HTTPSConnection", _Conn):
            with self.assertRaises(RuntimeError):
                C.get_object(self.t, "panel/x", self.dest)
        self.assertFalse(os.path.exists(self.dest))

    def test_timeout_interrupts_and_removes(self):
        """慢对端：超过这一次调用的时限就断开，而不是让 root 进程一直等下去。"""
        body = b"z" * (C.GET_CHUNK + 10)
        ticks = iter([0.0, 0.0, 1e9])
        _Conn.responder = lambda **_kw: _Stream(
            200, [body[:C.GET_CHUNK], body[C.GET_CHUNK:], b""], {"Content-Length": str(len(body))})
        with (mock.patch.object(C, "HTTPSConnection", _Conn),
              mock.patch.object(C.time, "monotonic", lambda: next(ticks))):
            with self.assertRaises(RuntimeError) as ctx:
                C.get_object(self.t, "panel/x", self.dest, timeout=30)
        self.assertIn("已中断", str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_metadata_endpoint_is_refused_before_connect(self):
        t = _tgt(endpoint="http://169.254.169.254")
        conn = mock.Mock()
        with mock.patch.object(C, "HTTPConnection", conn):
            with self.assertRaises(RuntimeError) as ctx:
                C.get_object(t, "panel/x", self.dest)
        self.assertIn("元数据", str(ctx.exception))
        conn.assert_not_called()


# ------------------------------------------------ 2. 云端列表

def _listing(items) -> list[dict]:
    return [{"key": k, "size": s, "modified": m} for k, s, m in items]


class TestListForJob(_Case):
    def setUp(self):
        super().setUp()
        self.bid = _mkjob()
        self.other = _mkjob()
        self.tid = _mktarget()

    def _run(self, rows, truncated=False):
        with (mock.patch.object(C, "get_secret", return_value=_tgt(id=self.tid)),
              mock.patch.object(C, "list_keys", return_value=(rows, truncated))):
            return C.list_for_job(self.tid, self.bid)

    def test_only_this_jobs_own_flat_artifacts_are_listed(self):
        rows = _listing([
            (f"panel/bk{self.bid}-2026-10-16-03-41-00-app.tar.gz", 10, "2026-10-16T03:41:00.000Z"),
            (f"panel/bk{self.bid}-manual-dump.sql", 20, "2026-10-15T03:41:00.000Z"),
            (f"panel/bk{self.other}-2026-10-16-03-41-00-app.tar.gz", 30, "2026-10-16T03:41:00.000Z"),
            ("panel/unrelated/note.txt", 40, "2026-10-16T03:41:00.000Z"),
            (f"panel/bk{self.bid}-nested/x.gz", 50, "2026-10-16T03:41:00.000Z"),
        ])
        res = self._run(rows)
        self.assertEqual([f["name"] for f in res["files"]],
                         [f"bk{self.bid}-2026-10-16-03-41-00-app.tar.gz",
                          f"bk{self.bid}-manual-dump.sql"],
                         "新的在前；别的任务、别人的对象、带目录的键都不算这一份的")
        self.assertEqual(res["remote_total"], 5)
        self.assertFalse(res["truncated"])

    def test_truncated_listing_is_passed_through_not_hidden(self):
        res = self._run(_listing([(f"panel/bk{self.bid}-a.gz", 1, "2026-10-16T03:41:00.000Z")]),
                        truncated=True)
        self.assertTrue(res["truncated"], "没列完必须说出来，否则「桶里就这一份」是假结论")

    def test_local_flag_says_whether_this_machine_still_has_a_copy(self):
        name = f"bk{self.bid}-2026-10-16-03-41-00-app.tar.gz"
        _touch(name)
        only = f"bk{self.bid}-only-remote.sql"
        res = self._run(_listing([(f"panel/{name}", 10, "2026-10-16T03:41:00.000Z"),
                                  (f"panel/{only}", 20, "2026-10-14T00:00:00.000Z")]))
        flags = {f["name"]: f["local"] for f in res["files"]}
        self.assertTrue(flags[name])
        self.assertFalse(flags[only], "重装后「仅异地」这一格就是拉回的动机，判反了等于没有这个功能")

    def test_missing_local_dir_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as gone:
            os.rmdir(gone)  # 目录真的不存在：重装后第一次
            with (mock.patch.object(C, "get_secret", return_value=_tgt(id=self.tid)),
                  mock.patch.object(C, "list_keys", return_value=([], False)),
                  mock.patch.object(C.config, "BACKUP_DIR", gone)):
                res = C.list_for_job(self.tid, self.bid)
        self.assertEqual(res["files"], [])
        self.assertTrue(res["ok"])


# ------------------------------------------------ 3. 拉回

class TestPullOne(_Case):
    def setUp(self):
        super().setUp()
        self.bid = _mkjob()
        self.tid = _mktarget()
        self.name = f"bk{self.bid}-2026-10-16-03-41-00-app.tar.gz"

    def _fake_get(self, body: bytes):
        def side(_t, key, d, **_kw):
            Path(d).write_bytes(body)
            return {"bytes": len(body), "declared": len(body), "key": key, "request_id": "R"}

        return side

    def _pull(self, name=None, *, bid=None, size=5, head=True, body=b"hello", patch_get=True):
        """跑一次 pull_one。

        `patch_get=True` 用假下载（只验落盘与判定），`None` 换成一个干净的 Mock
        并留在 `self.get_mock` 上（验"根本没发起下载"），`False` 表示调用方自己
        已经 patch 过——三种情况都必须只有一层 patch，否则外面那层会被这里盖掉，
        断言变成恒真。
        """
        t = _tgt(id=self.tid)
        if patch_get is True:
            go = mock.patch.object(C, "get_object", side_effect=self._fake_get(body))
        elif patch_get is False:
            go = contextlib.nullcontext()
        else:
            self.get_mock = mock.MagicMock()
            go = mock.patch.object(C, "get_object", self.get_mock)
        with (mock.patch.object(C, "get_secret", return_value=t),
              mock.patch.object(C, "head_object",
                                return_value={"key": f"panel/{name or self.name}", "size": size}
                                if head else None),
              go):
            return C.pull_one(self.tid, bid or self.bid, name or self.name)

    def test_happy_path_lands_the_file_and_reports_it_as_restoreable(self):
        dest = os.path.join(_bak_dir(), self.name)
        res = self._pull()
        self.assertTrue(res["ok"])
        self.assertEqual(res["name"], self.name)
        self.assertEqual(res["bytes"], 5)
        self.assertTrue(res["restoreable"], "app 任务的 .tar.gz 必须报成可以直接恢复")
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"hello")
        self.assertEqual(oct(os.stat(dest).st_mode & 0o777), "0o640")
        self.assertEqual([n for n in os.listdir(_bak_dir()) if ".pull.tmp" in n], [],
                         "临时名不许留在目录里")

    def test_foreign_or_nested_name_is_refused_before_any_request(self):
        for bad in (f"bk{self.bid + 999}-x.tar.gz", "bk0-panel.sqlite.gz", "../bk-e.tc",
                    f"{self.name}/x", "unrelated.tar.gz", f"bk{self.bid}-a/b.tar.gz"):
            with mock.patch.object(C, "get_secret") as gs, mock.patch.object(C, "head_object") as ho:
                with self.assertRaises(RuntimeError, msg=bad):
                    C.pull_one(self.tid, self.bid, bad)
                gs.assert_not_called()
                ho.assert_not_called()

    def test_missing_job_is_refused(self):
        with self.assertRaises(RuntimeError):
            C.pull_one(self.tid, 999_999, self.name)

    def test_missing_remote_object_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._pull(head=False)
        self.assertIn("远端没有这个对象", str(ctx.exception))

    def test_oversize_is_refused_before_writing(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._pull(size=C.MAX_SINGLE_GET + 1, patch_get=None)
        self.assertIn("超过单文件拉回上限", str(ctx.exception))
        self.get_mock.assert_not_called()

    def test_existing_local_file_is_never_overwritten(self):
        _touch(self.name, b"the local one")
        with self.assertRaises(RuntimeError) as ctx:
            self._pull(patch_get=None)
        self.assertIn("本机已有同名产物", str(ctx.exception))
        self.get_mock.assert_not_called()
        with open(os.path.join(_bak_dir(), self.name), "rb") as f:
            self.assertEqual(f.read(), b"the local one", "拒绝之后本机那份必须原封不动")

    def test_symlink_dest_is_refused(self):
        link = os.path.join(_bak_dir(), self.name)
        real = _touch(f"bk{self.bid}-other-target.tar.gz", b"real")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("这台机器不允许创建符号链接")
        with self.assertRaises(RuntimeError) as ctx:
            self._pull(patch_get=None)
        self.assertIn("符号链接", str(ctx.exception))
        self.get_mock.assert_not_called()
        self.assertTrue(os.path.islink(link), "拒绝不能把链接换成真文件")

    def test_disk_headroom_uses_the_same_reader_as_uploads(self):
        from app.services import files_service as FS

        with mock.patch.object(FS, "assert_write_budget",
                               side_effect=RuntimeError("落盘余量不足")) as budget:
            with self.assertRaises(RuntimeError) as ctx:
                self._pull(size=50, patch_get=None)
        self.assertIn("落盘余量不足", str(ctx.exception))
        self.assertEqual(budget.call_args[0][1], 50, "预检要的字节数必须是远端声明的那一份")
        self.get_mock.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(_bak_dir(), self.name)))

    def test_a_crash_midway_leaves_neither_the_target_nor_a_temp(self):
        """真走一遍名字与临时文件的配合：get_object 写了半份再抛错，目录必须回到原样。"""
        made: list[str] = []

        def slow(_t, _k, d, **_kw):
            made.append(d)
            Path(d).write_bytes(b"half a backup")
            raise RuntimeError("对端断了")

        with mock.patch.object(C, "get_object", side_effect=slow):
            with self.assertRaises(RuntimeError):
                self._pull(size=13, patch_get=False)
        self.assertEqual(len(made), 1)
        self.assertTrue(os.path.isabs(made[0]))
        self.assertFalse(os.path.exists(made[0]), "临时文件本身也必须被清掉")
        self.assertFalse(os.path.exists(os.path.join(_bak_dir(), self.name)))
        self.assertEqual([n for n in os.listdir(_bak_dir()) if ".pull.tmp" in n], [])

    def test_restoreable_is_the_same_judgment_as_the_local_list(self):
        """「能不能点恢复」必须由 `_restoreable` 一处判定：sqlite 任务的 .sqlite.gz 能，
        同一任务名下形状不对的那份不能。拉回这里另写一份的话，界面把按钮点亮、
        点下去才报「不支持自动恢复」，等于让人对白忙一次。"""
        bid = _mkjob(kind="sqlite")
        good = f"bk{bid}-2026-10-16-03-41-00-app.sqlite.gz"
        bad = f"bk{bid}-manual.tar.gz"
        self.assertTrue(self._pull(name=good, bid=bid)["restoreable"],
                        "应用 SQLite 库正是走自动恢复的那一类")
        self.assertFalse(self._pull(name=bad, bid=bid)["restoreable"],
                         "后缀与任务类型不符时不能报成可恢复")


# ------------------------------------------------ 4. 路由

def _req(role="admin", query=None, body=None):
    class R:
        def __init__(self):
            self.state = SimpleNamespace(cp_role=role, cp_sub="tester")
            self.query_params = query or {}

        async def json(self):
            return body or {}

    return R()


class TestRoutes(_Case):
    def test_viewer_is_refused(self):
        out = asyncio.run(RB.backups_cloud_files("1", _req(role="viewer", query={"target": "1"})))
        self.assertEqual(out.status_code, 403)
        out = asyncio.run(RB.backups_cloud_pull("1", _req(role="viewer",
                                                          body={"target": 1, "name": "x"})))
        self.assertEqual(out.status_code, 403, "拉回会写本机备份目录，只读账号一律挡住")

    def test_non_digit_ids_are_400_not_500(self):
        self.assertEqual(asyncio.run(
            RB.backups_cloud_files("abc", _req(query={"target": "1"}))).status_code, 400)
        self.assertEqual(asyncio.run(RB.backups_cloud_files("1", _req(query={}))).status_code, 400,
                         "缺 target= 不许拿任务自己关联的那个目标猜")
        self.assertEqual(asyncio.run(
            RB.backups_cloud_files("1", _req(query={"target": "x"}))).status_code, 400)
        self.assertEqual(asyncio.run(
            RB.backups_cloud_pull("1", _req(body={"target": "1"}))).status_code, 400,
            "name 缺失要报错，不能列整个桶更不能猜")

    def test_service_call_gets_the_parsed_ids(self):
        with mock.patch.object(C, "list_for_job", return_value={"ok": True}) as lst:
            out = asyncio.run(RB.backups_cloud_files("7", _req(query={"target": "3"})))
        self.assertEqual(out, {"ok": True})
        self.assertEqual(lst.call_args[0], (3, 7), "查询串里的 target 与路径上的任务 id 不能传反")

    def test_service_failure_is_500_and_the_access_key_stays_out(self):
        with mock.patch.object(C, "list_for_job",
                               side_effect=RuntimeError(f"凭据被拒 AccessKeyId={AK} /root/priv/x")):
            out = asyncio.run(RB.backups_cloud_files("7", _req(query={"target": "1"})))
        self.assertEqual(out.status_code, 500)
        text = json.dumps(json.loads(out.body), ensure_ascii=False)
        self.assertNotIn(AK, text, "Access Key 不能出现在响应里")

    def test_pull_success_is_audited_by_name(self):
        bid = _mkjob()
        name = f"bk{bid}-a.tar.gz"
        ret = {"ok": True, "name": name}
        with (mock.patch.object(C, "pull_one", return_value=ret) as p,
              mock.patch.object(RB.aio_db, "audit") as aud):
            out = asyncio.run(RB.backups_cloud_pull(str(bid),
                                                    _req(body={"target": 2, "name": name})))
        self.assertEqual(out, ret)
        self.assertEqual(p.call_args[0], (2, bid, name))
        self.assertIn(name, aud.call_args[0][2], "审计要留下拉回的是哪一份，只记任务 id 查不到东西")


# ------------------------------------------------ 5. CLI 与契约同源

def _schema_entry() -> dict:
    out = asyncio.run(cmd_schema(argparse.Namespace()))
    return next(c for c in out["commands"] if c["name"].split()[0] == "backup")


class TestCliContract(_Case):
    def test_parser_accepts_the_new_subcommands(self):
        p = build_parser()
        for cmd in ("cloud-list", "cloud-pull"):
            args = p.parse_args(["backup", cmd, "--id", "1", "--target", "2", "--name", "bk1-a.gz"])
            self.assertEqual(args.backup_cmd, cmd)

    def test_schema_subcommands_are_all_implemented(self):
        """契约里不许出现 argparse 没实现的子命令——智能体照契约敲命令。"""
        top = build_parser()
        sub = next(a for a in top._actions if a.dest == "cmd")
        backup_cmd = next(a for a in sub.choices["backup"]._actions if a.dest == "backup_cmd")
        verbs = [v for tok in _schema_entry()["name"].split()[1:] for v in tok.split("|")]
        self.assertIn("backup", set(sub.choices), "schema 里这条命令在 CLI 上根本不存在")
        for verb in verbs:
            self.assertIn(verb, set(backup_cmd.choices), f"schema 承诺了 backup {verb}，argparse 里没有")

    def test_schema_notes_cover_the_new_path(self):
        blob = _schema_entry()["desc"] + " ".join(_schema_entry()["notes"])
        for phrase in ("cloud-list", "cloud-pull", "半份"):
            self.assertIn(phrase, blob, f"契约没说清拉回这条路：缺「{phrase}」")

    def test_agents_md_documents_the_new_subcommands(self):
        row = next(line for line in (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
                   if line.startswith("| `backup run"))
        for token in ("cloud-list", "cloud-pull"):
            self.assertIn(token, row, "AGENTS.md 的 backup 行落后于实现了")

    def test_missing_arguments_are_usage_errors(self):
        cases = [({"backup_cmd": "cloud-list", "id": None, "target": 1, "name": None}, "--id"),
                 ({"backup_cmd": "cloud-list", "id": 1, "target": None, "name": None}, "--target"),
                 ({"backup_cmd": "cloud-pull", "id": 1, "target": 1, "name": ""}, "--name")]
        for argv, hint in cases:
            so, se = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                with self.assertRaises(SystemExit) as ctx:
                    asyncio.run(cmd_backup(argparse.Namespace(**argv)))
            self.assertEqual(ctx.exception.code, EXIT_USAGE, f"{argv} 该给用法错误")
            self.assertIn(hint, se.getvalue(), f"{argv} 的提示没点出缺哪个参数")

    def test_cloud_list_prints_only_this_job(self):
        bid = _mkjob()
        tid = _mktarget()
        files = [{"name": f"bk{bid}-a.tar.gz", "key": f"panel/bk{bid}-a.tar.gz", "size": 1234,
                  "modified": "2026-10-16T03:41:00.000Z", "local": False}]
        with (mock.patch.object(C, "list_for_job",
                                return_value={"ok": True, "target": "t", "bucket": "offsite",
                                              "prefix": "panel/", "job": bid, "files": files,
                                              "remote_total": 2, "truncated": False}),
              mock.patch.object(dbm, "audit"),
              contextlib.redirect_stdout(io.StringIO()) as so):
            res = asyncio.run(cmd_backup(argparse.Namespace(backup_cmd="cloud-list",
                                                            id=bid, target=tid, name=None)))
        self.assertTrue(res["ok"])
        self.assertIn(f"bk{bid}-a.tar.gz", so.getvalue())
        self.assertIn("仅异地", so.getvalue(), "「本机没有」必须直接可读，这是拉回的理由")
        self.assertNotIn("bk9-b", so.getvalue())

    def test_cloud_pull_reports_the_restore_next_step(self):
        bid = _mkjob()
        tid = _mktarget()
        name = f"bk{bid}-a.tar.gz"
        ret = {"ok": True, "name": name, "bytes": 4096, "target": "t", "key": f"panel/{name}",
               "job": bid, "kind": "app", "restoreable": True}
        with (mock.patch.object(C, "pull_one", return_value=ret) as p,
              mock.patch.object(dbm, "audit"),
              contextlib.redirect_stdout(io.StringIO()) as so):
            res = asyncio.run(cmd_backup(argparse.Namespace(backup_cmd="cloud-pull",
                                                            id=bid, target=tid, name=name)))
        self.assertEqual(p.call_args[0], (tid, bid, name))
        self.assertTrue(res["ok"])
        self.assertIn(human_bytes(4096), so.getvalue(), "拉回了多少字节要说得出")
        self.assertIn("restore", so.getvalue(), "拉回之后的下一步就是恢复，判词要说到位")

    def test_failure_exits_and_audits_without_the_secret(self):
        bid = _mkjob()
        tid = _mktarget()
        so, se = io.StringIO(), io.StringIO()
        with (mock.patch.object(C, "pull_one", side_effect=RuntimeError(f"对端拒绝 {AK}")),
              mock.patch.object(dbm, "audit") as aud,
              contextlib.redirect_stdout(so), contextlib.redirect_stderr(se)):
            with self.assertRaises(SystemExit) as ctx:
                asyncio.run(cmd_backup(argparse.Namespace(backup_cmd="cloud-pull",
                                                          id=bid, target=tid, name=f"bk{bid}-a")))
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("cloud-pull", aud.call_args[0][1])
        self.assertNotIn(AK, aud.call_args[0][2], "CLI 审计里也不能出现 Access Key")


if __name__ == "__main__":
    unittest.main()
