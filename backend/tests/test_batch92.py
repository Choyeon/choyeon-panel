"""批次92：firewalld 的 service 端口核对触顶后，不许悄悄把「压根没查」说成「核对过了」。

`_enrich_service_ports` 只核对前 `ENRICH_MAX_RULES` 条规则里的 service 项（一台规则上百条的机器
不该被这一层逐条 spawn `--info-service` 拖垮）。触顶本身是对的，错的是它静默：

1. 第 13 条起的 service 规则从来没人给它们写过 `ports_known`，于是 `might_protect` /
   `blind_allow_rules` 之所以还按「读不到」处理，全靠 `.get("ports_known")` 缺键兜底成 None——
   把「会不会把人锁在门外」的安全判定压在「少写一个键」这个巧合上，谁改动读取方式谁踩雷。
   这批把没轮到的 service 规则**显式**标成 `ports_known=None`，状态写在纸上而不是靠缺失。
2. 触顶的条数既不进载荷也不进判词：一张 13 条的放行表会被读成「端口定义全部核对过」，而其中
   一条可能正是唯一的 SSH 放行。这批让它在 `service_ports_skipped` 里如实报数，CLI 与前端照数说事。
3. 那个上限 `12` 原来裸写在服务层、CLI 抄一遍、测试再猜一遍。这批收成 `ENRICH_MAX_RULES` 一个出处。
4. 另一处坑：permanent 那份表的第二个数**不许**混进 `perm_bad`。`perm_bad` 的语义是「这份表读没读到」，
   喂给 `persisted`（这条规则重载后还在不在）。表读到了、只是有几条 service 的端口没查，
   不能因此把八条已写进配置的规则全标成「重载后会丢」。

真实探测三态（命令没回来 / 退 252 / 超时）在批次㊷A 已钉死；本批只管「读到了表、但端口核对没扫完」。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch92_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch92_roots_"))

import cli  # noqa: E402
from app import config  # noqa: E402
from app.services import firewall_service as fw  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
WEB = BACKEND.parent / "web" / "src"

# 一份 5 条 service 的运行期放行表：把 ENRICH_MAX_RULES 打到 3，后两条（dns/mysql）就是「压根没查」的。
FW_MANY = "  target: default\n  services: ssh http https dns mysql\n  ports: \n  rich rules:\n"


def _info(port):
    return {"code": 0, "out": f"  service\n  port: {port}/tcp\n"}


class Probe:
    """`run()` 的替身：按「命令 + 参数」逐条给答复，答不出来的命令让测试直接失败。"""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[str] = []

    async def run(self, cmd, args, cwd=None, timeout=None, env=None):
        key = " ".join([cmd, *[str(a) for a in args]])
        self.calls.append(key)
        for pat, val in self.answers.items():
            if key == pat or (pat.endswith(" ") and key.startswith(pat)):
                return dict(val)
        raise AssertionError(f"未预设的探测命令: {key}")


def _fw_answers(**over):
    base = {
        "firewall-cmd --state": {"code": 0, "out": "running\n"},
        "firewall-cmd --get-default-zone": {"code": 0, "out": "public\n"},
        "firewall-cmd --list-all": {"code": 0, "out": FW_MANY},
        "firewall-cmd --permanent --list-all": {"code": 0, "out": FW_MANY},
    }
    # 5 个 service，运行期与 permanent 各一条 info；被跳过的那两条永远不该被问到。
    for tok, port in (("ssh", 22), ("http", 80), ("https", 443), ("dns", 53), ("mysql", 3306)):
        base[f"firewall-cmd --info-service={tok}"] = _info(port)
        base[f"firewall-cmd --permanent --info-service={tok}"] = _info(port)
    base.update(over)
    return base


def _pin(test):
    tmp = tempfile.TemporaryDirectory(prefix="cp_batch92_")
    test.addCleanup(tmp.cleanup)
    Path(tmp.name, "sshd_config").write_text("Port 22\n", encoding="utf-8")
    patches = [
        mock.patch.object(fw, "SSHD_CONFIG", str(Path(tmp.name, "sshd_config"))),
        mock.patch.object(fw, "NGINX_CONF_GLOBS", (str(Path(tmp.name, "nginx", "*.conf")),)),
        mock.patch.object(config, "PORT", 3210),
        mock.patch.object(config, "HOST", "127.0.0.1"),
    ]
    for p in patches:
        p.start()
        test.addCleanup(p.stop)


def _run_patch(test, answers):
    p = Probe(answers)
    patch = mock.patch.object(fw, "run", p.run)
    patch.start()
    test.addCleanup(patch.stop)
    return p


# ---------------------------------------------------------------- _enrich_service_ports 本体

class EnrichCapTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_reason_and_skipped_count(self):
        _run_patch(self, _fw_answers())
        with mock.patch.object(fw, "ENRICH_MAX_RULES", 3):
            rules = fw.parse_firewalld(FW_MANY)
            reason, skipped = await fw._enrich_service_ports(rules)
        self.assertEqual(reason, "", "前 3 条都查成了，命令层没有失败")
        self.assertEqual(skipped, 2, "第 4、5 条是压根没查，不是查了没查到")

    async def test_beyond_cap_service_rules_are_marked_unread(self):
        _run_patch(self, _fw_answers())
        with mock.patch.object(fw, "ENRICH_MAX_RULES", 3):
            rules = fw.parse_firewalld(FW_MANY)
            await fw._enrich_service_ports(rules)
        # 触顶前两条：确实查到了端口定义。
        self.assertIs(rules[0]["ports_known"], True)
        self.assertIs(rules[2]["ports_known"], True)
        # 触顶后两条：键被显式写成 None（不是缺失），安全判定不再靠 .get() 兜底。
        for r in (rules[3], rules[4]):
            self.assertIn("ports_known", r, "没轮到的规则要写下它「没读到」，而不是留个缺键")
            self.assertIsNone(r["ports_known"])

    async def test_only_service_rules_within_cap_are_spawned(self):
        p = _run_patch(self, _fw_answers())
        with mock.patch.object(fw, "ENRICH_MAX_RULES", 3):
            await fw._enrich_service_ports(fw.parse_firewalld(FW_MANY))
        asked = [c for c in p.calls if "--info-service=" in c]
        self.assertEqual(asked, [
            "firewall-cmd --info-service=ssh",
            "firewall-cmd --info-service=http",
            "firewall-cmd --info-service=https",
        ], "触顶之后的两条 service 不该再 spawn，也不该被当成查过")

    async def test_within_cap_reports_no_skip(self):
        _run_patch(self, _fw_answers())
        with mock.patch.object(fw, "ENRICH_MAX_RULES", 12):
            reason, skipped = await fw._enrich_service_ports(fw.parse_firewalld(FW_MANY))
        self.assertEqual((reason, skipped), ("", 0), "没触顶就如实说没跳过")

    async def test_non_service_rules_beyond_cap_are_not_counted(self):
        _run_patch(self, _fw_answers())
        # ssh/http 在触顶前查完；第 3 条是端口规则，没有端口定义可查，不该算进「没核对的 service」。
        rules = fw.parse_firewalld("  services: ssh http\n  ports: 8080/tcp\n")
        with mock.patch.object(fw, "ENRICH_MAX_RULES", 2):
            _reason, skipped = await fw._enrich_service_ports(rules)
        self.assertEqual(skipped, 0, "端口规则没有端口定义可查，跳过它不算「没核对 service」")

    async def test_failure_within_cap_and_skip_beyond_cap_are_independent(self):
        _run_patch(self, _fw_answers(**{"firewall-cmd --info-service=http": {"code": 1, "out": ""}}))
        with mock.patch.object(fw, "ENRICH_MAX_RULES", 3):
            reason, skipped = await fw._enrich_service_ports(fw.parse_firewalld(FW_MANY))
        self.assertIn("--info-service", reason, "触顶前那条查失败仍要报「读不到的原因」")
        self.assertEqual(skipped, 2, "命令失败与触顶跳过是两笔账，各记各的")


# ---------------------------------------------------------------- 安全判定落在写下来的状态上

class LockoutSafetyTests(unittest.TestCase):
    def _skipped_rule(self):
        return {"kind": "service", "token": "ssh", "action": "allow",
                "direction": "in", "source": "anywhere", "ports_known": None}

    def test_might_protect_refuses_deleting_an_unchecked_ssh_service(self):
        self.assertTrue(fw.might_protect(self._skipped_rule(), 22),
                        "没核对端口的 ssh service 要按最坏情况「也许正保护 22」")

    def test_blind_allow_lists_the_unchecked_rule(self):
        self.assertEqual([r["token"] for r in fw.blind_allow_rules([self._skipped_rule()])], ["ssh"])

    def test_covers_does_not_claim_reachability_from_an_unchecked_rule(self):
        self.assertFalse(fw.covers(self._skipped_rule(), 22),
                         "没读到端口定义不能宣布「这端口外面进得来」")


# ---------------------------------------------------------------- read() 载荷：报数且不污染 persisted

class ReadPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_skipped_and_cap_surface_in_the_payload(self):
        _pin(self)
        _run_patch(self, _fw_answers())
        with mock.patch.object(fw, "ENRICH_MAX_RULES", 3):
            out = await fw.read(fw.FIREWALLD)
        self.assertTrue(out["rules_ok"], "这份表确实读到了")
        self.assertEqual(out["service_ports_skipped"], 2)
        self.assertEqual(out["service_ports_enrich_max"], 3)

    async def test_skip_does_not_turn_persisted_into_unknown(self):
        _pin(self)
        _run_patch(self, _fw_answers())
        with mock.patch.object(fw, "ENRICH_MAX_RULES", 3):
            out = await fw.read(fw.FIREWALLD)
        # permanent 那份表读到了（运行期与 permanent 内容一致），所以每条规则都该有 True/False，
        # 而不是因为有几条 service 的端口没查就把整列标成 None（那等于谎报「重载后会丢」）。
        self.assertTrue(all(r["persisted"] is not None for r in out["rules"]),
                        "端口没核对 ≠ 这份表没读到：persisted 不许被触顶跳过污染成未知")

    async def test_field_defaults_present_for_ufw(self):
        _pin(self)
        _run_patch(self, {
            "ufw status verbose": {"code": 0, "out": "Status: active\nDefault: deny (incoming)\n"},
            "ufw status numbered": {"code": 0, "out": "Status: active\n"},
        })
        out = await fw.read(fw.UFW)
        self.assertEqual(out["service_ports_skipped"], 0, "没有 service 规则的 ufw 也带着这两个键，前端不必判空")


# ---------------------------------------------------------------- CLI 文案与契约同源

class FirewallTextTests(unittest.TestCase):
    def _data(self, skipped, cap):
        return {"tool": "firewalld", "active": True, "rules_ok": True, "reason": "",
                "protected": [], "protected_reasons": {}, "zone": "public", "default_incoming": "",
                "service_ports_skipped": skipped, "service_ports_enrich_max": cap,
                "rules": [{"num": i, "kind": "service", "token": f"s{i}", "action": "allow",
                           "direction": "in", "source": "anywhere", "sig": f"sig{i}", "port": None}
                          for i in range(5)]}

    def test_names_the_uncapped_count_when_rules_were_skipped(self):
        text = cli._firewall_table(self._data(2, 3))
        self.assertIn("2 条 service 规则的端口定义本轮没查", text)
        self.assertIn("只核对前 3 条", text)
        self.assertIn("按读不到处理", text)

    def test_says_nothing_when_fully_enriched(self):
        text = cli._firewall_table(self._data(0, 12))
        self.assertNotIn("本轮没查", text)

    def test_schema_note_single_sources_the_cap(self):
        src = (BACKEND / "cli.py").read_text(encoding="utf-8")
        self.assertIn("firewall_service.ENRICH_MAX_RULES", src,
                      "契约里的这个数字要引用服务层那个常量，不是再抄一个 12")
        self.assertIn("service_ports_skipped", src)


# ---------------------------------------------------------------- 前端契约

class FrontendContractTests(unittest.TestCase):
    def test_firewall_view_reads_the_two_payload_fields(self):
        vue = (WEB / "views" / "Firewall.vue").read_text(encoding="utf-8")
        self.assertIn("service_ports_skipped", vue)
        self.assertIn("service_ports_enrich_max", vue)
        self.assertIn("enrichSkipped", vue)
        # 前端不硬写那个上限数字：从载荷读，别处改上限时页面不会说谎。
        self.assertNotIn("只核对前 12", vue)


if __name__ == "__main__":
    unittest.main()
