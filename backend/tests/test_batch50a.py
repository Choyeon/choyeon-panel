"""批次㊿A：「这个进程属于哪个 systemd 单元」只有一个出处（systemd_ops）。

定稿前在生产机上量到的事实：同一份 /proc/<pid>/cgroup，站内三个读者给出三个答案。
13 个候选进程（13/13）在「改动未生效」页被归到 `user@0.service`——那是 root 的 systemd
用户管理器，页面劝人去 `systemctl restart user@0.service`，重启它等于踢掉这台机器上所有
ssh 会话。归错的原因不是解析写得糙，而是三处各写一份、其中一份拿的是「整行第一个 .service」，
另外两份拿的是「叶子」，而这两种取法各覆盖不了一个真实形态。

所以这一层锁的是：

- **最深带后缀的那一层**：`0::/user.slice/user-0.slice/user@0.service/init.scope` 的答案是
  `init.scope`（scope），不是整行里第一个出现的 `.service`；而
  `0::/system.slice/systemd-udevd.service/udev`（单元目录下再开子组，叶子没后缀）的答案仍然是
  `systemd-udevd.service`。只看叶子和只看第一个后缀各错一半，`UnitOfCgroupTests` 用生产机原样
  抓回的 cgroup 把两侧都钉住，包括根 cgroup、v1 多行、整条都没有后缀的容器路径。
- **「面板自己」必须是一个 .service**：CLI 和手工 uvicorn 恰好跑在 sshd 的 scope 里，那里没有
  「我属于哪个单元」这个事实，于是 `panel_unit()` 只认 .service 叶子，其余退回本仓库安装的
  单元名。`PanelUnitTests` 同时钉这条回退——把它改成「有叶子就用叶子」，自检项会在 ssh 会话
  里把 `session-9855.scope` 当成面板服务并报 fail。
- **兄弟单元不等于面板自己**：旧的自我标记是 `tail.startswith(self_unit)`，于是
  `choyeon-panel-worker.service` 会被标成「面板自己」，跟着给出「改 deploy/choyeon-panel.service
  的 CPUQuota 再 upgrade --no-pull」这种会把人引向错误文件的处置。`UnderUnitTests` 钉住
  「前缀相同的兄弟不算、自己下面的子 scope 算、根外面不算」。
- **同一个事实的口径按调用点收窄，而不是按最宽的那个**：pending 只认 .service（处置动作是
  restart，scope 没有这个动作），load 另外认 scope（进程归因要如实显示它跑在会话里），
  memory 两者都要（内存是真占了）。三份收窄各有一条用例，防止有人「统一」成全认。
- **死读 env 不留第二份定义**：`CP_SELF_UNIT` 全线删除（它从未被任何脚本写进 .env，
  改了也不生效，而注释声称可以覆盖）；`PANEL_SERVICE` 字面量、`unit_of_cgroup` 定义、
  `/proc/self/cgroup` 读取点各只允许出现在 systemd_ops。`SingleSourceTests` 用源码 grep 钉住。

夹具里的 cgroup 字符串、`user@0.service` 与 `session-*.scope` 形态都是 2026-10-17 从生产机
（Debian 13 / cgroup v2 / systemd 257）的 /proc/[pid]/cgroup 原样抓回来的。
"""

from __future__ import annotations

import ast
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch50a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch50a_roots_"))

import unittest  # noqa: E402

from app.services import load_service as ls  # noqa: E402
from app.services import memory_service as ms  # noqa: E402
from app.services import pending_service as ps  # noqa: E402
from app.services import systemd_ops as so  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _code_lines(text: str) -> str:
    """只剩可执行行：docstring 与注释里的旧写法不算「又用回了旧写法」。

    `_under_unit` 的 docstring 明确记着旧写法长什么样（那是这一批的证据），所以守卫必须读 AST
    把文档与注释剔掉，否则这条用例永远在报一个说明文字。
    """
    tree = ast.parse(text)
    drop: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = []
    for no, line in enumerate(text.splitlines(), 1):
        if no in drop or line.strip().startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)

