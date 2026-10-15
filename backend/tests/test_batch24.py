"""批次㉔：自检判词里「一共几项」与「点名哪一项」必须同源，而且数字要站在冒号之前。

起因是本机实测的一行 fail：

    2 项改动没进正在跑的进程：unattended-upgrades.service：安全相关的库装上了新版，…

它说有两项，却只点了一个名字，被吞掉的第二项是 postgresql@17-main.service——恰好是最该
看见的那一个。同样的「数 N 点 1」在 load 的 fail 分支和 login 的合并判词上各有一份，
另外四处（load/pending 的 warn、memory 的两条、storage）各自手抄了一遍 `，另有 N 项`。
手抄的会漏，漏的正好是最该出声的 fail 分支。

补上尾巴以后又在浏览器里量了一遍：自检列表把判词放在项目名下面的副标题行，窄屏那一列
实测 187px，三行折行放不下整句（本机这条 pending 的 fail 要 5 行）——挂在句尾的「另有 N 项」
正好被裁掉。所以这一批把判词折成 `app/util.py::tallied`：报数和「另有」都在冒号之前，
点名那条放在冒号之后。句子被裁掉的是名字，不会再是那半句「不止这一条」。

三件事由用例锁住：这条句式只有一个出处、点名的那条属于被数的那一族、
数 == 1 + 另有 且「另有」落在冒号之前。
"""
import re
import unittest
from pathlib import Path

from app.services import (
    load_service,
    login_audit_service,
    memory_service,
    pending_service,
    storage_service,
)
from app.util import tallied

ROOT = Path(__file__).resolve().parents[1]


def finding(sev: str, title: str) -> dict:
    return {"id": title, "severity": sev, "title": title, "detail": "", "fix": ""}


def load_data(titles, sev):
    return load_service, {
        "load": {"ok": True, "one": 8.0, "five": 8.0, "fifteen": 8.0},
        "cores": 2,
        "counts": {"risk": 0, "warn": 0, "info": 0},
        "findings": [finding(sev, t) for t in titles],
    }


def pending_data(titles, sev):
    return pending_service, {
        "kernel": {"ok": True, "running": "6.12.111"},
        "stale": {"ok": True},
        "counts": {"risk": 0, "warn": 0, "info": 0},
        "findings": [finding(sev, t) for t in titles],
    }


def memory_data(titles, sev):
    return memory_service, {
        "mem": {"ok": True, "used_pct": 61, "available": 2_600_000},
        "oom": {"kernel_total": 0, "unit_kills": {}},
        "counts": {},
        "findings": [finding(sev, t) for t in titles],
    }


def storage_data(titles, sev):
    n = len(titles)
    return storage_service, {
        "counts": {"roots": 3, "bytes_total": 1, "info": 0,
                   "risk": n if sev == storage_service.SEV_RISK else 0,
                   "warn": 0 if sev == storage_service.SEV_RISK else n},
        "findings": [finding(sev, t) for t in titles],
        "disk": {"ok": True, "used_pct": 88},
    }


def login_data(titles, sev):
    # 登录项的总数来自 _summary(findings)，点名用的是 risk 那一族——两边的条数必须对得上。
    others = [finding("warn", "公钥权限松")] if sev == "risk" else []
    risk_n = len(titles) if sev == "risk" else 0
    warn_n = len(titles) if sev == "warn" else len(others)
    return login_audit_service, {
        "as_root": True,
        "sshd": {"source": "sshd -T", "port": 22, "reachable": True},
        "accounts": {"empty_password": [], "uid0_extra": []},
        "attempts": {"ok": True, "failed": 12, "window": "24h"},
        "notes": [],
        "counts": {"risk": risk_n, "warn": warn_n, "note": 0},
        "findings": others + [finding(sev, t) for t in titles],
    }


# (builder, 会走到「报数 + 点名」那条分支的严重度)。登录的 warn-only 分支只报条数不点名，
# 那里没有「数与点名对不上」可言，所以不列进来。
CASES = (
    (load_data, ("risk", "warn")),
    (pending_data, ("risk", "warn")),
    (memory_data, ("risk", "warn")),
    (storage_data, ("risk", "warn")),
    (login_data, ("risk",)),
)

TOTAL_RE = re.compile(r"(\d+) ([项处])")
REST_RE = re.compile(r"另有 (\d+) ([项处])")


