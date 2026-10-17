"""批次㊿C：CI 后端 job 挂死的两条守卫。

① 假 ASGI 传输必须像真服务器：正文给完就阻塞。starlette 1.8.0 起 FileResponse 在
   spec_version<2.4 时会起一个 `while True: await receive()` 的断连监听任务，
   旧桩「用尽后立刻再回一条 http.request」让它空转不死——CI 两套 Python 一起
   卡在「单元测试」25 分钟上限（run 38984972944），本机 starlette 1.7.0 一直是绿的。
② run_tests.py 的单用例看门狗：挂死必须自己把栈打出来，不再靠 step 超时猜。
"""

import asyncio
import os
import pathlib
import shutil
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b50c_data_"))
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent


class FakeTransportBlocksTests(unittest.IsolatedAsyncioTestCase):
    """批次㊿C 的桩形状：正文给完就挂住，不再每次调用立刻回一条新消息。

    这里测的是行为不是写法——断连监听循环要的正是「receive() 会等」，
    而「结尾有没有 return」跟它没有对应关系。
    """

    async def test_终止块只发一次之后调用会等待(self):
        from test_batch5d import Feed

        feed = Feed([b"a", b"b"])
        got = [await feed() for _ in range(3)]  # 两块正文 + 一个终止块
        self.assertEqual([m["type"] for m in got], ["http.request"] * 3)
        self.assertFalse(got[-1]["more_body"], "终止块必须宣告正文结束")
        for _ in range(2):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(feed(), 0.2)


class FileResponseCompletionTests(unittest.IsolatedAsyncioTestCase):
    """真跑一遍静态文件响应：不给断开信号也必须把正文发完并返回。"""

    async def test_静态文件响应自己收尾_not_等一个不会来的断开(self):
        from app.main import DistFiles

        root = tempfile.mkdtemp(prefix="cp_b50c_dist_")
        Path(f"{root}/index.html").write_text("<html>ok</html>")
        app = DistFiles(directory=root, html=True)
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
            "headers": [], "client": ("127.0.0.1", 50000), "server": ("test", 80),
        }
        sent, given = [], False
        gone = asyncio.Event()

        async def receive():
            nonlocal given
            if not given:
                given = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            sent.append(msg)

        # 挂死的形状就是"这条 await 永不返回"，所以这里必须能自己结束。
        await asyncio.wait_for(app(scope, receive, send), 10)
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual(start["status"], 200)
        self.assertTrue(any(m["type"] == "http.response.body" and not m.get("more_body") for m in sent))


class StaleSessionSweepTests(unittest.TestCase):
    """看门狗用 os._exit 退出，atexit 不会跑：遗留目录必须由下一轮自己收。"""

    def test_只收过期目录_还在跑的那轮不能动(self):
        import run_tests

        root = pathlib.Path(tempfile.mkdtemp(prefix="cp_unittest_"))
        fresh = pathlib.Path(tempfile.mkdtemp(prefix="cp_unittest_"))
        (fresh / "inner").mkdir()
        (fresh / "inner" / "f.txt").write_text("x")
        self.addCleanup(shutil.rmtree, fresh, True)
        old_ts = time.time() - 2 * 86_400
        os.utime(root, (old_ts, old_ts))
        removed = run_tests.sweep_stale_sessions()
        self.assertFalse(root.exists(), "超过一天的遗留树必须被收掉")
        self.assertTrue(fresh.is_dir() and (fresh / "inner" / "f.txt").exists(),
                        "新建的会话目录不能被上一轮的扫描误删")
        self.assertGreaterEqual(removed, 1)


class WatchdogWiringTests(unittest.TestCase):
    def test_run_tests_装了单用例看门狗(self):
        src = (BACKEND / "run_tests.py").read_text(encoding="utf-8")
        self.assertIn("import faulthandler", src)
        self.assertIn("faulthandler.dump_traceback_later(HANG_SECONDS, exit=True)", src)
        self.assertIn("faulthandler.cancel_dump_traceback_later()", src)
        self.assertIn("resultclass", src)
        self.assertIn("CP_TEST_HANG_SECONDS", src)
        # 必须在 unittest 入口真的生效，光定义一个类不算
        self.assertIn("testRunner=_Runner", src)


if __name__ == "__main__":
    unittest.main()