# 生产机实测样本（2026-10-17）：面板服务、面板应用服务、root 的用户管理器、sshd 会话、
# 内核线程（根 cgroup）、容器里无后缀的路径。
CG_PANEL = "0::/system.slice/choyeon-panel.service\n"
CG_APP = "0::/system.slice/panel-rosetta-backend.service\n"
CG_USERMGR = "0::/user.slice/user-0.slice/user@0.service/init.scope\n"
CG_SSHD = "0::/user.slice/user-0.slice/user@0.service/session-9855.scope\n"
CG_NESTED = "0::/system.slice/choyeon-panel.service/crio-conmon-1f2e.scope\n"
CG_SUBCGROUP = "0::/system.slice/systemd-udevd.service/udev\n"
CG_ROOT = "0::/\n"
CG_BARE_SLICE = "0::/user.slice/user-0.slice\n"
CG_NO_SUFFIX = "0::/kubepods/besteffort/pod6c1d/1f2e3d4c\n"
CG_SLICE = "0::/system.slice/machine.slice\n"
CG_V1 = ("12:memory:/system.slice/choyeon-panel.service\n"
         "11:pids:/system.slice/choyeon-panel.service\n")


class UnitOfCgroupTests(unittest.TestCase):
    """解析规则：只有叶子是这一行的答案，其余组件属于祖先。"""

    def test_service_叶子(self):
        self.assertEqual(so.unit_of_cgroup(CG_PANEL), ("choyeon-panel.service", "service"))
        self.assertEqual(so.unit_of_cgroup(CG_APP), ("panel-rosetta-backend.service", "service"))

    def test_usermgr_的叶子是_scope_不是第一个_service(self):
        """本批的主案发现场。

        `user@0.service/init.scope` 这一行里确实有 `.service`，但它是在管理 root 用户会话的
        那个 systemd 实例，不是这个进程自己的单元。整行正则抓第一个 `.service` 的旧写法把
        每个 ssh 会话里的进程都归到了它名下。
        """
        self.assertEqual(so.unit_of_cgroup(CG_USERMGR), ("init.scope", "scope"))
        self.assertEqual(so.unit_of_cgroup(CG_SSHD), ("session-9855.scope", "scope"))

    def test_服务下面再开的_scope_仍归到叶子(self):
        """单元目录里再分子组（面板自己 fork 出来的 scope）：叶子才是这个进程的位置。"""
        self.assertEqual(so.unit_of_cgroup(CG_NESTED), ("crio-conmon-1f2e.scope", "scope"))

    def test_单元目录下的子组归到那个单元(self):
        """`systemd-udevd.service/udev`：叶子没有后缀，但归属是明确的。

        只看叶子会把一台正在跑的 udevd 报成「归不到单元」，从进程归因表里整个消失。
        """
        self.assertEqual(so.unit_of_cgroup(CG_SUBCGROUP), ("systemd-udevd.service", "service"))

    def test_slice_也是单元但处置动作不同(self):
        """叶子是 .slice 时如实给出 slice：它是分组，不是能 restart 的服务。

        `0::/user.slice/user-0.slice` 这种「进程直接待在分组里」的形态是真的存在的（user@.service
        还没起来时的会话进程）。解析层不许把它折成 None，否则页面显示成「没有归属」；收窄由调用点做。
        """
        self.assertEqual(so.unit_of_cgroup(CG_SLICE), ("machine.slice", "slice"))
        self.assertEqual(so.unit_of_cgroup(CG_BARE_SLICE), ("user-0.slice", "slice"))

    def test_读不出归属的几种形态各给一个可区分的答案(self):
        self.assertEqual(so.unit_of_cgroup(CG_ROOT), (None, "root"))
        self.assertEqual(so.unit_of_cgroup(CG_NO_SUFFIX), (None, "other"))
        # v1 的多行没有 `0::`：本项目明确不做兼容猜测（把 v1 字段当 v2 读会得到一张假表）。
        self.assertEqual(so.unit_of_cgroup(CG_V1), (None, "none"))
        self.assertEqual(so.unit_of_cgroup(""), (None, "none"))
        self.assertEqual(so.unit_of_cgroup(None), (None, "none"))

    def test_尾斜杠与空行不影响判定(self):
        self.assertEqual(so.unit_of_cgroup("0::/system.slice/choyeon-panel.service//"),
                         ("choyeon-panel.service", "service"))
        self.assertEqual(so.unit_of_cgroup("\n\n0::/system.slice/x.service\n"),
                         ("x.service", "service"))

    def test_后缀表只有一份(self):
        """memory_service 以前自带一份 `_UNIT_SUFFIXES`，两处必须永远是同一个对象。"""
        self.assertIs(ms._UNIT_SUFFIXES, so.UNIT_SUFFIXES)