def total_rest_and_pos(detail: str) -> tuple[int, int, int]:
    """从判词里取出「总数」「另有几条」，以及「另有」相对冒号的位置（负数=在点名之前）。

    总数取句中最先出现的那个量词：五句都以它开头，登录那句以「登录方式有 3 项要紧」开头。
    冒号后面只有一个名字，所以总数应当等于 1 + 另有。
    """
    m = TOTAL_RE.search(detail)
    assert m, f"判词里没有总数：{detail}"
    rest = REST_RE.search(detail)
    colon = detail.find("：")
    return int(m.group(1)), (int(rest.group(1)) if rest else 0), (
        (rest.start() - colon) if rest and colon >= 0 else 0)


class TestTallied(unittest.TestCase):
    def test_nothing_to_name_leaves_a_count_only(self):
        """一条都没有时不该有冒号后面的空名字，也不该有「另有 0 项」。"""
        for arg in ([], None):
            with self.subTest(arg=arg):
                self.assertEqual(tallied(arg, " 项要紧的负载问题"), "0 项要紧的负载问题")

    def test_blank_titles_are_neither_named_nor_counted(self):
        """空标题既不点名也不计数：「另有 1 项」不能指一条没有文字的判词。"""
        self.assertEqual(tallied(["", ""], " 项要紧"), "0 项要紧")
        self.assertEqual(tallied(["", "甲"], " 项要紧"), "1 项要紧：甲")

    def test_single_item_is_just_its_name(self):
        self.assertEqual(tallied(["甲"], " 项要紧"), "1 项要紧：甲")

    def test_many_items_count_then_name(self):
        self.assertEqual(tallied(["甲", "乙", "丙"], " 项要紧"), "3 项要紧（另有 2 项）：甲")

    def test_measure_word_follows_the_caller(self):
        self.assertEqual(tallied(["甲", "乙"], " 处占用值得处理", "处"),
                         "2 处占用值得处理（另有 1 处）：甲")

    def test_explicit_total_decides_the_rest(self):
        """前半句说 3 项而手里只有 2 条标题时，「另有」必须是 3 减点名的那一条。"""
        self.assertEqual(tallied(["甲", "乙"], " 项要紧", "项要紧", 3), "3 项要紧（另有 2 项要紧）：甲")

    def test_total_below_the_named_column_is_clamped(self):
        """给的总数比这一列还小，是调用方算错；不许折成「另有 0 项」把人安抚成绿。"""
        self.assertEqual(tallied(["甲", "乙", "丙"], " 项", "项", 1), "3 项（另有 2 项）：甲")

    def test_lead_and_mid_keep_the_callers_sentence(self):
        """登录那句在数字前后各有自己的措辞，句式仍然由 tallied 拼。"""
        self.assertEqual(
            tallied(["甲", "乙"], " 项要紧", "项要紧", 2, lead="登录方式有 ", mid="、1 项值得处理"),
            "登录方式有 2 项要紧（另有 1 项要紧）、1 项值得处理：甲")

    def test_non_string_titles_survive(self):
        self.assertEqual(tallied([1, 2], " 项"), "2 项（另有 1 项）：1")


