"""批次98：证书续期不许把「读不到到期清单」说成「没有证书到期，这是正常结果，不是失败」。

certbot 退出码 0 只证明它自己跑完了，不证明面板观察得到到期日。旧实现里
`renew_certs._expiries()` 只留 `expiry`、丢掉 `list_certs()` 的 source/reason 三态：
certbot 不可用且证书目录也读不动时清单为空 → diff 为空 → 直接断言一条机器级否定
（「这是正常结果，不是失败」）并返回 200，而同页的证书面板却如实标「数据不可用」。
这里把「空清单到底是确认没有、还是根本没读出来」收进 list_certs 的 `confirmable`
单一出处，续期读它来决定 summary，前端读 `confirmed` 决定弹绿字还是黄字。

同一判据还要贯穿另外三个读者，否则同一个事实会有两套说法：链路对账的 `_cert_state`
（旧实现按 `source == "none"` 自推，把「certbot 正常但本机一张证书都没有」降级成「这一轮没查」）、
站点页证书区块的空表文案与「清单不可用」提示，以及两个续期入口各自的审计 detail。
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

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp98_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp98_roots_"))

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.routers import extras, sites  # noqa: E402
from app.services import cert_ops, nginx_ops, site_audit_service as SA  # noqa: E402


def _req(sub: str = "renewer"):
    return SimpleNamespace(state=SimpleNamespace(cp_role="admin", cp_sub=sub),
                           json=None, headers={}, client=SimpleNamespace(host="ut"))


def _readers(certbot, openssl):
    """把两条来源换成受控替身：每个返回 (certs|None, reason|None)。"""
    async def _cb(_now, _th):
        return certbot

    async def _op(_now, _th):
        return openssl

    return mock.patch.object(cert_ops, "_from_certbot", _cb), \
        mock.patch.object(cert_ops, "_from_openssl", _op)


class CertListConfirmableTests(unittest.TestCase):
    """空清单分两档，判据只出自 list_certs 一个地方，别让调用方去 match reason 文本。"""

    def test_有清单就可信(self):
        rows = [{"name": "a.example.com", "expiry": None}]
        p1, p2 = _readers((rows, None), (None, "x"))
        with p1, p2:
            d = asyncio.run(cert_ops.list_certs())
        self.assertTrue(d["confirmable"])
        self.assertEqual(d["source"], "certbot")

    def test_两个读者都跑通且为空_是确认没有_可信(self):
        p1, p2 = _readers(([], None), ([], None))
        with p1, p2:
            d = asyncio.run(cert_ops.list_certs())
        self.assertEqual(d["certs"], [])
        self.assertTrue(d["confirmable"], "两条来源都读成功而确实为空＝确认本机没有证书，必须可信")
        self.assertIn("本机没有证书", d["reason"])

    def test_certbot_不可用且退化也失败_不可信(self):
        p1, p2 = _readers((None, "certbot 不可用（exit 1）"),
                          (None, "证书目录不可读（PermissionError）"))
        with p1, p2:
            d = asyncio.run(cert_ops.list_certs())
        self.assertEqual(d["certs"], [])
        self.assertFalse(d["confirmable"], "certbot 读不了、退化也没给出清单，不能冒充已确认")
        self.assertIn("exit 1", d["reason"])

    def test_certbot_可用但报空_可信(self):
        p1, p2 = _readers(([], None), (None, "证书目录不可读（OSError）"))
        with p1, p2:
            d = asyncio.run(cert_ops.list_certs())
        self.assertTrue(d["confirmable"])
        self.assertEqual(d["source"], "certbot")


async def _run_ok(*_a, **_k):
    return {"code": 0, "out": "No renewals were attempted.", "err": ""}


def _lc(payload):
    async def _stub():
        return payload
    return mock.patch.object(cert_ops, "list_certs", _stub)


class RenewCertsHonestyTests(unittest.TestCase):
    """续期结论必须随「这次到底读不读得到清单」而变，而不是无条件给绿灯。"""

    def test_读不到清单不许说成正常结果(self):
        untrusted = {"certs": [], "source": "none", "confirmable": False,
                     "reason": "certbot 不可用（exit 1）"}
        with mock.patch.object(nginx_ops, "run", _run_ok), _lc(untrusted):
            r = asyncio.run(nginx_ops.renew_certs())
        self.assertFalse(r["confirmed"])
        self.assertNotIn("没有证书到期", r["summary"])
        self.assertNotIn("这是正常结果，不是失败", r["summary"])
        self.assertIn("没能确认", r["summary"])

    def test_确认本机没有证书仍给正常结论(self):
        confirmed_empty = {"certs": [], "source": "none", "confirmable": True,
                           "reason": "本机没有证书（certbot 与证书目录都为空）"}
        with mock.patch.object(nginx_ops, "run", _run_ok), _lc(confirmed_empty):
            r = asyncio.run(nginx_ops.renew_certs())
        self.assertTrue(r["confirmed"])
        self.assertIn("没有证书到期", r["summary"])

    def test_读到清单且到期日未变仍给正常结论(self):
        rows = {"certs": [{"name": "a.example.com", "expiry": "2026-11-01"}],
                "source": "certbot", "confirmable": True, "reason": None}
        with mock.patch.object(nginx_ops, "run", _run_ok), _lc(rows):
            r = asyncio.run(nginx_ops.renew_certs())
        self.assertTrue(r["confirmed"])
        self.assertEqual(r["renewed"], [])
        self.assertIn("没有证书到期", r["summary"])

    def test_真实续期照旧点名(self):
        seq = iter(["2026-10-20", "2027-01-19"])

        async def _stub():
            return {"certs": [{"name": "a.example.com", "expiry": next(seq)}],
                    "source": "certbot", "confirmable": True, "reason": None}

        async def _any_run(cmd, args, **_k):
            return {"code": 0, "out": "", "err": ""}

        with mock.patch.object(nginx_ops, "run", _any_run), \
             mock.patch.object(cert_ops, "list_certs", _stub):
            r = asyncio.run(nginx_ops.renew_certs())
        self.assertEqual(r["renewed"], ["a.example.com"])
        self.assertIn("已续期 1 张", r["summary"])

    def test_certbot_失败仍抛错不误伤(self):
        async def _fail(*_a, **_k):
            return {"code": 1, "out": "all attempts failed", "err": ""}
        with mock.patch.object(nginx_ops, "run", _fail):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(nginx_ops.renew_certs())
        self.assertIn("续期失败", str(ctx.exception))


def _unconfirmed_result():
    async def _r():
        return {"log": "x", "renewed": [], "unchanged": [], "reloaded": False,
                "confirmed": False,
                "summary": "certbot 退出码 0，但面板没能确认到期清单（…），无法断定这次到底续到了没有"}
    return mock.patch.object(nginx_ops, "renew_certs", _r)


class RenewEndpointContractTests(unittest.TestCase):
    """两个续期入口都要把 confirmed 原样带出去，前端据此分绿/黄，而不是只看有没有报错。"""

    def test_site_renew_带出_confirmed_false(self):
        with _unconfirmed_result(), mock.patch.object(sites.aio_db, "audit"):
            r = asyncio.run(sites.ssl_renew("a.example.com", _req()))
        self.assertIs(r["confirmed"], False)

    def test_panel_renew_带出_confirmed_false(self):
        with _unconfirmed_result(), mock.patch.object(extras.aio_db, "audit"):
            r = asyncio.run(extras.certbot_renew(_req()))
        self.assertIs(r["confirmed"], False)


class RenewToastTriStateSourceTests(unittest.TestCase):
    """前端把「读不到」弹成黄色告警而不是绿色成功——这条判据只有一个出处在 Sites.vue 的 renew()。"""

    SRC = (BACKEND.parent / "web" / "src" / "views" / "Sites.vue").read_text(encoding="utf-8")

    def test_未确认时走_warning_而非_success(self):
        self.assertIn("r.confirmed === false", self.SRC)
        self.assertIsNotNone(
            re.search(r"if \(r\.confirmed === false\)\s*msg\.warning", self.SRC),
            "confirmed=false 没有走 msg.warning（黄色告警），会被当成成功")

    def test_证书清单区块也读_confirmable_不再自推_source(self):
        """同一个 `list_certs` 三态判据（confirmable）要贯穿到区块，别在模板里重推。

        `source === 'none'` 混了两件事：两个读者都跑不通，和两个读者都跑通而本机一张证书
        都没有。模板按 source 判的话，后者会显示「读不到任何证书」，紧挨着的提示语却写着
        「本机没有证书（certbot 与证书目录都为空）」——两行自相矛盾。
        """
        self.assertNotIn("certsInfo.source === 'none'", self.SRC)
        self.assertIn("certsInfo.confirmable !== true", self.SRC, "「清单不可用」提示的判据")
        self.assertIn("certsInfo.confirmable === true", self.SRC, "空表文案的判据")


class SiteAuditUsesSameJudgeTests(unittest.TestCase):
    """链路对账不许从 `source` 再推一遍「读没读到」——它和续期那边会给出相反的说法。"""

    UNREADABLE = {"certs": [], "source": "none", "confirmable": False, "reason": "certbot 不可用（exit 1）"}
    CONFIRMED_EMPTY = {"certs": [], "source": "none", "confirmable": True, "reason": "本机没有证书"}

    def test_确认没有证书时照判覆盖(self):
        ok, covered, reason = SA._cert_state(self.CONFIRMED_EMPTY)
        self.assertTrue(ok, "两个读者都跑通而确实为空，等于已确认，不该降级成「没查」")
        self.assertEqual(covered, set())
        self.assertEqual(reason, "")

    def test_读不到时才给未确认(self):
        ok, _, reason = SA._cert_state(self.UNREADABLE)
        self.assertFalse(ok)
        self.assertIn("certbot 不可用", reason)


if __name__ == "__main__":
    unittest.main()
