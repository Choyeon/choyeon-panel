"""批次⑤B（文件与终端）的回归测试。

这一轮收的是"面板是 root 进程"带来的那类问题：
- 白名单没归一真实路径时，凡是用符号链接写的根都会整页判越界；
- 就地 O_TRUNC 改写让断电/满盘时留下半截配置；
- 指向白名单外的坏符号链接永远删不掉，用户只能进终端手工 rm；
- 压缩包只查名字不查体量，8MB 的包能把盘写满；
- WebSocket 握手失败时用 HTTP 响应回话，前端只看到"连接断开"；
  ?token= 对写操作也放行，长期凭证就原样躺在 nginx access log 里；
  终端没有 Origin 校验，跨站网页可以借已登录管理员的会话接一个 root shell。
"""

import itertools
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_5b_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_5b_roots_"))

from app import config, deps, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import files as files_router  # noqa: E402
from app.routers import terminal as terminal_router  # noqa: E402
from app.services import files_service  # noqa: E402

_seq = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


def _mode(p: str) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# ------------------------------------------------------------------ 白名单归一
class TestRootsAreRealpaths(unittest.TestCase):
    def test_module_roots_are_resolved(self):
        """ROOTS 必须已经是 realpath：否则用软链接写的根会整页判越界。"""
        self.assertEqual(
            files_service.ROOTS,
            [os.path.realpath(r) for r in config.FILE_ROOTS if r],
        )
        for r in files_service.ROOTS:
            self.assertEqual(r, os.path.realpath(r), f"{r} 没有归一成真实路径")

    def test_path_written_through_a_symlinked_root_is_accepted(self):
        real = os.path.realpath(tempfile.mkdtemp(prefix="cp_5b_real_"))
        link = os.path.join(tempfile.gettempdir(), f"cp_5b_link_{os.getpid()}_{next(_seq)}")
        os.symlink(real, link)
        self.addCleanup(lambda: os.path.islink(link) and os.unlink(link))
        self.addCleanup(shutil.rmtree, real, ignore_errors=True)
        old = files_service.ROOTS
        files_service.ROOTS = [os.path.realpath(link)]
        self.addCleanup(setattr, files_service, "ROOTS", old)
        # 用户从配置里拿到的字符串是软链接写法，路径要照样可用
        self.assertEqual(files_service.safe_path(f"{link}/sub/f.txt"), os.path.join(real, "sub/f.txt"))


