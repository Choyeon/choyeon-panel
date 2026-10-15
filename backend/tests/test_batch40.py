"""批次㊵：部署这一趟里"要写多少只有上游知道"的三步，接上同一个落盘读者。

㉜/㊲ 把「这块盘按管理员自己划的警戒线还能写多少」收成唯一读者 `files_service.write_budget()`，
㊳ 又把备份与恢复的落盘字节接到它上面。那些入口有一个共同点：**字节数事先知道**（声明值、
gzip ISIZE、扫描估算）。部署不一样——仓库多大由 `git` 说了算，`npm install` 装多大由
package.json 说了算，`pip install` 由依赖树说了算。改完之后 `apps_service.py` 里这类写入
仍然一个判据都没有，而面板是以 root 跑这些命令的：一块被 clone + node_modules 顶穿的盘，
第一个写不进去的往往是面板自己的 SQLite，后果不是"这次部署失败"，是这台机器上所有业务
一起停摆。所以这一步用同一读者的另一种判法：**没有声明值可比，就判余量本身还在不在**
（`_assert_disk_room`，闸口 = 在警戒线以下且不少于 `_MIN_DEPLOY_ROOM`）。

第二件事是这一趟留下的现场。clone 中途失败（网络断、超时、盘满）会留下一个"git 建了目录、
写了一半对象"的目录，而它让**每一次**后续部署都撞在同一句上：`git clone` 拒绝"目标已存在
且非空"（那个拒绝是对的，见 `_ensure_app_dir` 的注释——不覆盖别人的工作区）。于是面板上
表现为"这个应用永远部署不上，报错还看不出根因"，只有手工 `rm -rf` 才能继续。修法是让
失败收尾自己收：目录是这次建的、代码又没拉完 → 删掉，下次从空目录重来。两个条件缺一不可，
`_discard_partial_checkout` 把判据做成了一个必传参数而不是两条 `except` 各自记得，
因为漏一处就等于给"别人的目录"装了删除路径。

`backup_service.save_upload` 是同一批里剩下的最后一处就地改写：旧写法 `O_TRUNC` 直接开目标
文件，一次中途失败（盘满 / 进程被取消）会把**上一份完好的备份产物**变成半份，而半份文件名
照样带 `bk<id>-` 前缀，列表列得出来、恢复按钮点得动——用户是在"这里有一份好备份"的认知下
点恢复的。改成同目录临时文件 + `os.replace`（要么旧的一份要么新的一份），符号链接目标改为
显式拒绝（旧行为靠 `O_NOFOLLOW` 抛 ELOOP 兑现，换成 replace 之后必须自己挡，否则 replace
会把链接换成一个真文件，等于替用户把"这份备份在别处"的线索抹掉）。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch40_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch40_roots_"))

from app import config
from app.services import alerts_service, files_service
from app.services import apps_service as A
from app.services import backup_service as B

BACKEND = Path(__file__).resolve().parents[1]

# 假分区：100 GiB、警戒线 80%，于是 room = 80GiB - 已用。
# 想要 room=want 就令 avail = want + 20GiB（已用 = 100GiB - avail = 80GiB - want）。
_TOTAL = 100 * 1024**3
_FRSIZE = 4096
_GIB = 1024**3
_MIB = 1024**2


class _Vfs:
    f_frsize = _FRSIZE
    f_blocks = _TOTAL // _FRSIZE

    def __init__(self, avail: int):
        self.f_bavail = avail // _FRSIZE


def _body(rel: str, header: str) -> str:
    src = (BACKEND / rel).read_text(encoding="utf-8")
    start = src.index(header) + len(header)
    rest = src[start:]
    m = re.search(r"\n(?=(async\s+def |def |class ))", rest)
    return rest[: m.start()] if m else rest


def _pin_room(case: unittest.TestCase, want: int) -> None:
    """把 `write_budget()` 的读数钉成想要的余量（不依赖测试跑在哪块盘上）。

    注册 cleanup 而不是裸 start()：这两个补丁打在真的 `os.statvfs` 上，漏停一次会把假分区
    留给后面整个测试会话。
    """
    avail = want + (_TOTAL - _TOTAL * 80 // 100)
    line = mock.patch.object(alerts_service, "disk_thresholds", lambda: {"warn": 80, "crit": 90})
    vfs = mock.patch.object(files_service.os, "statvfs", lambda *_a: _Vfs(avail))
    line.start()
    vfs.start()
    case.addCleanup(vfs.stop)
    case.addCleanup(line.stop)


class DiskRoomGate(unittest.TestCase):
    """余量闸口：判的是"还能不能写"，不是"声明值够不够小"。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp_b40_room_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_余量不足时拒绝且报错能自解释(self):
        _pin_room(self, 8 * _MIB)  # 只剩 8MiB
        with self.assertRaises(RuntimeError) as ctx:
            A._assert_disk_room(self.d, "克隆这个仓库")
        msg = str(ctx.exception)
        self.assertIn("克隆这个仓库", msg)
        self.assertIn("警戒线 80%", msg)
        self.assertIn("未写入任何文件", msg)
        # 修复方向要写在同一句里：这类失败唯一的出路是清理或调阈值，不能只说"不行"。
        self.assertIn("清理这块盘", msg)
        self.assertIn("设置页", msg)

    def test_余量充足时放行并交出同一个读数(self):
        _pin_room(self, 30 * _GIB)
        b = A._assert_disk_room(self.d, "安装依赖")
        self.assertEqual(b["line"], 80)
        self.assertGreater(b["room"], A._MIN_DEPLOY_ROOM)

    def test_反证_下限就是那条线(self):
        # 界要钉在 _MIN_DEPLOY_ROOM 本身：差一个字节就拒，说明这条线真的是判据。
        _pin_room(self, A._MIN_DEPLOY_ROOM - 1)
        with self.assertRaises(RuntimeError):
            A._assert_disk_room(self.d, "安装依赖")
        _pin_room(self, A._MIN_DEPLOY_ROOM)
        self.assertEqual(
            A._assert_disk_room(self.d, "安装依赖")["room"],
            A._MIN_DEPLOY_ROOM,
            "刚好够下限要放行：判成 room < line 会把正当部署一起挡掉",
        )

    def test_口径只有_write_budget_一处(self):
        # 这批新增的三只函数都不许自己调 statvfs：线和读数只有一个出处（批次㉜的约定）。
        # 判的是"有没有第二次去读盘"，注释里提到 statvfs 是解释为什么要往上退目录，不算。
        for fn in ("_assert_disk_room", "_budget_probe_dir", "_discard_partial_checkout"):
            body = _body("app/services/apps_service.py", f"def {fn}(")
            self.assertNotIn("statvfs(", body, f"{fn} 自己读了 statvfs，警戒线就有了第二个出处")
        self.assertIn("write_budget(dest)", _body("app/services/apps_service.py", "def _assert_disk_room("))

    def test_clone_目标还不存在时往上问分区(self):
        missing = os.path.join(self.d, "no-such-app", "sub")
        self.assertEqual(A._budget_probe_dir(missing), self.d, "要退到最近的真目录，否则 statvfs 只会报错")
        self.assertEqual(A._budget_probe_dir(self.d), self.d)
        rootish = A._budget_probe_dir("/definitely/not/here/at/all")
        self.assertTrue(os.path.isdir(rootish), "一路退到根也要停住，不能返回一个不存在的路径")


