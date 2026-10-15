"""批次⑧A：防火墙写路径的回归测试。

锁定的语义都来自接管线上防火墙时的实测，不是照文档想象的行为：
- 规则表必须结构化（编号 / 动作 / 来源 / 端口区间 / v4 还是 v6），前端才置得了灰、删除前的
  签名比对才成立；ufw 的 `LIMIT IN` 也是动作列，曾被当成来源解析；limit 本质是带限速的 accept，
  删掉它 SSH 就真的不通，所以自锁判定必须把它算作放行；
- 受保护端口 = sshd 实际监听（含 Include 片段）+ 反代到面板的 nginx 入口 + 当前会话的到达端口；
  删/拒这些端口一律拒绝，而且没有"我已确认，继续执行"这种绕过参数；
- `ufw delete N` 不带 --force 时打印 "Proceed with operation (y|n)? Aborted" 而退出码仍是 0，
  所以删除既要用 --force，也要删完重新列表确认——只看返回码会把"什么都没删掉"报成成功；
- 编号会在两次请求之间移动（删一条，后面的全部前移），DELETE 必须带 expect 签名比对；
- v4/v6 是两条独立规则，签名必须不同，否则"核对签名"形同虚设。
"""

import copy
import os
import tempfile
import types
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch8a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch8a_roots_"))

import unittest  # noqa: E402

from app import config, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.errors import BadInput, ExecFailed, Forbidden  # noqa: E402
from app.services import firewall_service as fw  # noqa: E402

UFW_NUM = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 443/tcp (v6)               ALLOW IN    Anywhere (v6)
[ 3] 8000:8100/tcp              ALLOW IN    10.0.0.0/8              # office vpn
[ 4] Anywhere                   DENY OUT    203.0.113.5             # ban
[ 5] 57998/tcp                  LIMIT IN    Anywhere
"""

FW_LIST = """  target: default
  services: ssh cockpit
  ports: 8000/tcp 9000-9100/udp
  rich rules:
    rule family="ipv4" source address="10.0.0.0/8" port port="8443" protocol="tcp" accept
    rule family="ipv6" source address="2001:db8::/32" port port="9999" protocol="udp" drop