# ------------------------------------------------------------------ 原子写
class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cp_5b_w_"))
        old = files_service.ROOTS
        files_service.ROOTS = [self.root]
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(setattr, files_service, "ROOTS", old)

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_new_file_is_world_readable_and_leaves_no_temp(self):
        p = self._p("a.txt")
        files_service.write_text(p, "hello")
        self.assertEqual(open(p, "rb").read(), b"hello")
        # root 面板建出来的文件若保持 0600，应用进程下次重启就读不到，
        # 而报错发生在"我只编辑了一个配置"之后
        self.assertEqual(_mode(p), 0o644)
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith(".tmp")], [])

    def test_existing_mode_and_owner_are_carried_over(self):
        p = self._p("b.env")
        Path(p).write_text("OLD")
        os.chmod(p, 0o640)
        files_service.write_text(p, "NEW")
        self.assertEqual(open(p).read(), "NEW")
        self.assertEqual(_mode(p), 0o640, "原子替换不能把原文件权限换成默认值")

    def test_failure_mid_write_keeps_old_content(self):
        """写失败必须留旧内容：就地 O_TRUNC 的写法在这里会留下半截文件。"""
        p = self._p("c.conf")
        Path(p).write_bytes(b"GOOD-CONFIG")
        with mock.patch.object(os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(RuntimeError) as cm:
                files_service.write_text(p, b"x" * 10)
        self.assertIn("写入失败", str(cm.exception))
        self.assertEqual(open(p, "rb").read(), b"GOOD-CONFIG")
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith(".tmp")], [])

    def test_symlink_to_outside_is_refused_and_untouched(self):
        outside = os.path.realpath(tempfile.mkdtemp(prefix="cp_5b_v_"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        victim = os.path.join(outside, "secret")
        Path(victim).write_text("victim")
        link = self._p("link.txt")
        os.symlink(victim, link)
        with self.assertRaises(RuntimeError):
            files_service.write_text(link, "pwn")
        self.assertEqual(Path(victim).read_text(), "victim")
        self.assertTrue(os.path.islink(link), "拒绝写入不该把链接本身换掉")

    def test_internal_symlink_edit_lands_on_the_real_file(self):
        """应用目录里"共享配置软链"是常见布局：编辑 link 要落到真文件上，
        链接本身保持是链接。若换成原子替换直接顶掉链接，其它实例就读不到后续更新了。
        """
        real = self._p("real.txt")
        Path(real).write_text("R")
        link = self._p("link.txt")
        os.symlink("real.txt", link)
        r = files_service.write_text(link, "NEW")
        self.assertEqual(r["path"], real)
        self.assertTrue(os.path.islink(link), "跟随链接写入不该把链接换成普通文件")
        self.assertEqual(Path(real).read_text(), "NEW")

    def test_upload_of_dotfile_lands_verbatim(self):
        """点文件允许上传：部署应用时 .env 本来就要传，拒绝只会逼人改名再改回来。"""
        r = files_service.save_upload(self.root, ".env", b"A=1\n")
        self.assertEqual(r["path"], self._p(".env"))
        self.assertEqual(open(r["path"], "rb").read(), b"A=1\n")


class TestSafeName(unittest.TestCase):
    def test_dotfiles_allowed_but_dot_dirs_rejected(self):
        self.assertEqual(files_service.safe_name("/a/b/.env"), ".env")
        self.assertEqual(files_service.safe_name(".gitignore"), ".gitignore")
        for bad in ("", ".", "..", "   ", "/"):
            with self.assertRaises(RuntimeError):
                files_service.safe_name(bad)


# ------------------------------------------------------------------ 解压体量
class TestExtractLimits(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cp_5b_x_"))
        old = files_service.ROOTS
        files_service.ROOTS = [self.root]
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(setattr, files_service, "ROOTS", old)

    def _zip(self, name, entries):
        path = os.path.join(self.root, name)
        with zipfile.ZipFile(path, "w") as z:
            for arc, data in entries:
                z.writestr(arc, data)
        return path

    def test_ordinary_archive_still_extracts(self):
        src = self._zip("ok.zip", [("a.txt", b"A"), ("sub/b.txt", b"B")])
        r = files_service.extract_archive(src)
        self.assertEqual(r["files"], 2)

    def test_declared_bomb_is_rejected_before_any_write(self):
        src = self._zip("big.zip", [("a.txt", b"x" * 4096)])
        dest = os.path.join(self.root, "out")
        with mock.patch.object(config, "MAX_EXTRACT_BYTES", 1024), self.assertRaises(RuntimeError) as cm:
            files_service.extract_archive(src, dest)
        self.assertIn("总大小超过上限", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(dest, "a.txt")), "拒绝解压不该留下产物")

    def test_member_count_cap(self):
        src = self._zip("many.zip", [(f"m{i}.txt", b"x") for i in range(6)])
        with mock.patch.object(config, "MAX_EXTRACT_FILES", 3), self.assertRaises(RuntimeError) as cm:
            files_service.extract_archive(src)
        self.assertIn("条目数", str(cm.exception))


# ------------------------------------------------------------------ 删除/改名
class TestEntryLevelActions(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cp_5b_a_"))
        old = files_service.ROOTS
        files_service.ROOTS = [self.root]
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(setattr, files_service, "ROOTS", old)

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_bad_symlink_is_deletable(self):
        outside = os.path.realpath(tempfile.mkdtemp(prefix="cp_5b_o_"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        secret = os.path.join(outside, "secret.txt")
        Path(secret).write_text("no")
        link = self._p("link.txt")
        os.symlink(secret, link)
        # 旧实现按 safe_path 判：链接指向白名单外 → 永远删不掉，只能进终端手工 rm
        self.assertEqual(files_service.fs_action("delete", link), {"ok": True})
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(Path(secret).read_text(), "no", "删链接不该动到指向的真文件")

    def test_dangling_rename_target_is_not_clobbered(self):
        src = self._p("a.txt")
        Path(src).write_text("body")
        dangling = self._p("b.txt")
        os.symlink(self._p("nowhere"), dangling)
        with self.assertRaises(RuntimeError) as cm:
            files_service.fs_action("rename", src, dangling)
        self.assertIn("目标已存在", str(cm.exception))
        self.assertTrue(os.path.exists(src), "被拒绝的改名不该把原文件移走")

    def test_rename_outside_roots_rejected(self):
        src = self._p("c.txt")
        Path(src).write_text("x")
        with self.assertRaises(RuntimeError) as cm:
            files_service.fs_action("rename", src, "/etc/cp-5b.txt")
        self.assertIn("超出允许范围", str(cm.exception))
        self.assertTrue(os.path.exists(src))

    def test_root_directory_still_protected(self):
        for action, args in (("delete", (self.root,)), ("rename", (self.root, self._p("other")))):
            with self.assertRaises(RuntimeError) as cm:
                files_service.fs_action(action, *args)
            self.assertIn("根目录", str(cm.exception))


# ------------------------------------------------------------------ 终端积压
class TestTerminalBacklog(unittest.TestCase):
    def test_watermark_and_drain(self):
        b = terminal_router._Backlog(10_000)
        self.assertFalse(b.add(4096))
        self.assertFalse(b.add(4096))
        b.drain(4096)  # 消费过的量要从积压里扣掉，否则跑久了必然误杀
        self.assertFalse(b.add(4096))
        self.assertTrue(b.add(4096))
        self.assertGreater(b.pending, b.cap)

    def test_eof_frame_never_trips_the_watermark(self):
        """进程退出那帧是空字节：它必须能进入队列收尾，不能被当成积压。"""
        b = terminal_router._Backlog(1)
        self.assertTrue(b.add(9))
        self.assertFalse(b.add(0))

    def test_default_cap_is_bounded(self):
        self.assertLessEqual(config.TERMINAL_OUT_BUFFER, 64 * 1024 * 1024)


# ------------------------------------------------------------------ 上传路由
class TestUploadRouteGuards(unittest.IsolatedAsyncioTestCase):
    async def test_read_capped_stops_at_limit(self):
        class Field:
            def __init__(self, blob: bytes):
                self.blob, self.at = blob, 0

            async def read(self, size: int):
                chunk = self.blob[self.at:self.at + size]
                self.at += len(chunk)
                return chunk

        f = Field(b"x" * 700)
        self.assertEqual(await files_router._read_capped(f, 1000), b"x" * 700)
        self.assertIsNone(await files_router._read_capped(Field(b"y" * 700), 500))

    async def test_declared_length_over_limit_returns_413_before_parsing(self):
        big = str(config.MAX_UPLOAD_BYTES + 5 * 1024 * 1024)
        req = SimpleNamespace(
            state=SimpleNamespace(cp_role="admin", cp_sub="t"),
            headers={"content-length": big},
            query_params={},
        )
        with mock.patch.object(files_router.aio_db, "audit"):
            r = await files_router.files_upload(req)
        self.assertEqual(r.status_code, 413)

    async def test_put_streams_and_bails_on_real_bytes(self):
        """Content-Length 是客户端自己写的，谎报时也得按真实收到的字节数拦下。"""
        sent = []

        class Req:
            state = SimpleNamespace(cp_role="admin", cp_sub="t")
            headers = {"content-length": "10"}
            query_params = {"path": "/x"}

            async def stream(self):
                for _ in range(200):
                    chunk = b"a" * 65536
                    sent.append(len(chunk))
                    yield chunk

        with mock.patch.object(files_router, "run_in_threadpool") as pool:
            r = await files_router.files_write(Req())
        self.assertEqual(r.status_code, 413)
        pool.assert_not_called()  # 超限的正文不该被交给写盘
        cap_chunks = config.MAX_UPLOAD_BYTES // 65536 + 1
        self.assertLessEqual(len(sent), cap_chunks, "读到超限就停手，剩下的字节交给中间件收尾（见批次⑤E）")


# ------------------------------------------------------------------ WS 鉴权
def _ws_scope(query: bytes = b"", origin: str | None = None) -> dict:
    headers = [(b"host", b"panel.example.com")]
    if origin:
        headers.append((b"origin", origin.encode()))
    return {
        "type": "websocket",
        "path": "/api/terminal",
        "raw_path": b"/api/terminal",
        "query_string": query,
        "headers": headers,
        "client": ("1.2.3.4", 5),
        "server": ("panel.example.com", 443),
        "scheme": "wss",
        "subprotocols": [],
        "asgi": {"version": "3.0"},
        "state": {},
    }


async def _run_ws(scope):
    """跑一遍中间件，返回 (是否到达业务层, 发出的消息)。"""
    out: list[dict] = []
    reached = []

    async def app(scope, receive, send):
        reached.append(scope.get("state"))
        await send({"type": "websocket.accept"})

    async def receive():
        return {"type": "websocket.connect"}

    async def send(msg):
        out.append(msg)

    await deps.AuthMiddleware(app)(scope, receive, send)
    return bool(reached), out


class TestWebsocketHandshake(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_closes_with_a_code_not_an_http_body(self):
        """旧实现在 websocket 通道上发 http.response.start：uvicorn 当协议错误，
        浏览器只看到"连接失败"，401 文案根本到不了前端。
        另外 close 前必须 accept：不 accept 的话 uvicorn 回 HTTP 403，
        浏览器侧只会收到 code=1006，几种原因又糊成一句话。
        """
        reached, out = await _run_ws(_ws_scope())
        self.assertFalse(reached)
        self.assertEqual([m["type"] for m in out], ["websocket.accept", "websocket.close"])
        self.assertEqual(out[1]["code"], 4401)

    async def test_expired_token_is_4401_not_4403(self):
        """过期会话该说"重新登录"。旧写法让握手继续、由终端路由按角色关成 4403，
        前端报成"无终端权限"，用户反复点重连也连不上。"""
        reached, out = await _run_ws(_ws_scope(b"token=not-a-jwt"))
        self.assertFalse(reached)
        self.assertEqual(out[1]["code"], 4401)

    async def test_cross_origin_handshake_is_refused(self):
        """面板的终端是 root shell：跨站网页带着已登录管理员的凭证握手就能接管终端。

        Origin 检查必须在验签之前——恶意页面完全可能带上一个真 token。
        """
        reached, out = await _run_ws(_ws_scope(b"token=garbage", origin="http://evil.example"))
        self.assertFalse(reached)
        self.assertEqual(out[1]["code"], 4498)

    async def test_same_origin_with_valid_token_reaches_route(self):
        user = _uid("wsa")
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        token = security.sign_token(user, "admin")
        reached, out = await _run_ws(_ws_scope(b"token=" + token.encode(),
                                               origin="https://panel.example.com"))
        self.assertTrue(reached, out)
        self.assertEqual(out[0]["type"], "websocket.accept")

    async def test_http_get_may_still_use_query_token(self):
        """下载/日志流这类浏览器带不了 Header 的 GET 是 ?token= 的唯一合法用途。"""
        from fastapi.testclient import TestClient

        from app import main

        user = _uid("hta")
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        token = security.sign_token(user, "admin")
        client = TestClient(main.app)
        r = client.get(f"/api/files?path=&token={token}")
        self.assertNotEqual(r.status_code, 401)

    async def test_http_mutation_must_not_authenticate_via_url(self):
        """写操作认 URL 里的 token，等于把长期凭证写进 nginx access log、
        浏览器历史和代理日志——那些地方的留存策略没人替面板负责。"""
        from fastapi.testclient import TestClient

        from app import main

        user = _uid("htp")
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        token = security.sign_token(user, "admin")
        client = TestClient(main.app)
        probe_dir = os.path.join(os.path.realpath(config.FILE_ROOTS[0]), ".cp-5b-probe-dir")
        self.addCleanup(lambda: os.path.isdir(probe_dir) and os.rmdir(probe_dir))
        r = client.post(f"/api/files/action?token={token}",
                        json={"action": "mkdir", "path": probe_dir})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(os.path.exists(probe_dir), "URL token 不该完成这次写操作")
        # 同一个请求换成 Authorization 头就正常放行（证明不是接口本身坏了）
        r2 = client.post("/api/files/action", json={"action": "mkdir", "path": probe_dir},
                         headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r2.status_code, 200, r2.text)


if __name__ == "__main__":
    unittest.main()