class DeployBodyWiring(unittest.TestCase):
    """闸必须排在写盘动作之前；排在之后就只是把失败说得更晚一点。"""

    def setUp(self):
        self.body = _body("app/services/apps_service.py", "async def _deployment_body(")

    def _before(self, gate: str, action: str) -> None:
        self.assertIn(gate, self.body, f"部署正文里找不到这道闸：{gate}")
        self.assertLess(
            self.body.index(gate),
            self.body.index(action),
            f"{gate} 排在 {action} 之后，等于让它在已经写盘之后才投诉",
        )

    def test_三步写入各有一道闸且都在动作之前(self):
        self._before('_assert_disk_room(repo_dir, "拉取这个仓库的更新")', '"git", ["fetch"')
        # clone 那一次调用是折行写的，动作的锚点取参数本身而不是 run( 那一行。
        self._before('_assert_disk_room(_budget_probe_dir(app["path"]), "克隆这个仓库")', '"clone", "--depth"')
        self._before('_assert_disk_room(app["path"], "安装依赖")', '"-lc", install')

    def test_反证_没有仓库的那一支不设闸(self):
        # 「跳过拉取」不写任何由外部决定的字节量，加闸只会多一次 statvfs 和一个假拒因。
        self.assertEqual(self.body.count("_assert_disk_room("), 3, "闸的数目就是三道，别再多也别更少")
        i = self.body.index("跳过拉取（未配置仓库")
        seg = self.body[self.body.rindex("else:", 0, i) : i + 60]
        self.assertNotIn("_assert_disk_room", seg, "未配置仓库这一支不该出现闸")

    def test_放行时把当时的余量写进日志(self):
        # 用户在部署日志里要看得见"当时还剩多少"，否则事后分不清是盘的问题还是仓库的问题。
        self.assertIn("分区按警戒线", self.body)
        self.assertIn("human_bytes(_budget['room'])", self.body)

    def test_目录归属判据在任何写盘之前取(self):
        first = self.body.index('path_existed = os.path.isdir(app["path"])')
        self.assertLess(first, self.body.index("repo_dir = await resolve_repo_dir"))
        # 拉完之后必须翻成"这份代码值得留下"，否则装依赖失败会把整份 clone 一起删掉。
        # 取最后一次出现：函数开头那个保守初值也叫 `pulled = True`，不是这里要断的东西。
        self.assertLess(self.body.index("pulled = False"), self.body.rindex("pulled = True"))
        self.assertLess(self.body.rindex("pulled = True"), self.body.index('"-lc", install'))

    def test_两条失败出口都收尾(self):
        self.assertEqual(self.body.count("_discard_partial_checkout("), 2, "取消与异常两支各一次")
        cancel = self.body.index("except asyncio.CancelledError")
        generic = self.body.index("except Exception as e")
        self.assertIn("_discard_partial_checkout(", self.body[cancel:generic], "被取消那支没收尾")
        self.assertIn("_discard_partial_checkout(", self.body[generic:], "失败那支没收尾")


