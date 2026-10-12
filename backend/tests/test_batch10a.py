"""批次⑩：端口暴露面对账（exposure_service）+ 自检项 + `choyeonctl ports`。

这一层的价值全在判词能不能信，所以锁的都是"报错方向"上的语义：
- 未启用防火墙必须判成"非回环端口全都可达"，不能给 unknown：关掉 ufw 就是没有过滤，
  把最该看的机器标成看不了，这一页就白做了；
- 配了域名反代却绑全网的源站是这一页最该抓出来的一条（能绕开 TLS 和限速直连 IP:端口），
  判词必须点名到具体应用；
- 只读：这一层只允许 `ss`，不改绑定、不动防火墙；
- docker 发布端口不经过 ufw 的放行表，"防火墙没放行"对它是假安慰；
- 判不出进程名（非 root）时如实说判定不完整，不拿端口号猜成"未知风险"。
"""

import asyncio
import contextlib
import io
import os
import tempfile
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch10a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch10a_roots_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import exposure_service as ex  # noqa: E402

# 线上 `ss -tlnp` 的真实形态：v4/v6 各一行、systemd-resolved 带 %lo、comm 被截成 15 字符、
# 非 root 时整段 users:() 消失。
SS_OUT = """State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
LISTEN 0      4096   127.0.0.53%lo:53     0.0.0.0:*     users:(("systemd-resolve",pid=812,fd=13))
LISTEN 0      128          0.0.0.0:22     0.0.0.0:*     users:(("sshd",pid=1001,fd=3))
LISTEN 0      511             [::]:80        [::]:*     users:(("nginx",pid=901,fd=6),("nginx",pid=900,fd=6))
LISTEN 0      4096     127.0.0.1:5432     127.0.0.1:*   users:(("postgres",pid=1200,fd=6))
LISTEN 0      511        0.0.0.0:3100     0.0.0.0:*     users:(("next-server (v1",pid=1500,fd=18))
LISTEN 0      128            [::]:22        [::]:*      users:(("sshd",pid=1001,fd=4))
LISTEN 0      10         10.0.0.5:8787    0.0.0.0:*
ESTAB  0      0        127.0.0.1:5432     127.0.0.1:40001 users:(("postgres",pid=1200,fd=9))
"""