"""

RICH_V4 = 'rule family="ipv4" source address="10.0.0.0/8" port port="8443" protocol="tcp" accept'


class FakeRequest:
    """够用的 Request 替身：判定只用 headers 与 client.host。"""

    def __init__(self, headers=None, host="203.0.113.9"):
        self.headers = headers or {}
        self.client = types.SimpleNamespace(host=host)


def _to(port, port_to=None, proto="tcp", family="ipv4"):
    body = f"{port}:{port_to}" if port_to else str(port)
    spec = body if proto == "both" else f"{body}/{proto}"
    return f"{spec} (v6)" if family == "ipv6" else spec


def _rule(port=None, port_to=None, proto="tcp", action="allow", source="Anywhere", family="ipv4",
          direction="in", comment="", raw=None):
    return {
        "to": raw if raw is not None else _to(port, port_to, proto, family),
        "action": action,
        "source": source,
        "family": family,
        "direction": direction,
        "comment": comment,
    }


class UfwMachine:
    """ufw 的进程替身：状态表由内部规则列表渲染，删除只认真正执行了的那条命令。

    两处真实行为是故意保留的，它们正是产品缺陷的来源：不带 --force 的 delete 回 rc=0 却什么都
    不删；没有来源的 allow 会同时生成 v4 与 v6 两行，编号列表里各占一号。
    """

    def __init__(self, rules=None, active=True):
        self.rules = copy.deepcopy(rules if rules is not None else [_rule(port=22)])
        self.active = active
        self.calls = []
        self.fail_with = None

    def render(self):
        lines = [
            "Status: active" if self.active else "Status: inactive",
            "Logging: on (low)",
            "Default: deny (incoming), allow (outgoing), disabled (routed)",
            "New profiles: skip",
            "",
        ]
        for i, r in enumerate(self.rules, 1):
            act = f'{r["action"].upper()} {r["direction"].upper()}'
            src = f'{r["source"]} (v6)' if r["family"] == "ipv6" and r["source"] == "Anywhere" else r["source"]
            tail = f'  # {r["comment"]}' if r["comment"] else ""
            lines.append(f'[{i:>2}] {r["to"]:<28} {act:<9}  {src:<28}{tail}')
        return "\n".join(lines)

    def table(self):
        return fw.parse_ufw(self.render())

    def find(self, port):
        return [x for x in self.table() if x["port"] == port]

    async def run(self, cmd, args, cwd=None, timeout=120, env=None):
        self.calls.append((cmd, list(args)))
        if cmd != "ufw":
            raise AssertionError(f"ufw 替身收到了别的命令: {cmd} {args}")
        if args[:2] in (["status", "verbose"], ["status", "numbered"]):
            return {"code": 0, "out": self.render()}
        if args[:1] == ["delete"]:
            return {"code": 0, "out": "Proceed with operation (y|n)? Aborted"}
        if args[:2] == ["--force", "delete"]:
            idx = int(args[2]) - 1
            if not 0 <= idx < len(self.rules):
                return {"code": 1, "out": f"Could not find rule with specification {args[2]}"}
            gone = self.rules.pop(idx)
            return {"code": 0, "out": "Rule deleted" + (" (v6)" if gone["family"] == "ipv6" else "")}
        if args[:2] == ["--force", "enable"]:
            self.active = True
            return {"code": 0, "out": "Firewall is active and enabled on system startup"}
        if args[:2] == ["--force", "disable"]:
            self.active = False
            return {"code": 0, "out": "Firewall stopped and disabled on system startup"}
        if args[:1] == ["enable"]:
            return {"code": 0, "out": "Proceed with operation (y|n)? Aborted"}
        if args[:1] in (["allow"], ["deny"], ["limit"]):
            if self.fail_with:
                return {"code": 1, "out": self.fail_with}
            verb, body = args[0], list(args[1:])
            comment = ""
            if "comment" in body:
                comment = body[body.index("comment") + 1]
                body = body[: body.index("comment")]
            if body[:1] == ["from"]:
                source = body[1]
                spec = body[body.index("port") + 1]
                proto = body[body.index("proto") + 1] if "proto" in body else "both"
                twinned = 1
            else:
                head, _, tail = body[0].rpartition("/")
                spec, proto = (body[0], "both") if tail not in ("tcp", "udp") else (head, tail)
                source, twinned = "Anywhere", 2
            to = spec if proto == "both" else f"{spec}/{proto}"
            made = [{"to": to, "action": verb, "source": source, "family": "ipv4",
                     "direction": "in", "comment": comment}]
            if twinned == 2:
                made.append({**made[0], "to": f"{to} (v6)", "family": "ipv6"})
            self.rules.extend(made)
            return {"code": 0, "out": "\n".join(["Rule added"] * twinned)}
        raise AssertionError(f"ufw 替身收到了未模拟的命令: {args}")


def _zone(spec):
    base = {"services": [], "ports": [], "rich": []}
    for key, value in (spec or {}).items():
        base[key] = list(value)
    return base


class FirewalldMachine:
    """firewall-cmd 的替身：permanent 与 runtime 两份配置，--reload 用前者覆盖后者。

    这正是"把 permanent 里最后一条 ssh 删掉后立刻 reload"会锁死的机制。两份配置必须分开存，
    否则测不到那条 reload 危险分支。
    """

    def __init__(self, runtime=None, permanent=None, running=True):
        self.rt = _zone(runtime)
        self.pm = _zone(permanent)
        self.running = running
        self.calls = []
        self.fail_with = None
        self.reload_after = None

    @staticmethod
    def _render(zone):
        lines = ["  target: default", "  services: " + " ".join(zone["services"]),
                 "  ports: " + " ".join(zone["ports"]), "  rich rules:"]
        return "\n".join(lines + [f"    {raw}" for raw in zone["rich"]])

    async def run(self, cmd, args, cwd=None, timeout=120, env=None):
        self.calls.append((cmd, list(args)))
        if cmd == "systemctl":
            self.running = "enable" in args
            return {"code": 0, "out": ""}
        if cmd != "firewall-cmd":
            raise AssertionError(f"firewalld 替身收到了别的命令: {cmd} {args}")
        if args == ["--state"]:
            return {"code": 0, "out": ("running\n" if self.running else "not running\n")}
        if args == ["--get-default-zone"]:
            return {"code": 0, "out": "public\n"}
        if args == ["--list-all"]:
            return {"code": 0, "out": self._render(self.rt)}
        if args == ["--permanent", "--list-all"]:
            return {"code": 0, "out": self._render(self.pm)}
        if args and args[-1].startswith("--info-service="):
            # permanent 那份表也要问 service 开哪些端口：`["--permanent", "--info-service=ssh"]`
            # 走的是同一条分支，否则替身会把它当成一次 --permanent 写操作，把 ssh 从配置里抹掉。
            port = {"ssh": "22/tcp", "cockpit": "9090/tcp"}.get(args[-1].split("=", 1)[1], "")
            return {"code": 0, "out": f"  service\n  port: {port}\n"}
        if args == ["--reload"]:
            if self.reload_after:
                out, self.fail_with = self.reload_after, None
                self.reload_after = None
                return {"code": 1, "out": out}
            self.rt = copy.deepcopy(self.pm)
            return {"code": 0, "out": "success\n"}
        if len(args) == 2 and args[0] == "--permanent":
            if self.fail_with:
                return {"code": 1, "out": self.fail_with}
            flag, _, token = args[1].partition("=")
            bucket = "rich" if flag.endswith("rich-rule") else ("services" if flag.endswith("service") else "ports")
            if flag.startswith("--add"):
                self.pm[bucket].append(token)
                self.rt[bucket].append(token)
            else:
                self.pm[bucket] = [x for x in self.pm[bucket] if x != token]
            return {"code": 0, "out": "success\n"}
        raise AssertionError(f"firewalld 替身收到了未模拟的命令: {args}")


def _pin_protected(test):
    """把受保护端口钉死成 {22}：sshd 配置与 nginx 扫描都指向空的临时目录。"""
    tmp = tempfile.TemporaryDirectory(prefix="cp_batch8a_")
    test.addCleanup(tmp.cleanup)
    test.tmp = tmp
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
    return tmp


def _write_nginx(test, name, text):
    base = Path(test.tmp.name, "nginx")
    base.mkdir(exist_ok=True)
    (base / name).write_text(text, encoding="utf-8")


def _use_ufw(test, rules=None, active=True):
    m = UfwMachine(
        rules if rules is not None else [_rule(port=22), _rule(port=22, family="ipv6")],
        active=active,
    )
    patch = mock.patch.object(fw, "run", m.run)
    patch.start()
    test.addCleanup(patch.stop)
    return m


def _use_firewalld(test, runtime=None, permanent=None, running=True):
    m = FirewalldMachine(
        runtime=runtime if runtime is not None else {"services": ["ssh"]},
        permanent=permanent if permanent is not None else {"services": ["ssh"]},
        running=running,
    )
    patch = mock.patch.object(fw, "run", m.run)
    patch.start()
    test.addCleanup(patch.stop)
    return m


# --------------------------------------------------------------------- 解析
class ParseUfwCase(unittest.TestCase):
    def test_structured_columns_and_actions(self):
        rules = fw.parse_ufw(UFW_NUM)
        self.assertEqual([r["num"] for r in rules], [1, 2, 3, 4, 5])
        by_num = {r["num"]: r for r in rules}
        self.assertEqual((by_num[1]["port"], by_num[1]["proto"], by_num[1]["action"]), (22, "tcp", "allow"))
        self.assertEqual((by_num[2]["family"], by_num[2]["source"]), ("ipv6", "Anywhere"))
        self.assertEqual((by_num[3]["port"], by_num[3]["port_to"], by_num[3]["source"]), (8000, 8100, "10.0.0.0/8"))
        self.assertEqual(by_num[3]["comment"], "office vpn")
        self.assertEqual((by_num[4]["action"], by_num[4]["direction"], by_num[4]["port"]), ("deny", "out", None))
        # 回归：LIMIT IN 曾被当成来源解析，来源变成 "LIMIT IN Anywhere"
        self.assertEqual((by_num[5]["action"], by_num[5]["source"]), ("limit", "Anywhere"))

    def test_v4_v6_twins_have_different_signatures(self):
        sigs = [r["sig"] for r in fw.parse_ufw(UFW_NUM)]
        self.assertEqual(len(sigs), len(set(sigs)), "同一条规则的 v4/v6 签名相同，expect 比对就白做了")

    def test_limit_counts_as_opening_the_port_and_deny_out_does_not(self):
        rules = fw.parse_ufw(UFW_NUM)
        self.assertTrue(fw.covers(rules[4], 57998))
        self.assertFalse(fw.covers(rules[3], 22))


class ParseFirewalldCase(unittest.TestCase):
    def test_services_ports_and_rich_rules(self):
        rules = fw.parse_firewalld(FW_LIST, "runtime")
        self.assertEqual([r["kind"] for r in rules], ["service", "service", "port", "port", "rich", "rich"])
        by_token = {r["token"]: r for r in rules}
        self.assertEqual(by_token["ssh"]["raw"], "service:ssh")
        self.assertEqual((by_token["9000-9100/udp"]["port"], by_token["9000-9100/udp"]["port_to"]), (9000, 9100))
        rich = by_token[RICH_V4]
        self.assertEqual((rich["source"], rich["port"], rich["action"], rich["family"]), ("10.0.0.0/8", 8443, "allow", "ipv4"))
        drop = [r for r in rules if r["action"] == "deny"][0]
        self.assertEqual((drop["port"], drop["family"], drop["proto"]), (9999, "ipv6", "udp"))

    def test_every_rich_line_is_parsed(self):
        """回归：第一版只按 `key:` 结构解析，富规则的缩进行整段漏掉。"""
        self.assertEqual(len(fw.parse_firewalld(FW_LIST)), 6)
        self.assertEqual([r["token"] for r in fw.parse_firewalld("  services: ssh\n")], ["ssh"])
        self.assertEqual(fw.parse_firewalld("  interfaces: eth0\n"), [])


class EnrichServiceCase(unittest.IsolatedAsyncioTestCase):
    async def test_service_ports_are_translated(self):
        m = FirewalldMachine(runtime={"services": ["ssh"]})
        with mock.patch.object(fw, "run", m.run):
            rules = fw.parse_firewalld("  services: ssh\n")
            await fw._enrich_service_ports(rules)
        self.assertTrue(rules[0]["ports_known"])
        self.assertEqual(rules[0]["port_specs"], [(22, None, "tcp")])
        self.assertTrue(fw.covers(rules[0], 22))

    async def test_unknown_service_claims_nothing(self):
        m = FirewalldMachine(runtime={"services": ["weird"]})
        with mock.patch.object(fw, "run", m.run):
            rules = fw.parse_firewalld("  services: weird\n")
            await fw._enrich_service_ports(rules)
        self.assertFalse(rules[0]["ports_known"])
        self.assertFalse(fw.covers(rules[0], 22), "查不到端口定义的 service 不能假装它放行全部")


# --------------------------------------------------------------------- 校验
class ValidateRuleCase(unittest.TestCase):
    def test_normalizes_source_and_accepts_numeric_strings(self):
        rule = fw.validate_rule({"port": "8000", "port_to": "8010", "source": "10.0.0.5/16", "comment": "办公网"})
        self.assertEqual((rule["port"], rule["port_to"], rule["source"], rule["comment"]), (8000, 8010, "10.0.0.0/16", "办公网"))

    def test_anywhere_source_becomes_empty(self):
        for value in ("Anywhere", "any", "", None):
            self.assertEqual(fw.validate_rule({"port": 22, "source": value})["source"], "")

    def test_rejection_matrix(self):
        bad = [
            ({"action": "reject", "port": 22}, "action"),
            ({"port": 22, "proto": "icmp"}, "proto"),
            ({"port": 22, "rate": True, "action": "deny"}, "限速"),
            ({"port": "22; rm -rf /"}, "整数"),
            ({"port": 0}, "范围"),
            ({"port": 70000}, "范围"),
            ({"port_to": 9000}, "同时填开始端口"),
            ({"port": 9000, "port_to": 8000}, "不合法"),
            ({"port": 1, "port_to": 20000}, "端口区间最多跨"),
            ({"port": 22, "source": "evil.example.com"}, "不接受域名"),
            ({"port": 22, "source": "0.0.0.0/0"}, "覆盖过广"),
            ({"port": 22, "source": "10.0.0.0/7"}, "覆盖过广"),
            ({"port": 22, "comment": "-list-all"}, "命令选项"),
            ({"port": 22, "comment": "a" * 41}, "最多 40"),
            ({"port": 22, "comment": "备注`id`"}, "只能使用中英文"),
            ({}, "请填写要放行的端口"),
        ]
        for body, hint in bad:
            with self.subTest(body=body):
                with self.assertRaises(BadInput) as cm:
                    fw.validate_rule(body)
                self.assertIn(hint, str(cm.exception))

    def test_bool_is_not_a_port(self):
        with self.assertRaises(BadInput):
            fw.validate_rule({"port": True})


# --------------------------------------------------------------------- 受保护端口
class ProtectedPortsCase(unittest.TestCase):
    def setUp(self):
        _pin_protected(self)
    def test_sshd_include_dir_is_followed(self):
        base = Path(self.tmp.name, "sshd_config.d")
        base.mkdir()
        (base / "10-port.conf").write_text("Port 22222\n#Port 22\n", encoding="utf-8")
        with mock.patch.object(fw, "SSHD_CONFIG", str(base.parent / "sshd_config")):
            Path(self.tmp.name, "sshd_config").write_text(f"Include {base}/*.conf\n", encoding="utf-8")
            ports, reasons = fw.protected_ports()
        self.assertEqual(ports, {22222})
        self.assertEqual(reasons[22222], "sshd 监听端口")

    def test_sshd_without_port_lines_falls_back_to_22(self):
        Path(self.tmp.name, "sshd_config").write_text("# 没有 Port 行\n", encoding="utf-8")
        self.assertEqual(fw.protected_ports()[0], {22})

    def test_panel_port_is_protected_only_when_not_loopback(self):
        self.assertEqual(fw.protected_ports()[0], {22}, "回环绑定时把面板端口列进保护会凭空挡住一次正常启用")
        with mock.patch.object(config, "HOST", "0.0.0.0"):
            ports, reasons = fw.protected_ports()
        self.assertEqual(ports, {22, config.PORT})
        self.assertEqual(reasons[config.PORT], "面板自身端口")

    def test_arrival_ports_from_headers(self):
        cases = [
            (FakeRequest({"origin": "https://admin.example.cc"}), {22, 443}),
            (FakeRequest({"origin": "https://admin.example.cc:8443"}), {22, 8443}),
            (FakeRequest({"referer": "http://admin.example.cc:3210/apps"}), {22, 3210}),
            (FakeRequest({"x-forwarded-host": "admin.example.cc:8443"}), {22, 8443}),
            (FakeRequest({"host": "admin.example.cc:3210"}, host="127.0.0.1"), {22}),
            (FakeRequest({"host": "admin.example.cc:3210"}, host="203.0.113.9"), {22, 3210}),
            (FakeRequest(), {22}),
        ]
        for req, expected in cases:
            with self.subTest(headers=req.headers):
                self.assertEqual(fw.protected_ports(req)[0], expected)

    def test_nginx_reverse_proxy_entry_is_protected(self):
        """没有 Origin 头的调用（CLI、脚本）也要挡住删 443——那是面板自己的门。"""
        _write_nginx(self,
            "panel.conf",
            "server {\n  location / {\n    proxy_pass http://127.0.0.1:3210;\n  }\n"
            "  listen 443 ssl; # managed by Certbot\n  listen [::]:443 ssl;\n}\n"
            "server {\n  listen 80;\n  return 301 https://$host$request_uri;\n}\n",
        )
        _write_nginx(self,
            "other.conf",
            "server {\n  location / {\n    proxy_pass http://127.0.0.1:32100;\n  }\n  listen 8444;\n}\n"
            "server {\n  location /api {\n    proxy_pass http://backend_pool;\n  }\n  listen 8443;\n}\n"
            "server {\n  listen 8080;\n  root /var/www;\n}\n",
        )
        self.assertEqual(fw.panel_entry_ports(), {443})
        ports, reasons = fw.protected_ports()
        self.assertEqual(ports, {22, 443})
        self.assertEqual(reasons[443], "反代到面板的 nginx 入口端口")

    def test_nginx_localhost_form_and_nested_blocks(self):
        _write_nginx(self,
            "panel.conf",
            "upstream pool { server 127.0.0.1:3211; }\n"
            "server {\n  listen 9443 ssl;\n  location / {\n    proxy_pass http://localhost:3210;\n  }\n}\n",
        )
        self.assertEqual(fw.panel_entry_ports(), {9443})

    def test_missing_nginx_is_not_an_error(self):
        self.assertEqual(fw.panel_entry_ports(), set())


# --------------------------------------------------------------------- 自锁判定
class LockoutCase(unittest.TestCase):
    def setUp(self):
        self.rules = fw.parse_ufw(UFW_NUM)
        self.protected = {22, 443}

    def test_enable_needs_every_protected_port_already_allowed(self):
        """启用即生效：受保护端口里只要有一条还没放行，就要拒绝并点名是哪一条。"""
        rules = fw.parse_ufw("[ 1] 22/tcp                     ALLOW IN    Anywhere\n")
        why = fw.lockout_reasons(rules, {22, 443}, intent="enable")
        self.assertIn("没有任何放行规则：443。", why[0])
        self.assertIn("请先添加放行规则", why[0])

    def test_enable_passes_when_all_covered(self):
        rules = fw.parse_ufw(
            "[ 1] 22/tcp                     ALLOW IN    Anywhere\n"
            "[ 2] 443/tcp                    ALLOW IN    Anywhere\n"
        )
        self.assertEqual(fw.lockout_reasons(rules, self.protected, intent="enable"), [])
        # limit 是带限速的 accept，同样算已放行
        limited = fw.parse_ufw(
            "[ 1] 22/tcp                     LIMIT IN    Anywhere\n"
            "[ 2] 443/tcp                    ALLOW IN    Anywhere\n"
        )
        self.assertEqual(fw.lockout_reasons(limited, self.protected, intent="enable"), [])

    def test_removing_a_covering_rule_is_blocked(self):
        why = fw.lockout_reasons(self.rules, {22}, intent="remove", target=self.rules[0])
        self.assertIn("22/tcp", why[0])
        self.assertIn("唯一的放行来源", why[0])

    def test_twin_v6_rule_does_not_take_over_for_an_ipv4_admin(self):
        """ufw 的 22/tcp 与 22/tcp (v6) 是两条规则：删掉 v4 之后 v6 仍"覆盖 22"，但人是从 v4 连的。"""
        rules = fw.parse_ufw(
            "[ 1] 22/tcp                     ALLOW IN    Anywhere\n"
            "[ 2] 22/tcp (v6)                ALLOW IN    Anywhere (v6)\n"
        )
        self.assertIn("唯一的放行来源", fw.lockout_reasons(rules, {22}, intent="remove", target=rules[0])[0])
        self.assertEqual(fw.lockout_reasons(rules, {8080}, intent="remove", target=rules[0]), [])

    def test_a_redundant_allow_in_the_same_family_is_deletable(self):
        """同族里还有别的规则顶着 22 时，删除不该被拦——否则保护会淹掉真正的风险。"""
        rules = fw.parse_ufw(
            "[ 1] 22:25/tcp                  ALLOW IN    Anywhere\n"
            "[ 2] 22/tcp                     ALLOW IN    Anywhere\n"
        )
        # 22 有 22:25 顶着，可以删；23 只有区间那条覆盖，删它就锁死。
        self.assertEqual(fw.lockout_reasons(rules, {22}, intent="remove", target=rules[1]), [])
        why = fw.lockout_reasons(rules, {22, 23}, intent="remove", target=rules[0])
        self.assertIn("唯一的放行来源", why[0])
        self.assertIn("23", why[0])
        self.assertNotIn("22,", why[0])

    def test_removing_an_unrelated_rule_is_allowed(self):
        rules = fw.parse_ufw("[ 1] 8080/tcp                   ALLOW IN    Anywhere\n")
        self.assertEqual(fw.lockout_reasons(rules, {22}, intent="remove", target=rules[0]), [])

    def test_deny_on_ssh_port_is_blocked_but_other_ports_pass(self):
        blocked = fw.validate_rule({"action": "deny", "port": 22})
        self.assertIn("SSH", fw.lockout_reasons(self.rules, {22}, intent="add", target=blocked)[0])
        self.assertEqual(fw.lockout_reasons(self.rules, {22}, intent="add", target=fw.validate_rule({"action": "deny", "port": 8080})), [])

    def test_deny_from_a_narrow_source_is_not_a_lockout(self):
        """只封一个外部地址段的 deny 不会挡管理员自己，不该拦。"""
        body = fw.validate_rule({"action": "deny", "port": 22, "source": "203.0.113.7"})
        self.assertEqual(fw.lockout_reasons(self.rules, {22}, intent="add", target=body), [])

    def test_reload_uses_the_permanent_rule_set(self):
        why = fw.lockout_reasons([], self.protected, intent="reload")
        self.assertIn("permanent", why[0])


# --------------------------------------------------------------------- ufw 写路径
class UfwWriteCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        _pin_protected(self)

    async def test_add_writes_expected_argv(self):
        m = _use_ufw(self)
        out = await fw.add_rule({"port": 8000}, fw.UFW)
        self.assertIn("Rule added", out["detail"])
        self.assertEqual(m.calls[-1], ("ufw", ["allow", "8000/tcp"]))
        await fw.add_rule({"port": 8000, "port_to": 8010, "proto": "udp"}, fw.UFW)
        self.assertEqual(m.calls[-1][1], ["allow", "8000:8010/udp"])
        await fw.add_rule({"port": 9000, "proto": "both"}, fw.UFW)
        self.assertEqual(m.calls[-1][1], ["allow", "9000"])
        await fw.add_rule({"port": 8002, "source": "10.0.0.0/8", "comment": "office"}, fw.UFW)
        self.assertEqual(
            m.calls[-1][1],
            ["allow", "from", "10.0.0.0/8", "to", "any", "port", "8002", "proto", "tcp", "comment", "office"],
        )
        await fw.add_rule({"port": 8003, "rate": True}, fw.UFW)
        self.assertEqual(m.calls[-1][1], ["limit", "8003/tcp"])

    async def test_added_rule_appears_twice_because_of_v6(self):
        m = _use_ufw(self)
        await fw.add_rule({"port": 8000}, fw.UFW)
        self.assertEqual([r["family"] for r in m.find(8000)], ["ipv4", "ipv6"])

    async def test_duplicate_allow_is_rejected_before_calling_ufw(self):
        m = _use_ufw(self)
        with self.assertRaises(BadInput):
            await fw.add_rule({"port": 22}, fw.UFW)
        self.assertNotIn("allow", [c[1][0] for c in m.calls])

    async def test_deny_on_protected_port_is_forbidden_with_reason(self):
        m = _use_ufw(self)
        with self.assertRaises(Forbidden) as cm:
            await fw.add_rule({"action": "deny", "port": 22}, fw.UFW)
        self.assertIn("sshd 监听端口", str(cm.exception))
        self.assertFalse([c for c in m.calls if c[1][:1] == ["deny"]])

    async def test_tool_failure_is_mapped_and_not_forwarded_verbatim(self):
        m = _use_ufw(self)
        m.fail_with = "ERROR: Could not commit ip6tables ruleset: iptables-restore: line 5 failed"
        with self.assertRaises(ExecFailed) as cm:
            await fw.add_rule({"port": 8000}, fw.UFW)
        self.assertNotIn("line 5", str(cm.exception))
        self.assertEqual(m.find(8000), [], "失败的添加不该留在表里")

    async def test_no_firewall_on_the_machine(self):
        _use_ufw(self)
        with self.assertRaises(ExecFailed):
            await fw.add_rule({"port": 8000}, None)
        with self.assertRaises(ExecFailed):
            await fw.remove_rule(1, "", None)

    async def test_delete_uses_force_and_checks_the_table_afterwards(self):
        """回归：不带 --force 的 `ufw delete N` 回 rc=0 却打印 Aborted，每次删除都"成功"而没删掉。"""
        m = _use_ufw(self, [_rule(port=22), _rule(port=8000), _rule(port=8000, family="ipv6")])
        target = m.find(8000)[0]
        out = await fw.remove_rule(target["num"], target["sig"], fw.UFW)
        self.assertEqual(out["removed"], "8000/tcp")
        self.assertIn(("ufw", ["--force", "delete", str(target["num"])]), m.calls)
        self.assertNotIn(("ufw", ["delete", str(target["num"])]), m.calls)
        self.assertEqual(m.calls[-1], ("ufw", ["status", "numbered"]), "删完必须重新列表确认")
        self.assertEqual([r["family"] for r in m.find(8000)], ["ipv6"], "v4 删了，v6 应当还在")
        # ufw 的 allow 不带来源时一次写两条，delete 只删一条编号：报告"已删除"却不提孪生那条，
        # 用户会以为这个端口已经关掉了。
        self.assertEqual(len(out["still_open"]), 1)
        self.assertIn("8000", out["still_open"][0])

    async def test_deleting_a_rule_without_a_twin_reports_no_still_open(self):
        m = _use_ufw(self, [_rule(port=22), _rule(port=8000)])
        target = m.find(8000)[0]
        out = await fw.remove_rule(target["num"], target["sig"], fw.UFW)
        self.assertNotIn("still_open", out, "没有孪生规则就别凭空多一个字段")

    async def test_deleting_the_ipv6_twin_does_not_blame_other_ports(self):
        """回归：删 v6 那条时，22/80 这些无关的 v4 规则不是孪生。

        早期实现比的是 token，而 ufw 状态表解析出的规则根本没有 token 字段（全是 None），
        于是同动作同方向的跨族规则被无差别当成孪生报出来——线上探针实测误报了 22/80/443。
        """
        m = _use_ufw(self, [_rule(port=22), _rule(port=80), _rule(port=8000),
                            _rule(port=8000, family="ipv6")])
        target = [r for r in m.find(8000) if r["family"] == "ipv6"][0]
        out = await fw.remove_rule(target["num"], target["sig"], fw.UFW)
        self.assertEqual(out["still_open"], ["8000/tcp"], "只剩 v4 那条 8000，别的端口不该被点名")

    async def test_a_twin_label_names_the_family(self):
        """两条的 raw 都是 "8000/tcp"，不带族标签用户根本分不出删掉了哪条。"""
        m = _use_ufw(self, [_rule(port=8000), _rule(port=8000, family="ipv6")])
        target = [r for r in m.find(8000) if r["family"] == "ipv4"][0]
        out = await fw.remove_rule(target["num"], target["sig"], fw.UFW)
        self.assertEqual(out["still_open"], ["8000/tcp（IPv6）"])

    async def test_another_protocol_on_the_same_port_is_not_a_twin(self):
        m = _use_ufw(self, [_rule(port=8000, proto="tcp"),
                            _rule(port=8000, proto="udp", family="ipv6")])
        target = [r for r in m.find(8000) if r["proto"] == "tcp"][0]
        out = await fw.remove_rule(target["num"], target["sig"], fw.UFW)
        self.assertNotIn("still_open", out, "tcp 与 udp 是两个端口，不该互相点名")

    async def test_a_rule_from_another_source_is_not_a_twin(self):
        m = _use_ufw(self, [_rule(port=8000, source="203.0.113.9"),
                            _rule(port=8000, source="2001:db8::/32", family="ipv6")])
        target = [r for r in m.find(8000) if r["family"] == "ipv4"][0]
        out = await fw.remove_rule(target["num"], target["sig"], fw.UFW)
        self.assertNotIn("still_open", out, "来源不同的两条放行互不相干")

    async def test_the_from_column_survives_the_status_table_render(self):
        """夹具的列宽必须留出两个以上空格：Action 与 From 挤成一列时，parse_ufw 会把来源读成
        Anywhere，来源判定就无从校验——这一版差点被它瞒过去。
        """
        m = _use_ufw(self, [_rule(port=8000, source="203.0.113.9"),
                            _rule(port=9000, source="2001:db8::/32", family="ipv6")])
        got = {r["port"]: r["source"] for r in m.table()}
        self.assertEqual(got, {8000: "203.0.113.9", 9000: "2001:db8::/32"})

    async def test_delete_reports_failure_when_the_rule_survives(self):
        m = _use_ufw(self, [_rule(port=22), _rule(port=8000)])
        target = m.find(8000)[0]

        async def lying(cmd, args, cwd=None, timeout=120, env=None):
            if args[:2] == ["--force", "delete"]:
                return {"code": 0, "out": "Rule deleted"}  # 假装删了，规则表不动
            return await m.run(cmd, args, timeout=timeout)

        with mock.patch.object(fw, "run", lying):
            with self.assertRaises(ExecFailed) as cm:
                await fw.remove_rule(target["num"], target["sig"], fw.UFW)
        self.assertIn("未被删除", str(cm.exception))

    async def test_numbers_shift_after_a_delete_and_old_signatures_are_useless(self):
        m = _use_ufw(self, [_rule(port=22), _rule(port=8000), _rule(port=8001)])
        second = m.find(8001)[0]
        await fw.remove_rule(m.find(8000)[0]["num"], m.find(8000)[0]["sig"], fw.UFW)
        self.assertEqual(second["num"], 3, "删除前它确实是 3 号")
        with self.assertRaises(BadInput):
            await fw.remove_rule(second["num"], second["sig"], fw.UFW)
        fresh = m.find(8001)[0]
        self.assertEqual(fresh["num"], 2)
        out = await fw.remove_rule(fresh["num"], fresh["sig"], fw.UFW)
        self.assertEqual(out["removed"], "8001/tcp")

    async def test_delete_refuses_stale_signature_and_missing_number(self):
        m = _use_ufw(self, [_rule(port=22), _rule(port=8000)])
        target = m.find(8000)[0]
        with self.assertRaises(BadInput) as cm:
            await fw.remove_rule(target["num"], m.find(22)[0]["sig"], fw.UFW)
        self.assertIn("现在对应别的规则", str(cm.exception))
        with self.assertRaises(BadInput) as cm:
            await fw.remove_rule(9, target["sig"], fw.UFW)
        self.assertIn("该编号不存在", str(cm.exception))
        self.assertTrue(m.find(8000))

    async def test_delete_rejects_out_of_range_numbers(self):
        _use_ufw(self)
        for bad in (0, 10000, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(BadInput):
                    await fw.remove_rule(bad, "", fw.UFW)

    async def test_delete_protected_rule_is_forbidden(self):
        m = _use_ufw(self, [_rule(port=22), _rule(port=443)])
        ssh = m.find(22)[0]
        with self.assertRaises(Forbidden) as cm:
            await fw.remove_rule(ssh["num"], ssh["sig"], fw.UFW)
        self.assertIn("sshd 监听端口", str(cm.exception))
        # 443 平时不在保护名单里，但管理员正从这个端口进来看面板时就得拦住
        target = m.find(443)[0]
        with self.assertRaises(Forbidden) as cm:
            await fw.remove_rule(target["num"], target["sig"], fw.UFW, FakeRequest({"origin": "https://admin.example.cc"}))
        self.assertIn("你当前访问面板所用的端口", str(cm.exception))
        self.assertFalse([c for c in m.calls if c[1][:2] == ["--force", "delete"]])

    async def test_enable_refuses_when_ssh_is_not_allowed(self):
        m = _use_ufw(self, [_rule(port=80)], active=False)
        with self.assertRaises(Forbidden) as cm:
            await fw.set_enabled(True, fw.UFW)
        self.assertIn("22", str(cm.exception))
        self.assertFalse(m.active)
        self.assertFalse([c for c in m.calls if c[1][:1] == ["enable"]], "不能用不带 --force 的 enable 赌一把")

    async def test_enable_and_disable_use_force(self):
        m = _use_ufw(self, [_rule(port=22), _rule(port=22, family="ipv6")], active=False)
        out = await fw.set_enabled(True, fw.UFW)
        self.assertTrue(out["active"])
        self.assertEqual(m.calls[-1], ("ufw", ["--force", "enable"]))
        self.assertTrue(m.active)
        await fw.set_enabled(False, fw.UFW)
        self.assertEqual(m.calls[-1], ("ufw", ["--force", "disable"]))
        self.assertFalse(m.active)

    async def test_read_shape_and_protection_flags(self):
        _use_ufw(self, [_rule(port=22), _rule(port=8000)])
        out = await fw.read(fw.UFW, FakeRequest({"origin": "https://admin.example.cc"}))
        self.assertEqual(out["tool"], "ufw")
        self.assertTrue(out["active"])
        self.assertEqual(out["default_incoming"], "deny")
        self.assertEqual(out["logging"], "on (low)")
        self.assertEqual(out["protected"], [22, 443])
        self.assertEqual(out["protected_reasons"]["443"], "你当前访问面板所用的端口")
        self.assertEqual({r["port"]: r["protected"] for r in out["rules"]}, {22: True, 8000: False})
        self.assertIn("Status: active", out["output"])


# --------------------------------------------------------------------- firewalld 写路径
class FirewalldWriteCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        _pin_protected(self)

    async def test_add_plain_port_reloads_after_writing_permanent(self):
        """reload 排在写之后。

        位置不能按下标取：add 与 reload 之间现在夹着 permanent 那份表的读取与逐条
        `--info-service`（批次㊷：reload 之前要确认重载之后 SSH 还在放行里），
        按 `calls[-2]` 取到的是探测，不是那条写命令。
        """
        m = _use_firewalld(self)
        await fw.add_rule({"port": 8000}, fw.FIREWALLD)
        write = ("firewall-cmd", ["--permanent", "--add-port=8000/tcp"])
        self.assertIn(write, m.calls)
        self.assertEqual(m.calls[-1], ("firewall-cmd", ["--reload"]))
        self.assertLess(m.calls.index(write), len(m.calls) - 1)
        self.assertIn("8000/tcp", m.pm["ports"])

    async def test_range_uses_firewalld_dash_syntax(self):
        m = _use_firewalld(self)
        await fw.add_rule({"port": 8000, "port_to": 8100, "proto": "udp"}, fw.FIREWALLD)
        self.assertIn(["--permanent", "--add-port=8000-8100/udp"], [c[1] for c in m.calls])

    async def test_sourced_rule_becomes_a_rich_rule(self):
        m = _use_firewalld(self)
        await fw.add_rule({"port": 8443, "source": "10.0.0.0/8"}, fw.FIREWALLD)
        self.assertEqual(m.pm["rich"], [RICH_V4])
        self.assertIn(f"--add-rich-rule={RICH_V4}", [arg for _c in m.calls for arg in _c[1]])

    async def test_v6_source_becomes_an_ipv6_rich_rule(self):
        m = _use_firewalld(self)
        await fw.add_rule({"port": 8443, "source": "2001:db8::/32", "proto": "udp", "action": "deny"}, fw.FIREWALLD)
        self.assertIn('family="ipv6"', m.pm["rich"][0])
        self.assertIn("drop", m.pm["rich"][0])

    async def test_reload_failure_is_reported(self):
        m = _use_firewalld(self)
        m.reload_after = "Error: FIREWALLD is not running"
        with self.assertRaises(ExecFailed):
            await fw.add_rule({"port": 8000}, fw.FIREWALLD)

    async def test_remove_uses_the_matching_flag(self):
        m = _use_firewalld(self,
            runtime={"services": ["ssh"], "ports": ["8000/tcp"], "rich": [RICH_V4]},
            permanent={"services": ["ssh"], "ports": ["8000/tcp"], "rich": [RICH_V4]},
        )
        by_kind = {r["kind"]: r for r in (await fw.read(fw.FIREWALLD))["rules"]}
        await fw.remove_rule(by_kind["port"]["num"], by_kind["port"]["sig"], fw.FIREWALLD)
        self.assertIn(("firewall-cmd", ["--permanent", "--remove-port=8000/tcp"]), m.calls)
        self.assertEqual(m.calls[-1], ("firewall-cmd", ["--reload"]))
        by_kind = {r["kind"]: r for r in (await fw.read(fw.FIREWALLD))["rules"]}
        await fw.remove_rule(by_kind["rich"]["num"], by_kind["rich"]["sig"], fw.FIREWALLD)
        self.assertIn(("firewall-cmd", ["--permanent", f"--remove-rich-rule={RICH_V4}"]), m.calls)

    async def test_removing_a_rule_that_empties_permanent_skips_reload(self):
        """运行期还有别的规则顶岗所以能删；但删完 permanent 就空了，此时 reload 才是锁死的动作。"""
        m = _use_firewalld(self,
            runtime={"services": ["ssh"], "ports": ["22/tcp"]},
            permanent={"services": ["ssh"]},
        )
        out = await fw.read(fw.FIREWALLD)
        ssh = [r for r in out["rules"] if r["token"] == "ssh"][0]
        self.assertFalse(ssh["protected"], "运行期还有一条 22/tcp 顶岗，删掉 ssh 不会立刻不通")
        result = await fw.remove_rule(ssh["num"], ssh["sig"], fw.FIREWALLD)
        self.assertFalse(result["reloaded"])
        self.assertIn("permanent", result["detail"])
        self.assertNotIn(["--reload"], [c[1] for c in m.calls])
        self.assertEqual(m.rt["ports"], ["22/tcp"], "不 reload 就是为了让运行期继续放行 SSH")

    async def test_reload_kept_when_permanent_still_covers_ssh(self):
        m = _use_firewalld(self,
            runtime={"services": ["ssh"], "ports": ["8000/tcp"], "rich": [RICH_V4]},
            permanent={"services": ["ssh"], "ports": ["8000/tcp"], "rich": [RICH_V4]},
        )
        out = await fw.read(fw.FIREWALLD)
        target = [r for r in out["rules"] if r["kind"] == "rich"][0]
        result = await fw.remove_rule(target["num"], target["sig"], fw.FIREWALLD)
        self.assertNotIn("reloaded", result)
        self.assertEqual(m.calls[-1], ("firewall-cmd", ["--reload"]))

    async def test_persisted_flag_marks_runtime_only_rules(self):
        _use_firewalld(self,
            runtime={"services": ["ssh"], "ports": ["8000/tcp", "9999/tcp"]},
            permanent={"services": ["ssh"], "ports": ["8000/tcp"]},
        )
        out = await fw.read(fw.FIREWALLD)
        self.assertEqual({r["port"]: r["persisted"] for r in out["rules"] if r["kind"] == "port"}, {8000: True, 9999: False})
        self.assertEqual(out["zone"], "public")
        self.assertTrue(out["active"])

    async def test_enable_requires_ssh_in_permanent(self):
        m = _use_firewalld(self, runtime={"services": ["ssh"]}, permanent={"services": []}, running=False)
        with self.assertRaises(Forbidden):
            await fw.set_enabled(True, fw.FIREWALLD)
        self.assertFalse(m.running)
        m2 = _use_firewalld(self, permanent={"services": ["ssh"]}, running=False)
        await fw.set_enabled(True, fw.FIREWALLD)
        self.assertEqual(m2.calls[-1], ("systemctl", ["enable", "--now", "firewalld"]))
        await fw.set_enabled(False, fw.FIREWALLD)
        self.assertEqual(m2.calls[-1], ("systemctl", ["disable", "--now", "firewalld"]))


# --------------------------------------------------------------------- 路由
_seq = iter(range(1000))


class FirewallApiCase(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        from app import main

        self.client = TestClient(main.app)
        self.user = f"fwcheck_{os.getpid()}_{next(_seq)}"
        self.viewer = f"fwview_{os.getpid()}_{next(_seq)}"
        for name, role in ((self.user, "admin"), (self.viewer, "viewer")):
            dbm.execute(
                "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                (name, security.hash_pass("Password123"), role),
            )
        self.token = security.sign_token(self.user, "admin")
        self.viewer_token = security.sign_token(self.viewer, "viewer")
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username IN (?,?)", (self.user, self.viewer))
        self.addCleanup(dbm.execute, "DELETE FROM audit WHERE username IN (?,?)", (self.user, self.viewer))
        self.tmp = tempfile.TemporaryDirectory(prefix="cp_batch8a_api_")
        self.addCleanup(self.tmp.cleanup)
        Path(self.tmp.name, "sshd_config").write_text("Port 22\n", encoding="utf-8")
        self.machine = UfwMachine([_rule(port=22), _rule(port=22, family="ipv6")])

        async def detect():
            return fw.UFW

        for p in (
            mock.patch.object(fw, "SSHD_CONFIG", str(Path(self.tmp.name, "sshd_config"))),
            mock.patch.object(fw, "NGINX_CONF_GLOBS", (str(Path(self.tmp.name, "nginx", "*.conf")),)),
            mock.patch.object(fw, "run", self.machine.run),
            mock.patch.object(fw, "detect", detect),
            mock.patch.object(config, "PORT", 3210),
            mock.patch.object(config, "HOST", "127.0.0.1"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def hdr(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    def last_audit(self):
        return dbm.query_one("SELECT * FROM audit ORDER BY id DESC LIMIT 1")

    def test_anonymous_and_viewer_cannot_read_or_write(self):
        self.assertEqual(self.client.get("/api/firewall").status_code, 401)
        for path, kw in (("/api/firewall", {}), ("/api/firewall/rules", {"json": {"port": 8000}}),
                         ("/api/firewall/state", {"json": {"active": True}})):
            for verb in ("get", "post"):
                if verb == "get" and path != "/api/firewall":
                    continue
                r = getattr(self.client, verb)(path, headers=self.hdr(self.viewer_token), **kw)
                self.assertEqual(r.status_code, 403, r.text)
        r = self.client.delete("/api/firewall/rules/1?expect=x", headers=self.hdr(self.viewer_token))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(len(self.machine.find(22)), 2, "被拒的请求一条命令都不该执行")

    def test_get_returns_structured_table(self):
        r = self.client.get("/api/firewall", headers=self.hdr())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["tool"], "ufw")
        self.assertEqual([x["num"] for x in body["rules"]], [1, 2])
        self.assertEqual(body["protected"], [22])
        self.assertTrue(all(x["protected"] for x in body["rules"]))
        self.assertIn("output", body)

    def test_add_and_remove_are_audited(self):
        r = self.client.post("/api/firewall/rules", json={"port": 8000, "comment": "self test"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200, r.text)
        added = self.machine.find(8000)[0]
        row = self.last_audit()
        self.assertEqual(row["action"], "firewall:add")
        self.assertEqual(row["username"], self.user)
        self.assertIn("port=8000", row["detail"])
        r = self.client.delete(f"/api/firewall/rules/{added['num']}?expect={added['sig']}", headers=self.hdr())
        self.assertEqual(r.status_code, 200, r.text)
        row = self.last_audit()
        self.assertEqual(row["action"], "firewall:remove")
        self.assertIn("已删除", row["detail"])
        self.assertEqual([x["family"] for x in self.machine.find(8000)], ["ipv6"])

    def test_refused_writes_are_audited_too(self):
        r = self.client.post("/api/firewall/rules", json={"action": "deny", "port": 22}, headers=self.hdr())
        self.assertEqual(r.status_code, 403, r.text)
        row = self.last_audit()
        self.assertEqual(row["action"], "firewall:add")
        self.assertIn("拒绝", row["detail"])
        self.assertIn("port=22", row["detail"])

    def test_state_switch_is_audited(self):
        r = self.client.post("/api/firewall/state", json={"active": True}, headers=self.hdr())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.last_audit()["action"], "firewall:state")
        self.assertIn("启用成功", self.last_audit()["detail"])
        self.assertTrue(self.machine.active)
        r = self.client.post("/api/firewall/state", json={"active": False}, headers=self.hdr())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("停用成功", self.last_audit()["detail"])

    def test_state_needs_an_explicit_boolean(self):
        """回归：`bool(body.get("active"))` 会把缺字段/字符串都读成"停用"，一次手滑就把防火墙关了。"""
        for body in ({}, {"active": "yes"}, {"active": 1}, {"active": None}):
            with self.subTest(body=body):
                r = self.client.post("/api/firewall/state", json=body, headers=self.hdr())
                self.assertEqual(r.status_code, 400, r.text)
                self.assertTrue(self.machine.active)
