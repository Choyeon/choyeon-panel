"""批次㉑：日志「一次看多少行」要算出来，动态输入行的按钮要有名字。

一、档位副本。⑲B 把 CLI / 白名单 / HTTP 三个上限收进了 `app/util.py`，网页那份还留着：
日志中心同时写着 200/500/1000/2000 四档、默认 500，还把 nginx 尾读的字节上限抄成
「512KB」。三个数没有一个是页面自己算的，而默认 500 和 `LOG_LINES_DEFAULT`（200）早就
对不上了。失真还是安静的那种：下拉里那一档照样能选，服务端已经按自己的上限截断，
用户以为自己要了 2000 行，实际拿到 2000 行只因为它恰好等于上限。

现在档位由 `log_ladder()` 从默认值与上限派生，经 `/api/system/log-options` 下发，
页面拿到清单才回填。今天派生结果仍是 200/500/1000/2000——这批只换出处，不改用户
看得见的那几档。

线上走查这一页时又撞到同类的三份副本：实时日志组件（面板日志 / 服务日志 / 应用日志都用它）
把初值 200 和上限 2000 抄在输入框上，`api.ts` 的 `logUrl()` 再把默认值 200 写成第三处，
SSE 路由里还藏着第二个 `min(lines, 2000)`。上限一改，这四处的话就各不相同。现在组件读
同一份清单（和日志中心同一个缓存键，两个页面共用一次请求），`logUrl()` 不传 lines 就走
服务端默认，路由那处兜底改用同一个常量。

二、可达性。`NDynamicInput` 每行默认给两块无名圆形图标按钮（删除 / 新增）。读屏只念得出
「按钮」，键盘用户更分不清哪块会删掉这一行。环境变量编辑是直接写文件的动作，按错一块
就丢一行配置，所以这一格由我们接管，两颗按钮各带 aria-label。
"""

from __future__ import annotations

import os
import re
import tempfile

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_21_"))

import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app.services import web_logs  # noqa: E402
from app.util import HTTP_LOG_LINES_MAX, LOG_LINES_DEFAULT, log_ladder, parse_lines  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LOG_SRC = (ROOT / "web/src/views/LogCenter.vue").read_text(encoding="utf-8")
APP_SRC = (ROOT / "web/src/views/AppDetail.vue").read_text(encoding="utf-8")
STREAM_SRC = (ROOT / "web/src/components/LogStream.vue").read_text(encoding="utf-8")
API_SRC = (ROOT / "web/src/api.ts").read_text(encoding="utf-8")
ROUTER_SRC = (ROOT / "backend/app/routers/system.py").read_text(encoding="utf-8")


def code_of(sfc: str) -> str:
    """剥掉注释后的 SFC：注释里可以叙述刚被删掉的那份副本，代码和模板里不行。"""
    no_html = re.sub(r"<!--.*?-->", "", sfc, flags=re.S)
    return re.sub(r"^[ \t]*//.*$", "", no_html, flags=re.M)


class TestLadderIsDerived(unittest.TestCase):
    """档位是算出来的，不是换了个地方抄的一组常数。"""

    def test_sorted_unique_and_in_range(self):
        ladder = log_ladder()
        self.assertEqual(ladder, sorted(set(ladder)))
        self.assertTrue(all(1 <= n <= HTTP_LOG_LINES_MAX for n in ladder), ladder)

    def test_covers_default_and_ceiling(self):
        ladder = log_ladder()
        self.assertIn(HTTP_LOG_LINES_MAX, ladder, "上限本身必须可选，否则用户永远够不到")
        self.assertIn(LOG_LINES_DEFAULT, ladder, "默认值必须能在下拉里被选中并回显")
        self.assertEqual(parse_lines(str(ladder[-1])), ladder[-1])

    def test_recomputes_when_the_constants_move(self):
        with (mock.patch("app.util.LOG_LINES_DEFAULT", 300),
              mock.patch("app.util.HTTP_LOG_LINES_MAX", 1200)):
            self.assertEqual(log_ladder(), [300, 600, 1200])

    def test_default_above_ceiling_is_dropped_not_offered(self):
        """默认值大于上限时，不能给一个服务端一定会截断的档位。"""
        with (mock.patch("app.util.LOG_LINES_DEFAULT", 3000),
              mock.patch("app.util.HTTP_LOG_LINES_MAX", 2000)):
            ladder = log_ladder()
        self.assertNotIn(3000, ladder)
        self.assertTrue(all(n <= 2000 for n in ladder), ladder)

    def test_today_ladder_is_unchanged(self):
        self.assertEqual(log_ladder(), [200, 500, 1000, 2000],
                         "这批只统一出处，用户看得见的那几档不该跟着变")


class TestLogOptionsEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.users = {"b21-admin": "admin", "b21-viewer": "viewer"}
        for name, role in cls.users.items():
            dbm.execute(
                "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                (name, security.hash_pass("Password123"), role),
            )

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username IN (?,?)", tuple(cls.users))

    def _get(self, name: str):
        token = security.sign_token(name, self.users[name])
        return self.client.get("/api/system/log-options", headers={"Authorization": f"Bearer {token}"})

    def test_admin_gets_the_backend_numbers(self):
        body = self._get("b21-admin").json()
        self.assertEqual(body["lines_default"], LOG_LINES_DEFAULT)
        self.assertEqual(body["lines_max"], HTTP_LOG_LINES_MAX)
        self.assertEqual(body["ladder"], log_ladder())

    def test_tail_bytes_come_from_the_reader_itself(self):
        """文案里那个 KB 必须和真正 seek 的字节数是同一个数。"""
        self.assertEqual(self._get("b21-admin").json()["nginx_tail_bytes"], web_logs.TAIL_BYTES)

    def test_endpoint_delegates_to_the_ladder(self):
        with mock.patch("app.routers.system.log_ladder", lambda: [7, 77]):
            self.assertEqual(self._get("b21-admin").json()["ladder"], [7, 77])

    def test_viewer_cannot_read_it(self):
        self.assertEqual(self._get("b21-viewer").status_code, 403)

    def test_anonymous_is_not_served(self):
        self.assertEqual(self.client.get("/api/system/log-options").status_code, 401)


class TestNoCopiesLeftInWeb(unittest.TestCase):
    def test_log_center_fetches_the_options(self):
        self.assertIn("req('/system/log-options')", LOG_SRC)
        self.assertIn("system:logopts", LOG_SRC, "清单要走缓存层，别每次切页都重打")

    def test_markup_has_no_number_copies(self):
        src = code_of(LOG_SRC)
        for needle in ("200 行", "500 行", "1000 行", "2000 行", "512KB"):
            self.assertNotIn(needle, src, f"页面不该再写死 {needle}")
        self.assertIn("lineOpts", src)
        self.assertIn("tailNote", src)

    def test_lines_start_empty_until_the_server_answers(self):
        """初值写死 500 等于又抄一份默认值：拿到清单前留空，由 watch 回填。"""
        self.assertIn("grep: '', limit: 0 }", LOG_SRC)
        self.assertIn("lines: 0, filter: ''", LOG_SRC)

    def test_zero_initial_value_does_not_paint_a_bare_number(self):
        """清单还没到、候选文件还没取时，下拉里不该孤零零画一个 0 而不是提示语。"""
        self.assertIn('jf.limit || null', LOG_SRC)
        self.assertIn('nf.lines || null', LOG_SRC)
        # 序号 0 是"第一个候选文件"，是有意义的值，只能按"有没有候选"判，不能按真假判
        self.assertIn('nginxFileOpts.length ? nf.file : null', LOG_SRC)


class TestStreamTailSharesTheSameNumbers(unittest.TestCase):
    """实时日志（面板 / 服务 / 应用三处共用一个组件）读的是同一份清单。"""

    def test_sse_clamp_is_the_shared_constant(self):
        self.assertNotIn("min(lines, 2000)", ROUTER_SRC, "路由里不许再有第二个 2000")
        self.assertIn("min(lines, HTTP_LOG_LINES_MAX)", ROUTER_SRC)

    def test_component_reads_options_instead_of_copying(self):
        src = code_of(STREAM_SRC)
        self.assertIn("system:logopts", src, "与日志中心同一个缓存键：两个页面共用一次请求")
        self.assertNotIn(':max="2000"', src)
        self.assertNotIn("ref(200)", src)

    def test_url_omits_lines_until_the_user_picks(self):
        """不传 lines 就是把默认值留给服务端——第三份 200 因此没有存在的必要。"""
        self.assertEqual(parse_lines(None), LOG_LINES_DEFAULT)
        self.assertNotIn("lines = 200", API_SRC)
        self.assertIn("lines ? ", API_SRC)

    def test_viewer_without_the_options_can_still_read_logs(self):
        """log-options 要 admin；viewer 在应用详情页拿不到清单时不该报错，只是不显示数字。"""
        self.assertIn("linesCount.value ?? undefined", code_of(STREAM_SRC))


class TestDynamicInputButtonsLabeled(unittest.TestCase):
    def test_action_slot_replaces_the_unnamed_buttons(self):
        src = code_of(APP_SRC)
        self.assertIn("#action=", src, "NDynamicInput 的行内按钮格要自己接管")
        self.assertIn("删除这一项环境变量", src)
        self.assertIn("新增一项环境变量", src)

    def test_both_buttons_actually_wire_the_slot_props(self):
        src = code_of(APP_SRC)
        self.assertIn("remove(index)", src)
        self.assertIn("create()", src)


if __name__ == "__main__":
    unittest.main()
