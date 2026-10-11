"""批次⑤H：nginx / 域名 / certbot 这一簇的写入正确性与权限收口。

这一轮的问题都是"面板改了 nginx，但机器上的结果不是它说的那个"：
- 写配置是 `Path.write_text` 原地截断，且"写 → nginx -t → reload"这段没有互斥：
  nginx -t 校验的是整个 /etc/nginx，并发写的一方会在校验时读到另一方刚截断的空文件，
  把好配置判成语法错误，回滚掉的还是别人的文件；reload 撞上半份 server 块则是全站同时 502。
- remove_vhost 先 unlink 再 nginx -t，校验不过就直接把用户那份配置丢了
  （坏的原因通常在别的文件里，比如别的配置引用了本文件的 upstream）。
- 面板固定往 conf.d/panel-<name>.conf 写，而 Debian 的 include 顺序是 conf.d 先于
  sites-enabled：同一个域名如果已经写在手工维护的配置里，面板再生成一份就是两份
  server 块互相遮蔽，nginx 只认先加载的那份，手工配置静默失效且看不出原因。
- renew_certs 读 util.run 返回里根本没有的 r["err"]；issue_cert 把没校验的邮箱直接
  交给以 root 运行的 certbot argv；应用详情页的 nginx 配置全文（含内部端口、证书路径、
  本机目录结构）对只读账号开放，而同样的内容在「网站与证书」页是 admin only。
- 改名的应用先拆旧 vhost 再建新的：新的没过 nginx -t 时旧文件已经没了，站点当场下线，
  界面却只剩一句"下次部署会补上"。
"""

import asyncio
import inspect
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_5h_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch5h_roots_"))

from app import (
    config,  # noqa: E402
    util,  # noqa: E402
)
from app.routers import apps as apps_router  # noqa: E402
from app.services import apps_service, nginx_ops  # noqa: E402


def _req(role="admin", sub="tester"):
    async def _json():
        return {}

    return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub=sub), json=_json, query_params={})