class PanelUnitTests(unittest.TestCase):
    """「面板自己」的判词：只认真正的 .service 单元，其余情况退回安装时的那个名字。"""

    def test_服务里跑时用cgroup上的名字(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=CG_PANEL)):
            self.assertEqual(so.panel_unit(), "choyeon-panel.service")

    def test_改名后的单元跟着走(self):
        """管理员把 unit 改名（deploy/ 里那份 + 重载）后，站内所有「面板自己」的判词一起改。

        这正是删掉 `CP_SELF_UNIT` 的依据：手工环境变量能做到的覆盖，本进程的 cgroup 本来就
        更权威，而且它不会像那个变量一样改了不生效。
        """
        with mock.patch("builtins.open", mock.mock_open(read_data="0::/system.slice/cp.service\n")):
            self.assertEqual(so.panel_unit(), "cp.service")

    def test_ssh_会话里退回安装名(self):
        """CLI 与手工 uvicorn 就跑在 sshd 的 scope 里。

        那时「我属于哪个单元」这个问题没有答案：session-9855.scope 不是面板服务，把它当答案
        会让自检项去查一个不存在的服务并报 fail。这两处要查的从来是「面板服务」。
        """
        with mock.patch("builtins.open", mock.mock_open(read_data=CG_SSHD)):
            self.assertEqual(so.panel_unit(), so.PANEL_UNIT)

    def test_容器与读不到时退回安装名(self):
        for raw in (CG_ROOT, CG_NO_SUFFIX, CG_V1, ""):
            with self.subTest(raw=raw.strip() or "(空)"):
                with mock.patch("builtins.open", mock.mock_open(read_data=raw)):
                    self.assertEqual(so.panel_unit(), so.PANEL_UNIT)

    def test_文件读不到不抛(self):
        """非 Linux（开发机、CI 的某些 runner）上没有 /proc/self/cgroup：拿回默认名，不是异常。"""
        with mock.patch("builtins.open", side_effect=OSError("no proc")):
            self.assertEqual(so.panel_unit(), so.PANEL_UNIT)


class UnderUnitTests(unittest.TestCase):
    """自我标记按路径分段判定，不按名字前缀。"""

    def _u(self, path: str, unit: str = "choyeon-panel.service") -> bool:
        return ls._under_unit(f"{ls.CG_ROOT}/{path}", unit)

    def test_单元目录本身(self):
        self.assertTrue(self._u("system.slice/choyeon-panel.service"))

    def test_前缀相同的兄弟单元不算(self):
        self.assertFalse(self._u("system.slice/choyeon-panel-worker.service"))
        self.assertFalse(self._u("system.slice/choyeon-panel@1.service"))

    def test_单元下面再开的子组算(self):
        self.assertTrue(self._u("system.slice/choyeon-panel.service/session-c1.scope"))
        self.assertTrue(self._u("system.slice/choyeon-panel.service/deepER/sub"))

    def test_别的分支与根外不算(self):
        self.assertFalse(self._u("user.slice/user-0.slice/session-9855.scope"))
        self.assertFalse(self._u("system.slice/panel-choyeon-panel.service"))
        self.assertFalse(ls._under_unit("/tmp/anything", "choyeon-panel.service"))

    def test_空段与点段不参与比对(self):
        self.assertTrue(ls._under_unit("/sys/fs/cgroup//system.slice/./choyeon-panel.service",
                                       "choyeon-panel.service"))