SS_NO_PIDS = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0      511        0.0.0.0:9000     0.0.0.0:*
"""


def _fw(tool="ufw", active=True, default_in="deny", rules=None):
    return {
        "tool": tool,
        "active": active,
        "default_incoming": default_in,
        "rules": rules if rules is not None else [],
        "protected": [22],
        "protected_reasons": {"22": "sshd 监听端口"},
        "output": "",
    }


def _allow(port):
    """一条ufw 形态的放行规则（covers() 吃得下的最小字段）。"""
    return {"action": "allow", "direction": "in", "source": "Anywhere", "port": port,
            "port_to": port, "proto": "tcp", "ports_known": True, "port_specs": [(port, port, "tcp")]}


def _audit(ss=SS_OUT, fw=None, apps=(), protected=None, code=0):
    """跑一次探测，返回 (数据, 调用过的命令)。缓存必须清掉：同一进程里连着调多次
    _audit 时，第二次拿到上一次的结论就是在断言上一个发行版。
    """
    fw = fw or _fw()
    protected = protected or ({22: "x"}, {22: "sshd 监听端口"})
    calls = []

    async def fake_run(cmd, args, cwd=None, timeout=120, env=None):
        calls.append(f"{cmd} {' '.join(args)}")
        return {"code": code, "out": ss, "err": ""}

    ex._cache["data"] = None
    ex._cache["at"] = 0.0
    with mock.patch.object(ex, "run", fake_run), \
         mock.patch.object(ex.firewall_service, "read", mock.AsyncMock(return_value=fw)), \
         mock.patch.object(ex.firewall_service, "protected_ports", lambda request=None: protected), \
         mock.patch.object(ex.dbm, "query", lambda sql, params=(): list(apps)), \
         mock.patch.object(ex.shutil, "which", lambda n: "/usr/bin/ss"):
        data = asyncio.run(ex.audit(True))
    return data, calls


def _port(data, port):
    return next(p for p in data["ports"] if p["port"] == port)


class BindScopeTests(unittest.TestCase):
    def test_wildcards_and_loopback(self):
        for host in ("*", "0.0.0.0", "::", "[::]"):
            self.assertEqual(ex._bind_scope(host), "wildcard", host)
        for host in ("127.0.0.1", "::1", "localhost", "127.0.0.53%lo"):
            self.assertEqual(ex._bind_scope(host), "loopback", host)

    def test_link_local_is_not_public(self):
        # fe80:: 路由不出这一条链路，和 127. 同档处理。
        self.assertEqual(ex._bind_scope("fe80::1%eth0"), "link-local")
        self.assertEqual(ex._bind_scope("10.0.0.5"), "public-ip")


class ParseSsTests(unittest.TestCase):
    def test_merges_v4_and_v6_by_port(self):
        rows = ex.parse_ss(SS_OUT)
        sshd = next(r for r in rows if r["port"] == 22)
        self.assertEqual(sshd["addrs"], ["0.0.0.0:22", "[::]:22"])
        self.assertEqual(sshd["scope"], "wildcard")
        self.assertEqual(sshd["pids"], [1001])

    def test_only_listen_lines_count(self):
        ports = {r["port"] for r in ex.parse_ss(SS_OUT)}
        self.assertNotIn(40001, ports)  # ESTAB 那行不是监听
        self.assertEqual(ports, {22, 53, 80, 3100, 5432, 8787})

    def test_missing_users_yields_no_processes(self):
        row = ex.parse_ss(SS_NO_PIDS)[0]
        self.assertEqual(row["processes"], [])
        self.assertEqual(row["pids"], [])

    def test_junk_lines_are_dropped(self):
        self.assertEqual(ex.parse_ss("garbage\n\nLISTEN 0 0 :::: 0\n"), [])

    def test_ports_are_sorted(self):
        rows = ex.parse_ss(SS_OUT)
        self.assertEqual([r["port"] for r in rows], sorted(r["port"] for r in rows))


class VerdictTests(unittest.TestCase):
    def test_loopback_is_internal(self):
        data, _ = _audit()
        self.assertEqual(_port(data, 5432)["verdict"], ex.INTERNAL)
        self.assertIn("只监听本机", _port(data, 5432)["reason"])

    def test_wildcard_v6_and_v4_makes_a_port_external(self):
        data, _ = _audit()
        self.assertTrue(_port(data, 22)["external"])

    def test_protected_ports_are_expected(self):
        data, _ = _audit()
        self.assertEqual(_port(data, 22)["verdict"], ex.EXPECTED)

    def test_web_daemon_on_80_is_expected(self):
        # 不这么处理的话，任何一台跑站点的机器第一条就是假警报。
        data, _ = _audit()
        self.assertEqual(_port(data, 80)["verdict"], ex.EXPECTED)

    def test_unknown_public_port_is_exposed(self):
        data, _ = _audit(fw=_fw(rules=[_allow(22), _allow(80), _allow(443), _allow(3100)]))
        p = _port(data, 3100)
        self.assertEqual(p["verdict"], ex.EXPOSED)
        self.assertIn("放行", p["reason"])
        self.assertIn("127.0.0.1", p["fix"])

    def test_no_firewall_means_reachable_not_unknown(self):
        data, _ = _audit(fw=_fw(active=False, tool="none"))
        p = _port(data, 3100)
        self.assertTrue(p["reachable"])
        self.assertEqual(p["verdict"], ex.EXPOSED)

    def test_blocked_when_firewall_denies(self):
        data, _ = _audit()  # 默认没有任何放行规则
        p = _port(data, 3100)
        self.assertEqual(p["verdict"], ex.BLOCKED)
        self.assertFalse(p["reachable"])

    def test_default_allow_incoming_opens_everything(self):
        data, _ = _audit(fw=_fw(default_in="allow"))
        self.assertEqual(_port(data, 3100)["verdict"], ex.EXPOSED)

    def test_docker_proxy_bypasses_ufw_table(self):
        ss = 'LISTEN 0 0 0.0.0.0:3306 0.0.0.0:* users:(("docker-proxy",pid=77,fd=4))\n'
        data, _ = _audit(ss=ss)
        p = _port(data, 3306)
        self.assertTrue(p["reachable"])
        self.assertIn("DNAT", p["reason"])

    def test_app_with_domain_bound_public_is_exposed_by_name(self):
        apps = [{"id": 3, "name": "choyeon-video", "port": 3100, "domain": "v.choyeon.cc"}]
        data, _ = _audit(apps=apps)
        p = _port(data, 3100)
        self.assertEqual(p["verdict"], ex.EXPOSED)
        self.assertIn("choyeon-video", p["reason"])
        self.assertIn("绕", p["reason"])

    def test_link_local_protocol_ports_are_internal(self):
        ss = 'LISTEN 0 0 0.0.0.0:5355 0.0.0.0:* users:(("systemd-resolve",pid=812,fd=12))\n'
        data, _ = _audit(ss=ss)
        self.assertEqual(_port(data, 5355)["verdict"], ex.INTERNAL)

    def test_specific_public_ip_counts_as_external(self):
        data, _ = _audit()
        p = _port(data, 8787)
        self.assertEqual(p["scope"], "public-ip")
        self.assertTrue(p["external"])


class AuditShapeTests(unittest.TestCase):
    def test_only_ss_is_executed(self):
        _, calls = _audit()
        self.assertEqual(calls, ["ss -tlnp"])

    def test_as_root_flag_tracks_pid_visibility(self):
        data, _ = _audit(ss=SS_NO_PIDS)
        self.assertFalse(data["as_root"])
        data, _ = _audit()
        self.assertTrue(data["as_root"])

    def test_scope_note_is_always_present(self):
        # 云上安全组不在本机视野里，这句话每次都得跟着结果走，不能只写在文档里。
        data, _ = _audit()
        self.assertTrue(any("安全组" in n for n in data["notes"]))

    def test_counts_match_ports(self):
        data, _ = _audit()
        c = data["counts"]
        self.assertEqual(c["total"], len(data["ports"]))
        self.assertEqual(
            c["internal"] + c["expected"] + c["blocked"] + c["exposed"], c["total"]
        )

    def test_risky_verdicts_sort_first(self):
        data, _ = _audit(fw=_fw(rules=[_allow(22), _allow(80), _allow(443), _allow(3100)]))
        self.assertEqual(data["ports"][0]["verdict"], ex.EXPOSED)

    def test_apps_cross_check_row(self):
        apps = [{"id": 1, "name": "api", "port": 8000, "domain": ""}]
        data, _ = _audit(apps=apps)
        row = data["apps"][0]
        self.assertEqual(row["name"], "api")
        self.assertFalse(row["listening"])
        self.assertIsNone(row["verdict"])

    def test_db_failure_does_not_break_the_audit(self):
        def boom(sql, params=()):
            raise RuntimeError("no such table: apps")

        fw = _fw()
        ex._cache["data"] = None
        ex._cache["at"] = 0.0
        with mock.patch.object(ex, "run", mock.AsyncMock(return_value={"code": 0, "out": SS_OUT, "err": ""})), \
             mock.patch.object(ex.firewall_service, "read", mock.AsyncMock(return_value=fw)), \
             mock.patch.object(ex.firewall_service, "protected_ports", lambda request=None: ({22}, {22: "sshd"})), \
             mock.patch.object(ex.dbm, "query", boom), \
             mock.patch.object(ex.shutil, "which", lambda n: "/usr/bin/ss"):
            data = asyncio.run(ex.audit(True))
        self.assertEqual(data["apps"], [])
        self.assertTrue(data["ports"])

    def test_cache_serves_then_refresh_bypasses(self):
        fw = _fw()
        read = mock.AsyncMock(return_value=fw)
        run = mock.AsyncMock(return_value={"code": 0, "out": SS_OUT, "err": ""})
        ex._cache["data"] = None
        ex._cache["at"] = 0.0
        with mock.patch.object(ex, "run", run), \
             mock.patch.object(ex.firewall_service, "read", read), \
             mock.patch.object(ex.firewall_service, "protected_ports", lambda request=None: ({22}, {22: "sshd"})), \
             mock.patch.object(ex.dbm, "query", lambda sql, params=(): []), \
             mock.patch.object(ex.shutil, "which", lambda n: "/usr/bin/ss"):
            first = asyncio.run(ex.audit(True))
            run.assert_awaited()
            run.reset_mock()
            cached = asyncio.run(ex.audit())
            self.assertTrue(cached["cached"])
            self.assertEqual(run.await_count, 0)
            self.assertFalse(asyncio.run(ex.audit(True))["cached"])
        self.assertTrue(first["ports"])


class DoctorItemTests(unittest.TestCase):
    def test_exposed_is_warn_with_cli_pointer(self):
        data, _ = _audit(fw=_fw(rules=[_allow(22), _allow(80), _allow(443), _allow(3100)]))
        item = ex.doctor_item(data)
        self.assertEqual(item["status"], "warn")
        self.assertIn("3100", item["detail"])
        self.assertEqual(item["fix"], "choyeonctl ports")

    def test_blocked_only_is_pass_but_says_so(self):
        data, _ = _audit()
        item = ex.doctor_item(data)
        self.assertEqual(item["status"], "pass")
        self.assertIn("防火墙", item["detail"])

    def test_empty_table_is_warn_not_pass(self):
        # 读不到监听表时"没有风险"是探测失败的伪装。
        item = ex.doctor_item({"ports": [], "counts": {}, "as_root": True})
        self.assertEqual(item["status"], "warn")

    def test_non_root_discloses_incomplete_judgement(self):
        data, _ = _audit(ss=SS_NO_PIDS)
        item = ex.doctor_item(data)
        self.assertIn("非 root", item["detail"])

    def test_many_exposed_ports_are_capped_in_detail(self):
        ports = [9000 + i for i in range(9)]
        ss = "".join(
            f'LISTEN 0 0 0.0.0.0:{p} 0.0.0.0:* users:(("app{i}",pid={i},fd=1))\n'
            for i, p in enumerate(ports)
        )
        data, _ = _audit(ss=ss, fw=_fw(rules=[_allow(p) for p in ports]))
        item = ex.doctor_item(data)
        self.assertEqual(item["status"], "warn")
        self.assertIn("等 9 个", item["detail"])


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


AUDIT_DATA = {
    "ok": True,
    "cached": False,
    "as_root": True,
    "firewall": {"tool": "ufw", "active": True, "default_incoming": "deny"},
    "counts": {"total": 2, "internal": 1, "expected": 0, "blocked": 0, "exposed": 1},
    "ports": [
        {"port": 3100, "addrs": ["*:3100"], "scope": "wildcard", "external": True,
         "processes": ["node"], "pids": [15], "reachable": True, "verdict": ex.EXPOSED,
         "reason": "node 监听在 *:3100，外部可达", "fix": "改绑 127.0.0.1"},
        {"port": 5432, "addrs": ["127.0.0.1:5432"], "scope": "loopback", "external": False,
         "processes": ["postgres"], "pids": [9], "reachable": False, "verdict": ex.INTERNAL,
         "reason": "只监听本机地址，外部碰不到", "fix": ""},
    ],
    "apps": [],
    "notes": ["判定只看本机防火墙"],
    "checked_at": "2026-10-12T13:00:00+00:00",
}


class CliPortsTests(unittest.TestCase):
    def setUp(self):
        cli.Out.json_mode = False

    def tearDown(self):
        cli.Out.json_mode = False

    def test_ports_parser_takes_only_refresh(self):
        strings = set()
        for action in cli.build_parser()._subparsers._group_actions[0].choices["ports"]._actions:
            if action.option_strings and action.option_strings[0] != "-h":
                strings.add(action.option_strings[0])
        self.assertEqual(strings, {"--refresh", "--json"})

    def test_refresh_flag_reaches_the_service(self):
        audit = mock.AsyncMock(return_value=AUDIT_DATA)
        with mock.patch.object(ex, "audit", audit):
            _run(cli.cmd_ports(_args("ports")))
            self.assertFalse(audit.await_args.kwargs["refresh"])
            _run(cli.cmd_ports(_args("ports", "--refresh")))
            self.assertTrue(audit.await_args.kwargs["refresh"])

    def test_text_output_names_the_risky_port(self):
        with mock.patch.object(ex, "audit", mock.AsyncMock(return_value=AUDIT_DATA)):
            _, out, _, code = _run(cli.cmd_ports(_args("ports")))
        self.assertEqual(code, 0)
        self.assertIn("3100", out)
        self.assertIn("[exposed]", out)
        self.assertNotIn("5432", out)  # 仅本机监听的不占屏
        self.assertIn("改绑 127.0.0.1", out)

    def test_empty_table_is_exit_code_3(self):
        empty = {**AUDIT_DATA, "ports": [], "counts": {"total": 0}}
        with mock.patch.object(ex, "audit", mock.AsyncMock(return_value=empty)):
            _, _, _, code = _run(cli.cmd_ports(_args("ports")))
        self.assertEqual(code, 3)

    def test_schema_lists_the_command(self):
        data, *_ = _run(cli.cmd_schema(_args("schema")))
        self.assertIn("ports", [c["name"] for c in data["commands"]])


class MetaWiringTests(unittest.TestCase):
    def test_ports_is_a_registered_doctor_item(self):
        # 只认这一项在不在、叫什么：批次⑪在后面加了「登录安全」，位置不该被这一层钉死。
        ids = [c[0] for c in meta._CHECKS]
        self.assertIn("ports", ids)
        self.assertEqual(meta._CHECKS[ids.index("ports")][1], "端口暴露面")

    def test_ports_route_is_registered(self):
        self.assertIn("/api/ports", [r.path for r in meta.router.routes])

    def test_agents_doc_claims_the_real_item_count(self):
        want = f"当前 {len(meta._CHECKS)} 项"
        with open(os.path.join(cli.ROOT, "AGENTS.md"), encoding="utf-8") as fh:
            text = fh.read()
        # assertTrue 而不是 assertIn：后者失败会把整份 AGENTS.md 打进日志，没人看得完。
        self.assertTrue(want in text, f"AGENTS.md 应写着 {want!r}")


if __name__ == "__main__":
    unittest.main()
