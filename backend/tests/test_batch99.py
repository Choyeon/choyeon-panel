"""批次99：firewall 写进 permanent 表 ≠ 现在就生效，审计与前端 toast 不许只看 200 就报「已生效」。

firewalld 命中防自锁保护时（运行期表读不到、或 reload 会冲掉管理员 SSH 里的应急放行），
`add_rule` / `remove_rule` 只改 permanent，故意不替用户跑 `--reload`，并在响应体里带
`reloaded: false` + 一句明说「没有执行 --reload」的 detail。旧端点把这句话原样回了前端，
审计却无条件写「已生效」/「已删除」——同一次请求两条通道对同一事实各说各话，事后翻审计的
人以为那条放行早就生效了（而它此刻还在门外）。判据统一收成 `_written_or_live`：只读响应体
那一个 `reloaded`，缺这个键（ufw 走的是立即生效路径）仍按「已生效」写。

前端同理：删除路径早就按 `reloaded === false` 分黄字，添加路径只看 200 报绿字，
这里补上同一个分支，并把两条路径的判据钉在源码断言里，避免下一个视图又各写一套。
"""

import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp99_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp99_roots_"))

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.routers import system  # noqa: E402


def _req(sub: str = "fw_admin"):
    return SimpleNamespace(
        state=SimpleNamespace(cp_role="admin", cp_sub=sub),
        json=None, headers={}, query_params={},
        client=SimpleNamespace(host="ut"))


def _run_add(out):
    """跑一次真实端点，把 service 层换成受控响应体，返回审计里那句 detail。"""
    details = []

    async def _detect():
        return "firewalld"

    async def _add(_body, _tool, _req):
        return out

    async def _audit(_sub, _action, detail):
        details.append(detail)

    with mock.patch.object(system.firewall_service, "detect", _detect), \
         mock.patch.object(system.firewall_service, "add_rule", _add), \
         mock.patch.object(system.aio_db, "audit", _audit):
        resp = asyncio.run(system.firewall_add(_req()))
    return resp, details[-1] if details else ""


def _run_remove(out):
    details = []

    async def _detect():
        return "firewalld"

    async def _rm(_num, _expect, _tool, _req):
        return out

    async def _audit(_sub, _action, detail):
        details.append(detail)

    with mock.patch.object(system.firewall_service, "detect", _detect), \
         mock.patch.object(system.firewall_service, "remove_rule", _rm), \
         mock.patch.object(system.aio_db, "audit", _audit):
        resp = asyncio.run(system.firewall_remove(7, _req()))
    return resp, details[-1] if details else ""


class AddAuditVerdictTests(unittest.TestCase):
    def test_未reload时审计不许写已生效(self):
        out = {"ok": True, "added": True, "reloaded": False,
               "detail": "规则已写入，但没有执行 --reload（会导致运行期应急放行被冲掉），请手动 reload"}
        resp, detail = _run_add(out)
        self.assertIs(resp, out)
        self.assertIn("未 reload", detail)
        self.assertIn("尚未生效", detail)
        self.assertNotIn("已生效", detail)

    def test_立刻生效的路径照旧写已生效(self):
        """ufw 那一条根本不带 reloaded 键，不能被新判据误降级成「只写进表里」。"""
        out = {"ok": True, "added": True, "detail": "规则已添加"}
        resp, detail = _run_add(out)
        self.assertIs(resp, out)
        self.assertIn("已生效", detail)
        self.assertNotIn("尚未生效", detail)

    def test_reloaded为true时就是已生效(self):
        _resp, detail = _run_add({"ok": True, "added": True, "reloaded": True, "detail": "x"})
        self.assertIn("已生效", detail)


class RemoveAuditVerdictTests(unittest.TestCase):
    def test_只删permanent时审计点名运行期规则仍在(self):
        out = {"ok": True, "removed": "ALLOW tcp port=8080", "reloaded": False, "detail": "未 reload"}
        resp, detail = _run_remove(out)
        self.assertIs(resp, out)
        self.assertIn("已从 permanent 删除 ALLOW tcp port=8080", detail)
        self.assertIn("运行期规则仍在放行", detail)
        self.assertNotRegex(detail, r"-> 已删除 ")

    def test_缺reloaded键仍按已删除写(self):
        _resp, detail = _run_remove({"ok": True, "removed": "80/tcp", "detail": "ok"})
        self.assertIn("已删除 80/tcp", detail)
        self.assertNotIn("permanent", detail)


class FrontendJudgeSourceTests(unittest.TestCase):
    """添加与删除两条写路径必须读同一个 `reloaded === false`，否则同一事实两种颜色的 toast。"""

    SRC = BACKEND.parent / "web" / "src" / "views" / "Firewall.vue"

    def test_添加路径按reloaded分黄字(self):
        src = self.SRC.read_text(encoding="utf-8")
        add_block = src.split("async function submitAdd()")[1].split("async function ")[0] \
            if "async function submitAdd()" in src else ""
        self.assertTrue(add_block, "没找到 submitAdd，断言不能空过")
        self.assertIsNotNone(
            re.search(r"if \(r\.reloaded === false\)\s*msg\.warning", add_block),
            "submitAdd 没有按 reloaded===false 走 warning")

    def test_删除路径的判据没有被改掉(self):
        src = self.SRC.read_text(encoding="utf-8")
        self.assertIn("if (res.reloaded === false) msg.warning", src)


if __name__ == "__main__":
    unittest.main()