class ReaderWiringTests(unittest.TestCase):
    """三个读者各自的收窄，以及归属判词真的来自 systemd_ops。"""

    def test_pending_只认_service(self):
        """处置动作是 `systemctl restart <unit>`，而 scope 没有「重启它」这个动作。"""
        for raw, want in ((CG_PANEL, "choyeon-panel.service"),
                          (CG_APP, "panel-rosetta-backend.service"),
                          (CG_USERMGR, ""),
                          (CG_SSHD, ""),
                          (CG_NESTED, ""),
                          (CG_SUBCGROUP, "systemd-udevd.service"),
                          (CG_SLICE, ""),
                          (CG_NO_SUFFIX, "")):
            with self.subTest(raw=raw.strip()):
                with mock.patch.object(ps, "_read", return_value=raw):
                    self.assertEqual(ps._unit_of(1), want)

    def test_pending_不再把ssh会话归到用户管理器(self):
        """主缺陷的直接回归：旧写法返回 user@0.service，页面劝人重启它（会踢掉全部 ssh）。"""
        with mock.patch.object(ps, "_read", return_value=CG_SSHD):
            self.assertNotIn("user@0.service", ps._unit_of(1))

    def test_load_另外认_scope(self):
        """负载归因要如实显示「这个进程跑在某个会话里」，留空反而像没归属。"""
        for raw, want in ((CG_PANEL, "choyeon-panel.service"),
                          (CG_USERMGR, "init.scope"),
                          (CG_SSHD, "session-9855.scope"),
                          (CG_SUBCGROUP, "systemd-udevd.service"),
                          (CG_SLICE, ""),          # 裸 slice 是分组，不是单元
                          (CG_ROOT, "")):
            with self.subTest(raw=raw.strip()):
                with mock.patch.object(ls, "_read", return_value=raw):
                    self.assertEqual(ls._unit_of(1), want)

    def test_memory_的行按同一个self_unit标记(self):
        """临时造一棵 cgroup 目录树：面板那一行无条件带 self=True，别人没有。"""
        root = tempfile.mkdtemp(prefix="cp50a_cg_")
        unit = "renamed-panel.service"
        base = Path(root) / "system.slice" / unit
        base.mkdir(parents=True)
        (base / "memory.current").write_text("41943040\n", encoding="utf-8")
        (base / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
                                            encoding="utf-8")
        mine = ms._unit_row(root, unit, None, unit)
        other = ms._unit_row(root, unit, None, "choyeon-panel.service")
        self.assertTrue(mine["self"])
        self.assertFalse(other["self"])
        self.assertEqual(mine["current"], 41943040)
        self.assertEqual(mine["kind"], "service")

    def test_限流处置用的是当前单元名而不是字面量(self):
        """改名后的面板被自己的 CPUQuota 限流时，fix 里必须出现真名。

        旧写法写死 choyeon-panel.service，指向一个根本不存在的文件，管理员照着改
        永远改不到正在限流的那个配额。
        """
        row = {"unit": "cp.service", "self": True, "window_pct": 60.0, "quota_cores": 0.8,
               "period_us": 100000, "window_throttled": 60, "window_periods": 100,
               "total_throttled": 60, "total_throttled_usec": 6_000_000}
        quota = {"ok": True, "units": [row], "self_unit": "cp.service"}
        findings = ls._findings({"ok": False, "reason": "x"}, {"ok": False, "reason": "x"},
                               {"ok": False, "reason": "x"}, {"ok": False, "reason": "x"},
                               quota, 2)
        f = next(i for i in findings if i["id"] == "throttle:cp.service")
        self.assertEqual(f["severity"], "risk")
        self.assertIn("systemctl show cp -p CPUQuotaPerSecUSec", f["fix"])
        self.assertIn("deploy/cp.service", f["fix"])
        self.assertNotIn("choyeon-panel", f["fix"])

    def test_兄弟单元被限流时不劝人改面板的配额(self):
        row = {"unit": "choyeon-panel-worker.service", "self": False, "window_pct": 60.0,
               "quota_cores": 0.5, "period_us": 100000, "window_throttled": 60,
               "window_periods": 100, "total_throttled": 60, "total_throttled_usec": 6_000_000}
        quota = {"ok": True, "units": [row], "self_unit": "choyeon-panel.service"}
        findings = ls._findings({"ok": False, "reason": "x"}, {"ok": False, "reason": "x"},
                               {"ok": False, "reason": "x"}, {"ok": False, "reason": "x"},
                               quota, 2)
        f = next(i for i in findings if i["id"] == "throttle:choyeon-panel-worker.service")
        self.assertIn("systemctl show choyeon-panel-worker.service", f["fix"])
        self.assertNotIn("upgrade --no-pull", f["fix"])
        self.assertNotIn("面板自己", f["title"])