class TestTallyMatchesNaming(unittest.TestCase):
    def test_naming_plus_rest_equals_total(self):
        for builder, sevs in CASES:
            for sev in sevs:
                for titles in (["甲", "乙"], ["甲", "乙", "丙"], ["甲", "乙", "丙", "丁", "戊"]):
                    with self.subTest(service=builder.__name__, sev=sev, n=len(titles)):
                        svc, data = builder(titles, sev)
                        detail = svc.doctor_item(data)["detail"]
                        total, rest, _ = total_rest_and_pos(detail)
                        self.assertEqual(total, len(titles), detail)
                        self.assertEqual(total, 1 + rest, detail)
                        self.assertIn("甲", detail.split("：", 1)[1], detail)

    def test_the_rest_speaks_before_the_naming(self):
        """「另有 N」必须在冒号之前：窄屏那列放不下整句，句尾是被裁的那一半。"""
        for builder, sevs in CASES:
            for sev in sevs:
                with self.subTest(service=builder.__name__, sev=sev):
                    svc, data = builder(["甲", "乙", "丙"], sev)
                    detail = svc.doctor_item(data)["detail"]
                    _, _, rest_pos = total_rest_and_pos(detail)
                    self.assertLess(rest_pos, 0, detail)

    def test_measure_word_matches_the_side_being_counted(self):
        """前半句数的是「几处」，补数就不能说「另有几项」——同一件事换个量词像另一件事。"""
        for builder, sevs in CASES:
            for sev in sevs:
                with self.subTest(service=builder.__name__, sev=sev):
                    svc, data = builder(["甲", "乙", "丙"], sev)
                    detail = svc.doctor_item(data)["detail"]
                    front = TOTAL_RE.search(detail)
                    tail = REST_RE.search(detail)
                    self.assertIsNotNone(tail, detail)
                    self.assertEqual(front.group(2), tail.group(2), detail)

    def test_single_item_does_not_claim_a_hidden_rest(self):
        for builder, sevs in CASES:
            for sev in sevs:
                with self.subTest(service=builder.__name__, sev=sev):
                    svc, data = builder(["只有这一条"], sev)
                    detail = svc.doctor_item(data)["detail"]
                    self.assertNotIn("另有", detail)
                    self.assertIn("只有这一条", detail)

    def test_the_named_one_belongs_to_the_counted_family(self):
        """点名的那条必须属于前半句在数的那一族：不能拿一条 warn 冒充「3 项要紧」的第一项。"""
        data = login_data(["口令认证开着", "root 能直连"], "risk")[1]
        data["findings"] = [finding("warn", "公钥权限松"), finding("risk", "口令认证开着"),
                           finding("risk", "root 能直连"), finding("note", "知道就好")]
        detail = login_audit_service.doctor_item(data)["detail"]
        self.assertIn("口令认证开着", detail)
        self.assertNotIn("公钥权限松", detail)
        self.assertNotIn("知道就好", detail)


class TestRiskBranchesStillSpeak(unittest.TestCase):
    """三条本来漏写补数的分支，逐条按各自的档位复验。"""

    def test_pending_fail_admits_the_second_unit(self):
        """本机实测那一条：两项 stale 服务，判词必须承认还有第二项（postgresql 就是这么被吞的）。"""
        svc = pending_service
        data = {
            "kernel": {"ok": True, "running": "6.12.111"},
            "stale": {"ok": True},
            "counts": {"risk": 2, "warn": 0, "info": 0},
            "findings": [finding(svc.SEV_RISK, "unattended-upgrades.service：安全相关的库没进内存"),
                         finding(svc.SEV_RISK, "postgresql@17-main.service：安全相关的库没进内存")],
        }
        item = svc.doctor_item(data)
        self.assertEqual(item["status"], "fail")
        self.assertTrue(item["detail"].startswith("2 项改动没进正在跑的进程（另有 1 项）："), item["detail"])

    def test_load_fail(self):
        svc = load_service
        item = svc.doctor_item(load_data(["每核排队 4.0", "面板自己被配额压住"], svc.SEV_RISK)[1])
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["detail"], "2 项要紧的负载问题（另有 1 项）：每核排队 4.0")

    def test_login_merged_verdict(self):
        """补数跟着量词走，不把前半句的名词再念一遍（load 那行就是这么写的）。

        登录项原先把"项要紧"整个当成量词传给 `tallied`，于是写成「2 项要紧（另有 1 项要紧）」——
        括号里那半句是「不止这一条」的唯一凭证，念成复读机就没人再看它。
        """
        item = login_audit_service.doctor_item(login_data(["甲要紧", "乙要紧"], "risk")[1])
        self.assertEqual(item["detail"], "登录方式有 2 项要紧（另有 1 项）、1 项值得处理：甲要紧")

    def test_memory_oom_fail_lists_the_other_verdicts(self):
        """OOM 那一条原先也只点第一个判词：判词是「为什么被杀」，藏在第二条里就等于没说。"""
        svc = memory_service
        data = memory_data(["内核 OOM 杀过 2 次", "某单元被自己的 MemoryMax 压住"], svc.SEV_RISK)[1]
        data["oom"] = {"kernel_total": 2, "unit_kills": {"a.service": 1}}
        item = svc.doctor_item(data)
        self.assertEqual(item["status"], "fail")
        self.assertIn("。2 项判词（另有 1 项）：内核 OOM 杀过 2 次", item["detail"])

    def test_storage_keeps_the_partition_number_last(self):
        """点名后面那句「分区已用 N%」是这一行的结论，得留在最后。

        数的是「处」，补数也跟着用「处」：「2 处占用值得处理（另有 1 项）」读起来像换了一件事在数。
        """
        svc = storage_service
        item = svc.doctor_item(storage_data(["分区已用 93%", "日志太大"], svc.SEV_RISK)[1])
        self.assertIn("另有 1 处", item["detail"])
        self.assertIn("分区已用 88%", item["detail"])
        self.assertLess(item["detail"].index("另有 1 处"), item["detail"].index("分区已用 88%"))