class DiscardPartialCheckout(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp_b40_discard_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.logs: list[str] = []
        p = mock.patch.object(A, "_append_log", lambda _dep, text: self.logs.append(text))
        p.start()
        self.addCleanup(p.stop)

    def _app_dir(self) -> str:
        path = os.path.join(self.d, "probe-app")
        os.makedirs(os.path.join(path, ".git"))
        Path(path, "half-written").write_text("x", encoding="utf-8")
        return path

    def test_这次建的又没拉完_删掉(self):
        path = self._app_dir()
        A._discard_partial_checkout(path, 1, False)
        self.assertFalse(os.path.exists(path), "半份 clone 留着会让下一次部署撞在「目录已存在且非空」上")
        self.assertTrue(any("下次部署从空目录重新开始" in line for line in self.logs), "删了要在日志里说一声")

    def test_目录事先存在_一个字都不动(self):
        path = self._app_dir()
        A._discard_partial_checkout(path, 1, True)
        self.assertTrue(os.path.exists(path), "事先存在的目录可能是别人的工作区")

    def test_代码已拉全_失败也留着(self):
        path = self._app_dir()
        # pulled=True 传成 preserve：装依赖/重启失败时，盘上是一份完整代码，
        # 删掉它等于把用户已经等了几分钟的 clone 一起扔掉，下一次还要从头拉。
        A._discard_partial_checkout(path, 1, True)
        self.assertTrue(Path(path, "half-written").exists())

    def test_要害目录即使判据放行也不删(self):
        inside = os.path.join(str(config.DATA_DIR), "cp-b40-fake-app")
        os.makedirs(inside, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(inside, ignore_errors=True))
        A._discard_partial_checkout(inside, 1, False)  # 不抛错，只是不动它
        self.assertTrue(os.path.isdir(inside), "_check_not_protected 必须拦得住这里的 rmtree")

    def test_反证_目录本来就不存在时静默通过(self):
        A._discard_partial_checkout(os.path.join(self.d, "never-created"), 1, False)
        self.assertEqual(self.logs, [], "什么都没删成，就不该留一句已删除的日志")


class BackupUploadIsAtomic(unittest.TestCase):
    """上传备份产物：要么旧的一份，要么新的一份，不留半份。"""

    def setUp(self):
        _pin_room(self, 30 * _GIB)
        self.d = tempfile.mkdtemp(prefix="cp_b40_upload_")
        self.addCleanup(shutil.rmtree, self.d, True)
        p = mock.patch.object(B, "BACKUP_DIR", self.d)
        p.start()
        self.addCleanup(p.stop)
        self.bid = 4242
        q = mock.patch.object(
            B.dbm,
            "query_one",
            lambda *_a: {"id": self.bid, "kind": "pg", "target": "appdb"},
        )
        q.start()
        self.addCleanup(q.stop)
        self.name = f"bk{self.bid}-manual-a.dump.gz"

    def _names(self) -> list[str]:
        return sorted(os.listdir(self.d))

    def _read(self, p: str) -> bytes:
        with open(p, "rb") as f:
            return f.read()

    def test_写一半失败时旧的一份完好_且不留临时文件(self):
        B.save_upload(self.bid, "a.dump.gz", b"GOOD")
        boom = OSError(28, "No space left on device")
        with mock.patch.object(B.os, "fsync", side_effect=boom), self.assertRaises(RuntimeError):
            B.save_upload(self.bid, "a.dump.gz", b"B" * 4096, overwrite=True)
        self.assertEqual(self._read(os.path.join(self.d, self.name)), b"GOOD", "旧写法的 O_TRUNC 会先把这份清掉")
        self.assertEqual(self._names(), [self.name], "失败不能在目录里留下 .tmp——它长得像一份备份")

    def test_余量不足时在动手之前就拒(self):
        B.save_upload(self.bid, "a.dump.gz", b"GOOD")
        _pin_room(self, 1024)  # 只剩 1KiB
        with self.assertRaises(RuntimeError) as ctx:
            B.save_upload(self.bid, "a.dump.gz", b"x" * 4096, overwrite=True)
        self.assertIn("上传这份备份产物", str(ctx.exception))
        self.assertEqual(self._read(os.path.join(self.d, self.name)), b"GOOD")
        self.assertEqual(self._names(), [self.name])

    def test_符号链接目标一律拒绝(self):
        outside = os.path.join(self.d, "victim")
        Path(outside).write_text("victim", encoding="utf-8")
        os.symlink(outside, os.path.join(self.d, self.name))
        with self.assertRaises(RuntimeError) as ctx:
            B.save_upload(self.bid, "a.dump.gz", b"pwn", overwrite=True)
        self.assertIn("符号链接", str(ctx.exception))
        self.assertEqual(Path(outside).read_text(encoding="utf-8"), "victim", "replace 会把链接换成真文件，必须提前拒")
        self.assertTrue(os.path.islink(os.path.join(self.d, self.name)))

    def test_成功时字节精确且权限收紧(self):
        r = B.save_upload(self.bid, "a.dump.gz", b"\0" * 2048)
        self.assertEqual(r["size"], 2048)
        p = os.path.join(self.d, r["name"])
        self.assertEqual(self._read(p), b"\0" * 2048)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640, "产物含库内容，不能按 umask 建成世界可读")
        self.assertEqual(self._names(), [r["name"]])


class StaticContracts(unittest.TestCase):
    def test_上传产物走的是临时文件加替换(self):
        src = _body("app/services/backup_service.py", "def save_upload(")
        self.assertIn("tempfile.mkstemp(dir=BACKUP_DIR", src)
        self.assertIn("os.replace(tmp, dest)", src)
        self.assertNotIn("os.O_TRUNC", src, "就地截断的写法不许回来：失败会把上一份好备份变成半份")
        self.assertLess(src.index("assert_write_budget(BACKUP_DIR"), src.index("tempfile.mkstemp"))

    def test_部署不再自己算磁盘(self):
        src = (BACKEND / "app" / "services" / "apps_service.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("statvfs("), 0, "余量口径只有 files_service.write_budget() 一处")
        self.assertIn("from .files_service import write_budget", src)

    def test_反证_下限是个真实的下限(self):
        # 下限写成 0 就等于没有：半路的 ENOSPC 会留下半份 node_modules。
        self.assertEqual(A._MIN_DEPLOY_ROOM, 64 * _MIB)


if __name__ == "__main__":
    unittest.main()