class SingleSourceTests(unittest.TestCase):
    """一个事实一个出处：源码层面钉住，防止下一个读者又抄一份。"""

    def _app_files(self):
        for p in (REPO / "backend/app").rglob("*.py"):
            yield p, p.read_text(encoding="utf-8", errors="ignore")

    def test_CP_SELF_UNIT_全线不存在(self):
        """那个环境变量从未被任何脚本写进 .env，改了也不生效，注释却声称可以覆盖。"""
        hits = [str(p) for p, text in self._app_files() if "CP_SELF_UNIT" in text]
        hits += [str(p) for p in (REPO / "web/src").rglob("*")
                 if p.is_file() and "CP_SELF_UNIT" in p.read_text(encoding="utf-8", errors="ignore")]
        hits += [str(p) for p in (REPO / "scripts").glob("*.sh")
                 if "CP_SELF_UNIT" in p.read_text(encoding="utf-8", errors="ignore")]
        self.assertEqual(hits, [], f"CP_SELF_UNIT 又出现了：{hits}")

    def test_解析函数只有systemd_ops里一份(self):
        hits = [str(p) for p, text in self._app_files()
                if re.search(r"def _?unit_of_cgroup\(", text)]
        self.assertEqual(hits, [str(REPO / "backend/app/services/systemd_ops.py")],
                         f"unit_of_cgroup 出现了 {len(hits)} 份定义")

    def test_读自身cgroup的地方只有panel_unit(self):
        hits = [str(p) for p, text in self._app_files() if "/proc/self/cgroup" in text]
        self.assertEqual(hits, [str(REPO / "backend/app/services/systemd_ops.py")],
                         "「面板自己算哪个单元」必须只有一个读者：别处读 /proc/self/cgroup "
                         "就会再次分裂成两套答案")

    def test_服务名字面量只在systemd_ops定义(self):
        hits = [str(p) for p, text in self._app_files()
                if re.search(r"PANEL_SERVICE\s*=\s*[\"']choyeon-panel", text)]
        self.assertEqual(hits, [str(REPO / "backend/app/services/systemd_ops.py")],
                         f"PANEL_SERVICE 被重新定义：{hits}")

    def test_三个读者都走systemd_ops(self):
        for name in ("load_service.py", "memory_service.py", "pending_service.py"):
            with self.subTest(name=name):
                text = (REPO / "backend/app/services" / name).read_text(encoding="utf-8")
                self.assertIn("systemd_ops.", text, f"{name} 不再问 systemd_ops")
                code = _code_lines(text)
                self.assertNotIn("startswith(self_unit)", code,
                                 f"{name} 又用回前缀匹配了：兄弟单元会被标成面板自己")

    def test_纳管unit白名单也用同一个名字(self):
        """定时任务只许 restart 纳管 unit；面板自身那一项必须来自 systemd_ops，
        否则这里是一份会随改名而失效的影子清单。"""
        text = (REPO / "backend/app/services/cron_service.py").read_text(encoding="utf-8")
        self.assertNotIn('PANEL_SERVICE = "choyeon-panel"', text)
        self.assertIn("systemd_ops.PANEL_UNIT", text)

    def test_default_名字与deploy目录里的unit一致(self):
        """PANEL_UNIT 是回退答案，它必须真的等于本仓库安装的那份 unit 文件名，
        否则非 systemd 环境里的自检会去查一个不存在的服务。"""
        unit_file = (REPO / "deploy" / so.PANEL_UNIT)
        self.assertTrue(unit_file.exists(), f"deploy/{so.PANEL_UNIT} 不存在，回退名是错的")


if __name__ == "__main__":
    unittest.main()