class TestSingleSource(unittest.TestCase):
    """源码级守卫：这句式不许在别处再抄一份，判词不许再直接取第一条标题。"""

    SERVICES = ("app/services/load_service.py", "app/services/pending_service.py",
                "app/services/memory_service.py", "app/services/storage_service.py",
                "app/services/login_audit_service.py")

    @staticmethod
    def code_of(rel: str) -> str:
        """剥掉注释与文档字符串后的源码：注释里可以叙述刚被删掉的那份副本。"""
        src = (ROOT / rel).read_text(encoding="utf-8")
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        return re.sub(r"^[ \t]*#.*$", "", src, flags=re.M)

    def test_no_hand_copied_rest_left(self):
        """盯的是「从列表长度现算一条补数」这个形状。

        load 的表格还有一句「另有 N 个没列出来」（数的是没列出的进程，不是判词条数），
        登录的 warn-only 那句是「没有要紧项，另有 3 项加固建议」（不点名，只报条数），
        两处语义不同，不该被这条检测误伤，所以锚点带上 `{len(`。
        """
        for rel in self.SERVICES:
            with self.subTest(file=rel):
                self.assertNotIn("另有 {len(", self.code_of(rel), f"{rel} 又手抄了一份补数")

    def test_no_index_zero_title_anywhere(self):
        for rel in ("app/util.py", *self.SERVICES):
            with self.subTest(file=rel):
                self.assertNotIn("[0]['title']", self.code_of(rel), f"{rel} 又直接取第一条标题")

    def test_every_service_uses_the_helper(self):
        for rel in self.SERVICES:
            with self.subTest(file=rel):
                self.assertIn("tallied(", self.code_of(rel))
                self.assertRegex(self.code_of(rel), r"from \.\.util import [^\n]*tallied")

    def test_helper_defined_once_and_owns_the_phrase(self):
        joined = "".join(self.code_of(rel) for rel in ("app/util.py", *self.SERVICES))
        self.assertEqual(joined.count("def tallied("), 1)
        self.assertEqual(joined.count('"（另有 {n - 1}'), 1, "「另有 N」这句只该在 tallied 里拼一次")

    def test_detectors_are_not_running_empty(self):
        """反证：把改之前的两种写法喂给同一套检测，必须都被拦住。"""
        bad_rest = 'more = f"，另有 {len(warns) - 1} 项" if len(warns) > 1 else ""'
        bad_named = "detail = f\"{len(risky)} 项：{risky[0]['title']}\""
        self.assertIn("另有 {len(", bad_rest)
        self.assertIn("[0]['title']", bad_named)
        code = self.code_of("app/services/pending_service.py")
        self.assertNotIn(bad_rest, code)
        self.assertNotIn("[0]['title']", code)


class TestPageShowsTheWholeVerdict(unittest.TestCase):
    """判词还得在页面上看得全：手机上没有 hover，一行的省略号等于没写。"""

    def test_detail_is_allowed_to_wrap(self):
        """折行的出处在批次㉕ 并进了 `.cp-prose`（全站一份），这里改认那一份。"""
        css = (ROOT.parent / "web" / "src" / "style.css").read_text(encoding="utf-8")
        rule = re.search(r"\.cp-prose\.cp-prose\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "判词的折行规则没了——整句话又会折回一行的省略号")
        self.assertIn("white-space: normal", rule.group(1))
        vue = (ROOT.parent / "web" / "src" / "views" / "Doctor.vue").read_text(encoding="utf-8")
        self.assertIn("'cell-sub cp-prose'", vue, "自检列表的判词行没挂上折行的类")


if __name__ == "__main__":
    unittest.main()
