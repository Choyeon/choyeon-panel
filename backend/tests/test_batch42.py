"""批次㊷A：防火墙的"没读到"必须是第三种答案。

线上那台机器给过一版很干净的证据：`ufw status verbose` 卡住（面板按批次㊶的口径等 20 秒后
杀掉整个进程组），旧代码把退出码直接丢掉，正文是超时那句短话，于是
`read()` 回的是 `active=False、rules=[]`——"这台机器防火墙没开，而且一条规则都没有"。
三个下游同时照着这句话办事：防火墙页显示"未启用"，端口暴露面对账把每个绑 0.0.0.0 的端口
都盖成"防火墙挡住了"（可能正对公网的口子被报成安全），自检那一项——它当时是**第二个独立的
读者**，自己找工具、自己在正文里找 inactive 字样——反过来报"ufw 已启用"。同一台机器在同一秒
给出两个相反结论，而两个都不对。写路径更糟：`set_enabled(True)` 拿一份空表判"这些端口没人放行"，
`remove_rule` 拿一份空表报"编号不存在"，都是把探测失败当数据用。

这批把五件事钉成契约：

1. `read()` 只有一种产出形态：三态 `active`（True/False/None）配 `state_ok`、`rules_ok`、
   `reason`。探测没回来就是 None，不是 False；表没回来就是 `rules_ok=False`，不是空列表。
2. 两个判词分开，因为有两台机器需要不同的说法。`unread()` 回答"能不能判外部可达性"，
   `rules_unread()` 回答"那张放行表在不在手里"。firewalld 停着的那台机器上后者非空、前者为空：
   表确实不存在，但没有过滤就是全都进得来，这一题有答案。合成一个判词就会二选一地犯错——
   要么给没设防的机器打上 unknown（最该看到"可达，请改绑回环"的那台反而看不了），
   要么让删除/启用拿着一张不存在的表做决定。
3. 危险判定 fail-closed：表没读到的时候，删除/启用/reload 一律拒绝（没有绕过参数）；
   `--info-service` 没读到的那条 service 规则，删除侧按"它也许正是唯一的 SSH 放行"处理，
   可达性侧既不说放行也不说被挡。两边都不许把"没读到"当成"没有"。
4. 自检项不再是第二个读者：`_check_firewall()` 只渲染 `read()`，判词由服务层给一次。
   这条连源码一起钉住，否则以后有人"顺手"在那里再补一次 `ufw status` 就前功尽弃。

reload 被扣下时那句也单独写：那种情况下规则真的写进去了、只有重载没执行，
拿"这次操作已拒绝执行"回话会让管理员再点一次「添加」，于是 permanent 里落两份同样的规则。

第 5 条落在页面上：`read()` 交出去的三态一旦在模板里被 `st.active ? … : …` 这种真值判断吃掉，
就又只剩下两格答案，前面四条白写。所以防火墙页与端口对账的取值方式一起钉住源码——
`active === null` 必须单独渲染成"状态没读到"，`rules_ok === false` 必须锁住启停按钮，
端口表必须有第五档判词"判不了"，而且它的告警优先级排在"被防火墙挡住"之前。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch42_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch42_roots_"))

import cli  # noqa: E402
from app import config  # noqa: E402
from app.errors import Forbidden  # noqa: E402
from app.services import exposure_service as ex  # noqa: E402
from app.services import firewall_service as fw  # noqa: E402
from app.util import CTRL_TIMEOUT, timeout_note  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
WEB = BACKEND.parent / "web" / "src"

UFW_HEAD = ("Status: active\nLogging: on (low)\n"
            "Default: deny (incoming), allow (outgoing), disabled (routed)\nNew profiles: skip\n")
UFW_SSH = UFW_HEAD + "\n     To            Action      From\n     --            ------      ----\n[ 1] 22/tcp      ALLOW IN    Anywhere\n"
UFW_INACTIVE = UFW_HEAD.replace("Status: active", "Status: inactive") + "\nNo rules to display\n"
FW_SSH = "  target: default\n  services: ssh\n  ports: \n  rich rules:\n"
FW_EMPTY = "  target: default\n  services: \n  ports: \n  rich rules:\n"
SSH_INFO = {"code": 0, "out": "  service\n  port: 22/tcp\n"}
OK = {"code": 0, "out": "success\n"}


def _res(code, out=""):
    return {"code": code, "out": out}


def _timeout(what):
    return _res(124, timeout_note(what, CTRL_TIMEOUT))


class Probe:
    """`run()` 的替身：按「命令 + 参数」逐条给答复，答不出来的命令让测试直接失败。

    这一批要测的全是"某一条探测没回来"，所以替身必须能只让其中一条失败——把整套状态机糊上去
    就看不见是哪条探测把结论带歪了，而那正是这批的缺陷本体。
    """

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def run(self, cmd, args, cwd=None, timeout=None, env=None):
        key = " ".join([cmd, *[str(a) for a in args]])
        self.calls.append((key, timeout))
        for pat, val in self.answers.items():
            if key == pat or (pat.endswith(" ") and key.startswith(pat)):
                return dict(val)
        raise AssertionError(f"未预设的探测命令: {key}")

    def keys(self):
        return [k for k, _t in self.calls]


def _pin(test):
    """受保护端口钉死成 {22}：sshd 配置指向临时文件，nginx 与面板端口都不参与。"""
    tmp = tempfile.TemporaryDirectory(prefix="cp_batch42_")
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


def _use(test, answers):
    _pin(test)
    p = Probe(answers)
    patch = mock.patch.object(fw, "run", p.run)
    patch.start()
    test.addCleanup(patch.stop)
    return p


def _fw_answers(**over):
    """一台正在运行的 firewalld 的完整答复表；`over` 用来把其中一条改成"没回来"。"""
    base = {
        "firewall-cmd --state": _res(0, "running\n"),
        "firewall-cmd --get-default-zone": _res(0, "public\n"),
        "firewall-cmd --list-all": _res(0, FW_SSH),
        "firewall-cmd --permanent --list-all": _res(0, FW_SSH),
        "firewall-cmd --info-service=ssh": SSH_INFO,
        "firewall-cmd --permanent --info-service=ssh": SSH_INFO,
        "firewall-cmd --permanent ": OK,
        "firewall-cmd --reload": OK,
        "systemctl enable --now firewalld": OK,
        "systemctl disable --now firewalld": OK,
    }
    base.update(over)
    return base


# ----------------------------------------------------------------- read() 的三态
class ReadTriStateCase(unittest.IsolatedAsyncioTestCase):
    async def test_ufw_timeout_is_not_inactive(self):
        p = _use(self, {"ufw status verbose": _timeout("ufw"), "ufw status numbered": _timeout("ufw")})
        out = await fw.read(fw.UFW)
        self.assertIsNone(out["active"], "超时不是「没启用」：这两个答案的后果一个是显示、一个是启停")
        self.assertFalse(out["state_ok"])
        self.assertFalse(out["rules_ok"])
        self.assertIn("超时", out["reason"])
        self.assertTrue(fw.unread(out))
        self.assertEqual(p.keys(), ["ufw status verbose", "ufw status numbered"])

    async def test_ufw_numbered_failure_loses_only_the_table(self):
        """状态读到了、编号表没读到：`active` 照实给，表那一格必须标成没读到。"""
        _p = _use(self, {
            "ufw status verbose": _res(0, UFW_SSH),
            "ufw status numbered": _res(6, "ERROR: problem running ufw-init"),
        })
        out = await fw.read(fw.UFW)
        self.assertIs(out["active"], True)
        self.assertTrue(out["state_ok"])
        self.assertFalse(out["rules_ok"])
        self.assertEqual(out["rules"], [])
        self.assertIn("退出码 6", fw.rules_unread(out))
        self.assertIn("退出码 6", fw.unread(out), "判可达性用的是同一份原因，不另起一句")

    async def test_firewalld_stopped_is_an_answer_not_a_failure(self):
        """`--state` 退 252 回 "not running" 是一次**成功**的探测：没在运行。

        这台机器上运行期规则表确实不存在，可达性却判得了（没有过滤）。两个判词在这里分开。
        """
        _p = _use(self, _fw_answers(**{
            "firewall-cmd --state": _res(252, "not running\n"),
            "firewall-cmd --list-all": _res(252, "FirewallD is not running\n"),
        }))
        out = await fw.read(fw.FIREWALLD)
        self.assertIs(out["active"], False)
        self.assertTrue(out["state_ok"])
        self.assertFalse(out["rules_ok"])
        self.assertEqual(fw.unread(out), "", "停着的防火墙要能判可达性，否则整页变 unknown")
        self.assertIn("运行期规则表读不到", fw.rules_unread(out))

    async def test_firewalld_state_garbage_is_unread(self):
        p = _use(self, _fw_answers(**{"firewall-cmd --state": _res(1, "")}))
        out = await fw.read(fw.FIREWALLD)
        self.assertIsNone(out["active"], "正文里没有 running / not running，就不能说它没在跑")
        self.assertFalse(out["state_ok"])
        self.assertTrue(fw.unread(out))
        self.assertIn("firewall-cmd --state", p.keys())

    async def test_tool_none_is_a_confirmed_state(self):
        out = await fw.read("none")
        self.assertIs(out["active"], False)
        self.assertTrue(out["state_ok"])
        self.assertTrue(out["rules_ok"])
        self.assertEqual(fw.unread(out), "")
        self.assertEqual(fw.rules_unread(out), "")

    async def test_every_probe_uses_the_shared_timeout(self):
        """批次㊶的口径：这一层的超时预算只有一个出处，不许再有人写裸数字。"""
        p = _use(self, _fw_answers())
        await fw.read(fw.FIREWALLD)
        self.assertTrue(p.calls)
        for key, budget in p.calls:
            self.assertEqual(budget, CTRL_TIMEOUT, f"{key} 又自带一份超时预算")

    async def test_inactive_ufw_has_no_table_to_lose(self):
        _p = _use(self, {"ufw status verbose": _res(0, UFW_INACTIVE),
                         "ufw status numbered": _res(0, UFW_INACTIVE)})
        out = await fw.read(fw.UFW)
        self.assertIs(out["active"], False)
        self.assertTrue(out["rules_ok"], "ufw 关掉时那份表是「读到了，是空的」")
        self.assertEqual(out["rules"], [])
        self.assertEqual(fw.unread(out), "")


# --------------------------------------------------------- 表没读到，写操作一律拒绝
class WriteRefusesBlindReadCase(unittest.IsolatedAsyncioTestCase):
    async def test_remove_refuses_when_table_not_read(self):
        p = _use(self, {"ufw status verbose": _res(0, UFW_SSH), "ufw status numbered": _timeout("ufw")})
        with self.assertRaises(Forbidden) as ctx:
            await fw.remove_rule(1, "22/tcp|ALLOW|any", fw.UFW)
        self.assertIn("没读到", str(ctx.exception))
        self.assertNotIn("规则表已经变化", str(ctx.exception), "这句话会把人引去反复刷新一个刷不出来的表")
        self.assertNotIn("ufw --force delete 1", p.keys())

    async def test_enable_refuses_when_table_not_read(self):
        """最坏的一步：拿一份读不到的空表当「这些端口没人放行」的判据，然后按 ufw enable。"""
        p = _use(self, {"ufw status verbose": _res(0, UFW_SSH),
                        "ufw status numbered": _res(6, "ERROR: problem running ufw-init")})
        with self.assertRaises(Forbidden):
            await fw.set_enabled(True, fw.UFW)
        self.assertNotIn("ufw --force enable", p.keys(), "启停直接决定 SSH 还进不进得来")

    async def test_firewalld_reload_skipped_when_permanent_not_read(self):
        """写成功了，但 permanent 那份表没读到：只扣下 reload，并说清「写进去了」。"""
        p = _use(self, _fw_answers(**{"firewall-cmd --permanent --list-all": _res(1, "permission denied")}))
        out = await fw.add_rule({"port": 8080}, fw.FIREWALLD)
        self.assertTrue(out["added"])
        self.assertFalse(out["reloaded"])
        self.assertNotIn("firewall-cmd --reload", p.keys())
        self.assertIn("规则已写入", out["detail"])
        self.assertNotIn("这次操作判不了", out["detail"])

    async def test_stopped_firewalld_can_still_be_enabled(self):
        """防火墙停着、permanent 里写好了 SSH 放行：这台机器该能启用，不被「运行期表读不到」挡住。

        这是两个判词分家的另一半——只在一侧分开会修成新的误拒。
        """
        p = _use(self, _fw_answers(**{
            "firewall-cmd --state": _res(252, "not running\n"),
            "firewall-cmd --list-all": _res(252, "FirewallD is not running\n"),
        }))
        out = await fw.set_enabled(True, fw.FIREWALLD)
        self.assertTrue(out["active"])
        self.assertEqual(p.calls[-1][0], "systemctl enable --now firewalld")

    async def test_enable_refuses_when_permanent_has_no_ssh(self):
        _use(self, _fw_answers(**{
            "firewall-cmd --state": _res(252, "not running\n"),
            "firewall-cmd --list-all": _res(252, "FirewallD is not running\n"),
            "firewall-cmd --permanent --list-all": _res(0, FW_EMPTY),
        }))
        with self.assertRaises(Forbidden) as ctx:
            await fw.set_enabled(True, fw.FIREWALLD)
        self.assertIn("22", str(ctx.exception))


# ------------------------------------------------ service 规则读不到端口定义时
class BlindServiceRuleCase(unittest.IsolatedAsyncioTestCase):
    async def _blind_read(self):
        p = _use(self, _fw_answers(**{"firewall-cmd --info-service=ssh": _res(1, "Command 'ssh' is not valid")}))
        out = await fw.read(fw.FIREWALLD)
        self.assertTrue(out["rules_ok"], "表读到了，只是其中一条的端口定义没读到")
        self.assertEqual(fw.unread(out), "")
        rule = [r for r in out["rules"] if r["kind"] == "service"][0]
        self.assertIsNone(rule["ports_known"])
        return p, out, rule

    async def test_blind_rule_is_not_claimed_to_open_the_port(self):
        _p, _out, rule = await self._blind_read()
        self.assertFalse(fw.covers(rule, 22), "一次没成的探测不能拿来宣布「22 外面进得来」")

    async def test_blind_rule_still_blocks_being_deleted(self):
        """待删的那条按最坏情况算：它可能正是这台机器上唯一放行 SSH 的规则。

        原来这里是反的（"宁可少保护一条"），于是唯一放行 SSH 的 ssh service 成了最好删的那条。
        """
        p, _out, rule = await self._blind_read()
        self.assertTrue(rule["protected"], "前端那一格的置灰也来自同一个判据")
        with self.assertRaises(Forbidden) as ctx:
            await fw.remove_rule(rule["num"], rule["sig"], fw.FIREWALLD)
        self.assertIn("22", str(ctx.exception))
        self.assertNotIn("firewall-cmd --permanent --remove-service=ssh", p.keys())

    async def test_blind_rules_are_handed_to_the_exposure_layer(self):
        _p, out, _rule = await self._blind_read()
        self.assertEqual([r["token"] for r in fw.blind_allow_rules(out["rules"])], ["ssh"])

    async def test_a_known_service_without_ports_is_not_blind(self):
        """命令跑成了、定义里就是没有 port 行：这是「这个 service 不开端口」，不是没读到。"""
        _p = _use(self, _fw_answers(**{"firewall-cmd --info-service=ssh": _res(0, "  service\n  port: \n")}))
        out = await fw.read(fw.FIREWALLD)
        rule = [r for r in out["rules"] if r["kind"] == "service"][0]
        self.assertIs(rule["ports_known"], False)
        self.assertEqual(fw.blind_allow_rules(out["rules"]), [])


# ------------------------------------------------------------------- 暴露面对账
ROW = {"port": 3000, "external": True, "addrs": ["0.0.0.0"], "processes": ["node"], "pids": {1}}
PORT_RULE = {"num": 1, "kind": "port", "token": "22/tcp", "action": "allow", "direction": "in",
             "proto": "tcp", "port": 22, "port_to": None, "source": "Anywhere",
             "family": "ipv4", "sig": "22/tcp|ALLOW|any"}
BLIND_RULE = {"num": 1, "kind": "service", "token": "ssh", "action": "allow", "direction": "in",
              "source": "Anywhere", "family": "ipv4", "sig": "service:ssh|ALLOW|any",
              "ports_known": None}


def _state(**over):
    base = {"tool": "ufw", "active": True, "state_ok": True, "rules_ok": True, "reason": "",
            "rules": [dict(PORT_RULE)], "default_incoming": "deny",
            "protected": [22], "protected_reasons": {"22": "sshd 监听端口"}}
    base.update(over)
    return base


class ExposureUnknownCase(unittest.IsolatedAsyncioTestCase):
    async def _reachable(self, **over):
        state = _state(**over)

        async def fake_read(*a, **kw):
            return state

        with mock.patch.object(fw, "read", fake_read):
            _fw, reachable = await ex._firewall()
        return reachable

    async def test_unread_firewall_yields_unknown(self):
        reachable = await self._reachable(active=None, state_ok=False, rules_ok=False,
                                          reason="ufw status verbose 超时")
        ok, why = reachable(3000, ["node"])
        self.assertIsNone(ok)
        self.assertIn("判不了", why)
        verdict, reason, fix = ex._verdict(ROW, ok, why, {}, {})
        self.assertEqual(verdict, ex.UNKNOWN)
        self.assertIn("choyeonctl firewall list", fix)

    async def test_unknown_outranks_blocked(self):
        """排序上「判不了」要排在「被挡住」前面：绿灯队伍里不能夹着一张没判完的口子。"""
        self.assertLess(ex._VERDICT_RANK[ex.UNKNOWN], ex._VERDICT_RANK[ex.BLOCKED])
        self.assertEqual(ex._summary([{"verdict": ex.UNKNOWN}])[ex.UNKNOWN], 1)

    async def test_stopped_firewall_is_reachable_not_unknown(self):
        reachable = await self._reachable(tool="firewalld", active=False, rules_ok=False, rules=[],
                                          reason="firewalld 未运行，运行期规则表读不到")
        ok, why = reachable(3000, ["node"])
        self.assertIs(ok, True, "没设防的机器最该看到的是「可达，请改绑 127.0.0.1」")
        self.assertIn("未启用", why)

    async def test_blind_service_rule_is_not_claimed_blocked(self):
        reachable = await self._reachable(tool="firewalld", rules=[dict(BLIND_RULE)])
        ok, why = reachable(3000, ["node"])
        self.assertIsNone(ok, "不能因为「没有别的规则覆盖 3000」就宣布它被挡住了")
        self.assertIn("service 规则读不到", why)

    async def test_doctor_item_warns_when_everything_is_unknown(self):
        data = {"counts": {"total": 1, ex.UNKNOWN: 1},
                "ports": [{"port": 3000, "verdict": ex.UNKNOWN, "processes": ["node"], "scope": "public"}],
                "firewall": {"tool": "ufw", "active": None, "unread": "ufw status verbose 超时"}}
        item = ex.doctor_item(data)
        self.assertEqual(item["status"], "warn")
        self.assertIn("判不了", item["detail"])
        self.assertEqual(item["fix"], "choyeonctl firewall list")

    async def test_doctor_item_passes_when_nothing_is_external(self):
        data = {"counts": {"total": 1, ex.INTERNAL: 1}, "as_root": True,
                "ports": [{"port": 3000, "verdict": ex.INTERNAL, "processes": ["node"], "scope": "loopback"}],
                "firewall": {"tool": "ufw", "active": True, "unread": ""}}
        self.assertEqual(ex.doctor_item(data)["status"], "pass")


# ------------------------------------------------------ 自检与 CLI 只剩一个读者
class SingleReaderCase(unittest.TestCase):
    def test_self_check_no_longer_probes_firewall_itself(self):
        """`_check_firewall()` 里不许再长出第二个读者：同一台机器不能有两套判据。"""
        src = (BACKEND / "app" / "routers" / "meta.py").read_text(encoding="utf-8")
        body = src[src.index("async def _check_firewall"):src.index("async def _check_backup")]
        self.assertIn("firewall_service.read()", body)
        for banned in ('shutil.which("ufw")', '"ufw", ["status"', '"firewall-cmd", ["--state"]'):
            self.assertNotIn(banned, body, f"自检项里又出现了自己的防火墙探测：{banned}")

    def test_cli_head_names_the_state_failure(self):
        text = cli._firewall_table(_state(active=None, state_ok=False, rules_ok=False,
                                          reason="ufw status verbose 超时"))
        self.assertIn("状态没读到", text)
        self.assertIn("超时", text)
        self.assertIn("规则表没读到", text)
        self.assertNotIn("共 1 条", text)

    def test_cli_footer_does_not_say_zero_rules(self):
        st = _state(rules=[], rules_ok=False, reason="ufw status numbered 退出码 6")
        text = cli._firewall_table(st)
        self.assertIn("这一条不等于「没有放行规则」", text)
        self.assertNotIn("共 0 条", text)

    def test_cli_footer_counts_when_the_table_was_read(self):
        text = cli._firewall_table(_state(tool="none", active=False, rules=[]))
        self.assertIn("共 0 条", text)
        self.assertNotIn("没读到", text)


# ------------------------------------------------ 页面上「没读到」不能退回两格答案
class PageTriStateCase(unittest.TestCase):
    """模板里一个 `st.active ? A : B` 就能把服务层刚立起来的三态吃掉，所以连源码一起钉。"""

    def _web(self, rel: str) -> str:
        return (WEB / rel).read_text(encoding="utf-8")

    def test_firewall_page_reads_the_three_states_separately(self):
        fv = self._web("views/Firewall.vue")
        self.assertIn("s.active === null", fv, "active 为 null 必须单独有说法，不能落进 else 当未启用")
        self.assertIn("s.rules_ok === false", fv, "规则表没读到必须由 rules_ok 决定，不能看 rules 空不空")
        self.assertIn("toggleBlocked", fv)
        self.assertIn("{{ rulesBad ? '没读到' : rules.length }}", fv, "规则条数不许把没读到写成 0")

    def test_firewall_page_no_longer_uses_binary_state_labels(self):
        fv = self._web("views/Firewall.vue")
        for banned in ("st.active ? '运行中' : '未运行'", "st.active ? '已启用' : '未启用'"):
            self.assertNotIn(banned, fv, f"页面上还剩真值两格的写法：{banned}")

    def test_firewall_page_locks_writes_and_names_the_empty_box_reason(self):
        fv = self._web("views/Firewall.vue")
        self.assertIn("<EmptyBox v-if=\"rulesBad\"", fv, "空盒要先分清「读不到」和「真的没有」")
        self.assertIn("这一份表没读到，空表不等于「机器上没有放行规则」", fv)
        self.assertIn("v-if=\"!toggleBlocked\"", fv, "读不到时启停按钮不能只是点了才报错")

    def test_empty_box_can_carry_the_reason(self):
        eb = self._web("components/EmptyBox.vue")
        self.assertIn("hint?: string", eb)
        self.assertIn('class="eb-hint cp-break"', eb, "原因串里全是命令名，窄屏必须能折行")

    def test_exposure_page_has_the_fifth_verdict(self):
        sv = self._web("views/Services.vue")
        self.assertIn("unknown: { text: '判不了'", sv)
        self.assertIn("countOf('unknown')", sv)
        self.assertIn("portData.firewall.unread", sv, "防火墙那颗胶囊要跟着 unread 走")
        # 判不了要说在"被防火墙挡住"之前：绿灯不能由一次没成功的探测发出来
        self.assertLess(sv.index("if (unknownCount.value)"), sv.index("if (blockedCount.value)"))


if __name__ == "__main__":
    unittest.main()