class _ConfDir(unittest.TestCase):
    """把 CONF_DIR 与 config.NGINX_CONF_DIRS 一起挪进临时目录。

    少挪一个就会真的往 /etc/nginx 里写半截 server 块，测试机上那台真 nginx 会读到它。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_5h_conf_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._dirs = config.NGINX_CONF_DIRS
        config.NGINX_CONF_DIRS = [self.dir]
        self.addCleanup(setattr, config, "NGINX_CONF_DIRS", self._dirs)
        p = mock.patch.object(nginx_ops, "CONF_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        # loaded_conf_paths 有 15 秒 TTL 缓存，用例之间必须清掉，否则上一个用例里
        # "nginx 加载了哪些文件"会被下一个用例当成事实。
        nginx_ops._loaded_cache = None
        self.addCleanup(setattr, nginx_ops, "_loaded_cache", None)
        # 证书索引与 nginx 版本缓存同理；另外把 TLS_DIR 指向空目录，
        # 否则 apply_vhost 会真的去读 /etc/letsencrypt/live 并为每张在线证书起一个 openssl。
        self.tls = tempfile.mkdtemp(prefix="cp_5h_tls_")
        self.addCleanup(shutil.rmtree, self.tls, True)
        p2 = mock.patch.object(nginx_ops, "TLS_DIR", self.tls)
        p2.start()
        self.addCleanup(p2.stop)
        nginx_ops._cert_cache = None
        nginx_ops._ver_cache = None
        self.addCleanup(setattr, nginx_ops, "_cert_cache", None)
        self.addCleanup(setattr, nginx_ops, "_ver_cache", None)


async def _ok(**_kw):
    return {"code": 0, "out": "syntax is ok"}


# ---------------------------------------------------------------- 原子写与互斥
class TestAtomicConfWrite(_ConfDir):
    def test_nginx_test_reads_a_complete_file(self):
        """nginx -t 那一刻文件必须是完整的，且写完不留临时文件。"""
        path = f"{self.dir}/panel-new.conf"
        seen = {}

        async def rec(cmd, args, **kw):
            if cmd == "nginx" and args == ["-t"]:
                seen["content"] = Path(path).read_text()
            return await _ok()

        with mock.patch.object(nginx_ops, "run", rec):
            asyncio.run(nginx_ops.save_app_config(path, "server {\n    listen 80;\n}\n"))
        self.assertEqual(seen["content"], "server {\n    listen 80;\n}\n")
        self.assertEqual(os.listdir(self.dir), ["panel-new.conf"], "写完只剩目标文件")

    def test_failure_leaves_no_temp_file(self):
        path = f"{self.dir}/panel-bad.conf"

        async def boom(cmd, args, **kw):
            return {"code": 1, "out": "nginx: [emerg] bad"} if cmd == "nginx" else await _ok()

        with mock.patch.object(nginx_ops, "run", boom), self.assertRaises(RuntimeError):
            asyncio.run(nginx_ops.save_app_config(path, "server { broken"))
        self.assertFalse(os.path.exists(path))
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_concurrent_writes_are_serialized(self):
        """两个写路径不能在校验时读到对方的半成品：临界区必须整体串行。"""
        a, b = f"{self.dir}/panel-a.conf", f"{self.dir}/panel-b.conf"
        seq: list[str] = []

        async def slow(cmd, args, **kw):
            # 没有锁时这 20ms 足够让两个任务的 nginx -t 交叠在一起
            await asyncio.sleep(0.02)
            seq.append(cmd)
            return {"code": 0, "out": "syntax is ok"}

        async def both():
            await asyncio.gather(
                nginx_ops.save_app_config(a, "server {}"),
                nginx_ops.save_app_config(b, "server {}"),
            )

        with mock.patch.object(nginx_ops, "run", slow):
            asyncio.run(both())
        self.assertEqual(seq, ["nginx", "systemctl", "nginx", "systemctl"], f"临界区交叠了：{seq}")

    def test_lock_is_rebound_per_event_loop(self):
        """测试与 CLI 每次都新开事件循环，跨循环复用同一把锁会直接 RuntimeError。"""
        async def use():
            async with nginx_ops._nginx_section():
                return True

        self.assertTrue(asyncio.run(use()))
        self.assertTrue(asyncio.run(use()))


# ---------------------------------------------------------------- 域名归属冲突
class TestDomainShadowing(_ConfDir):
    def _manual(self, domain: str) -> str:
        """造一个"手工维护、且真的被 nginx 加载着"的配置文件。"""
        p = f"{self.dir}/choyeon-video"
        Path(p).write_text(
            f"server {{\n    listen 443 ssl;\n    server_name {domain} _;\n"
            "    proxy_pass http://127.0.0.1:3100;\n}\n"
        )
        return p

    async def _apply(self, loaded: list[str], domain: str, name: str):
        async def fake_run(cmd, args, **kw):
            if cmd == "nginx" and args == ["-T"]:
                if not loaded:
                    return {"code": 1, "out": "nginx: configuration file test failed"}
                out = "".join(f"# configuration file {p}:\n" for p in loaded)
                return {"code": 0, "out": out}
            return {"code": 0, "out": "syntax is ok"}

        with mock.patch.object(nginx_ops, "run", fake_run):
            await nginx_ops.apply_vhost({"name": name, "domain": domain, "port": 3100})

    def test_refuses_to_write_a_second_block_for_the_same_domain(self):
        domain = f"shadow{os.getpid()}.example.com"
        other = self._manual(domain)
        with self.assertRaises(nginx_ops.DomainOwnedElsewhere) as ctx:
            asyncio.run(self._apply([other], domain, "vid"))
        self.assertIn(domain, str(ctx.exception))
        self.assertFalse(os.path.exists(nginx_ops.vhost_path("vid")), "拒绝时一个字节都不该落盘")

    def test_own_file_is_not_a_conflict(self):
        """重复套用同一份 panel-*.conf（第二次部署、连点「应用」）不该被自己挡住。"""
        domain = f"twice{os.getpid()}.example.com"
        asyncio.run(self._apply([], domain, "vid"))          # nginx -T 不可用 → 不判断
        target = nginx_ops.vhost_path("vid")
        self.assertIn(domain, Path(target).read_text())
        asyncio.run(self._apply([target, f"{self.dir}/nginx.conf"], domain, "vid"))
        self.assertIn(domain, Path(target).read_text())

    def test_nginx_t_unavailable_does_not_refuse(self):
        """查不了 ≠ 没冲突，但也不能因为查不了就拒绝工作；照常写入，让 nginx -t 兜底。"""
        asyncio.run(self._apply([], f"unknown{os.getpid()}.example.com", "vid"))
        self.assertTrue(os.path.exists(nginx_ops.vhost_path("vid")))

    def test_conflict_type_is_a_runtime_error_for_existing_callers(self):
        self.assertTrue(issubclass(nginx_ops.DomainOwnedElsewhere, RuntimeError))

    def test_deploy_logs_conflict_instead_of_failing(self):
        """跑了几分钟的部署不该因为这次域名冲突被判失败，但要在日志里留下这一行。"""
        src = inspect.getsource(apps_service._run_deployment)
        self.assertIn("except nginx_ops.DomainOwnedElsewhere", src)
        self.assertLess(src.index("except nginx_ops.DomainOwnedElsewhere"), src.index("==> 重启并确认服务"),
                        "冲突处理必须收在域名那一段里，而不是漏到最外层把部署判成失败")


# ---------------------------------------------------------------- remove_vhost
class TestRemoveVhostSkipsWhenAbsent(_ConfDir):
    def test_no_file_means_no_nginx_call(self):
        calls = []

        async def rec(cmd, args, **kw):
            calls.append(cmd)
            return {"code": 0, "out": "ok"}

        with mock.patch.object(nginx_ops, "run", rec):
            asyncio.run(nginx_ops.remove_vhost(f"nothing{os.getpid()}"))
        self.assertEqual(calls, [], "从未配过域名的应用被清理时，不该白跑一次全站 reload")


# ---------------------------------------------------------------- certbot
class TestRenewCertsErrorShape(unittest.TestCase):
    def test_empty_output_does_not_raise_keyerror(self):
        """util.run 只返回 {code, out}：取 r["err"] 等于让"out 恰好为空"的失败变成 KeyError。"""

        async def fake_run(*_a, **_k):
            return {"code": 1, "out": ""}

        with mock.patch.object(nginx_ops, "run", fake_run), self.assertRaises(RuntimeError) as ctx:
            asyncio.run(nginx_ops.renew_certs())
        self.assertIn("续期失败", str(ctx.exception))


class TestIssueCertEmail(unittest.TestCase):
    def _args(self, email):
        seen = {}

        async def rec(cmd, args, **kw):
            seen["args"] = args
            return {"code": 0, "out": "ok"}

        with mock.patch.object(nginx_ops, "run", rec):
            asyncio.run(nginx_ops.issue_cert("a.example.com", email))
        return seen["args"]

    def test_valid_email_goes_to_argv(self):
        args = self._args("ops@example.com")
        self.assertEqual(args[args.index("-m") + 1], "ops@example.com")

    def test_blank_email_falls_back_to_no_email_registration(self):
        for blank in (None, "", "   "):
            self.assertIn("--register-unsafely-without-email", self._args(blank))

    def test_malformed_email_is_rejected_before_argv(self):
        # 以 - 开头的值会把后面的选项吃掉（certbot 以 root 跑，还要拿它注册 ACME 账户），
        # 带空白 / 多个 @ 的同样不该原样递进去
        for bad in ("--no-redirect", "a b@example.com", "a@@example.com", "x" * 70 + "@example.com"):
            with self.assertRaises(RuntimeError, msg=bad):
                asyncio.run(nginx_ops.issue_cert("a.example.com", bad))


class TestIsEmail(unittest.TestCase):
    def test_accepted(self):
        for ok in ["a@b.co", "ops+x@sub.example.com", "first.last@choyeon.cc"]:
            self.assertTrue(util.is_email(ok), ok)

    def test_rejected(self):
        for bad in ["", "no-at-sign", "@example.com", "a@", "a@b", "a@b.", "a b@c.co", "a@b@c.co"]:
            self.assertFalse(util.is_email(bad), bad)


# ---------------------------------------------------------------- 改名不弄断站点
class TestRenameKeepsOldVhostUntilNewIsLive(unittest.IsolatedAsyncioTestCase):
    OLD = {"id": 8, "name": "before", "type": "node", "path": "/tmp/cp-5h", "port": 8125,
           "domain": "a.example.com", "start_cmd": "node x.js", "env": "[]"}

    async def _rename(self, apply_error=None):
        new = {**self.OLD, "name": "after"}
        removed: list[str] = []
        applied: list[dict] = []

        async def _remove(name):
            removed.append(name)

        async def _apply(spec):
            applied.append(spec)
            if apply_error:
                raise apply_error

        with ExitStack() as st:
            st.enter_context(mock.patch.object(apps_service, "is_active", lambda *_: asyncio.sleep(0, False)))
            st.enter_context(mock.patch.object(apps_service, "remove_unit"))
            st.enter_context(mock.patch.object(apps_service, "ensure_unit"))
            st.enter_context(mock.patch.object(apps_service, "write_env_file"))
            st.enter_context(mock.patch.object(apps_service.nginx_ops, "apply_vhost", _apply))
            st.enter_context(mock.patch.object(apps_service.nginx_ops, "remove_vhost", _remove))
            out = await apps_service.apply_edited(self.OLD, new)
        return out, removed, applied

    async def test_old_vhost_survives_a_failed_new_one(self):
        err = nginx_ops.DomainOwnedElsewhere("域名已由手工配置定义")
        out, removed, _applied = await self._rename(apply_error=err)
        self.assertEqual(removed, [], "新的还没生效就拆旧的，等于自己把站点弄下线")
        self.assertFalse(out["applied"])
        self.assertIn("托管", out["note"])

    async def test_old_vhost_removed_after_the_new_one_is_live(self):
        out, removed, applied = await self._rename()
        self.assertEqual(applied[0]["name"], "after")
        self.assertEqual(removed, ["before"], "新配置已经建好，旧的必须接着拆掉，别留孤儿")
        self.assertTrue(out["applied"])


# ---------------------------------------------------------------- 只读账号看什么
class TestAppNginxViewerView(unittest.IsolatedAsyncioTestCase):
    OWN = "/etc/nginx/conf.d/panel-app.conf"

    async def _get(self, role):
        app = {"id": 1, "name": "app", "domain": "app.example.com", "port": 3000}
        configs = [{"file": self.OWN, "name": "panel-app.conf", "content": "server { # 内部端口 5432\n}",
                    "analysis": {"serverNames": ["app.example.com"], "ssl": True}}]
        with ExitStack() as st:
            st.enter_context(mock.patch.object(apps_service, "get_app", lambda *_: app))
            st.enter_context(mock.patch.object(apps_router, "_nginx_scope",
                                               mock.AsyncMock(return_value=(3000, "app.example.com"))))
            st.enter_context(mock.patch.object(nginx_ops, "find_app_configs", lambda *_a: configs))
            return await apps_router.app_nginx(1, _req(role=role))

    async def test_admin_gets_the_full_text(self):
        r = await self._get("admin")
        self.assertEqual(r["configs"][0]["file"], self.OWN)
        self.assertIn("5432", r["configs"][0]["content"])

    async def test_viewer_gets_analysis_only(self):
        # 与 /api/sites/{domain}/config 同一口径：vhost 原文里有内部端口、证书路径和
        # 本机目录结构，这些不是"只读账号该看的"。
        c = (await self._get("viewer"))["configs"][0]
        self.assertIsNone(c["content"])
        self.assertEqual(c["file"], "panel-app.conf")
        self.assertEqual(c["analysis"]["serverNames"], ["app.example.com"])

    async def test_viewer_still_sees_the_row_count(self):
        r = await self._get("viewer")
        self.assertEqual(len(r["configs"]), 1, "收掉正文不能顺手把配置项也变没")


class TestLoadedPathsCache(_ConfDir):
    def test_invalidate_clears_loaded_cache(self):
        nginx_ops._loaded_cache = (0.0, {"/etc/nginx/sites-enabled/x"})
        nginx_ops.invalidate_domain_cache()
        self.assertIsNone(nginx_ops._loaded_cache, "写完配置后「哪些文件在跑」必须重新问过 nginx")

    def test_temp_files_are_not_scanned_as_sites(self):
        # 原子写的临时文件只有进程被杀在中间才会残留，不该出现在站点扫描里
        Path(f"{self.dir}/panel-x.conf.123.tmp").write_text("server {}")
        self.assertNotIn("panel-x.conf.123.tmp", [f for _p, f in nginx_ops._iter_conf_files()])


if __name__ == "__main__":
    unittest.main()
