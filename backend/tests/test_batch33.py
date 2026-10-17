"""批次㉝：同一个目标上的写操作，三个入口必须互相看得见。

面板的写操作有三个入口——网页请求、`choyeonctl`、systemd timer/oneshot 起的独立进程。
它们各跑自己的事件循环，所以此前"同一个对象不许有两个改动现场的操作在跑"这件事
是靠两套都不完全的机制凑出来的：

- `asyncio.Lock` 只在这一个进程里有效，网页防得住网页，防不住 timer 起的子进程；
- `deployments.status='running'` 是"上次有人记得写、下次没人记得清"的状态位，进程被
  OOM 或升级杀掉就永远停在 running，而这个应用从此删不掉、回滚不了；
- 回滚此前不在**任何**互斥里（它只读 running 行，自己一行都不写），点完回滚再点部署，
  两条 `git reset --hard` 与两次服务重启叠在同一个工作区上；
- 恢复的锁键是 `kind:target`，同一个应用的两条备份任务（目录类存应用名、SQLite 类存
  app_id）拿到两把锁，两次恢复可以同时 stop 同一个 unit、往同一个目录写；
- 定时备份那一轮由 systemd 起独立进程，它和"立即备份"之间没有任何互斥，同一秒内还会
  写出同名产物互相覆盖。

这一批收进一个原语：`app/locks.py` 的 flock 目标锁（内核在进程死亡时释放，不需要任何人
记得清），键统一由 `key_app(id)` 出，deploy/sync/rollback/delete/restore 按 app id 抢同一把；
恢复在抢锁前把 target 解析成 app id；备份产物名用 `O_CREAT|O_EXCL` 原子占位；runner 的进程
入口自己抢 `backup-<id>`；定时任务那一侧的 ExecStart 套 `flock -n -E 0`，让"手动正在跑、
timer 恰好到点"的重叠也被挡，又不会因为 oneshot 非 0 退出在 failed-units 这个 crit 告警上
挂一条假故障。被杀在停服务与覆盖之间的恢复，由启动对账按 unit 实际状态逐条说话。
unit 文件只在任务增删改时渲染，所以那条 flock 对装机时就存在的任务是"代码里写了、盘上没有"，
这一批给它补上启动对账 `sync_all_units`（备份那侧的 `sync_all_timers` 早就是这个形状）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch33_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch33_roots_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch33_bak_"))

from app import backup_runner as R  # noqa: E402
from app import (
    config,  # noqa: E402
    locks,  # noqa: E402
)
from app import database as dbm  # noqa: E402
from app.services import apps_service  # noqa: E402
from app.services import backup_service as B
from app.services import cron_service as C  # noqa: E402

BACKEND = Path(config.BASE, "backend")

# 另一个进程里真去 flock 那个文件，拿到锁后打一行回执再睡着。
# 跨进程这件事只能在子进程里证：本进程自锁自问，测到的只是 fcntl 的语义。
_HOLD = textwrap.dedent(
    """
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(1, b"held\\n")
    time.sleep(float(sys.argv[2]))
    """
)

# 子进程侧的探针：它拿到的是与父进程不同的 open file description，
# 所以"抢不到"才是跨进程互斥的证据，而不是同一个 fd 重复加锁。
_PROBE = textwrap.dedent(
    """
    import json, sys
    from app import locks
    try:
        h = locks.acquire(sys.argv[1], "定时备份", "system")
    except RuntimeError as e:
        print(json.dumps({"ok": False, "msg": str(e)}))
    else:
        h.release()
        print(json.dumps({"ok": True}))
    """
)


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def _probe(key: str) -> dict:
    r = subprocess.run(
        [sys.executable, "-c", _PROBE, key],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
    )
    return json.loads((r.stdout or "").strip() or "{}")


def _mkapp(**kw) -> int:
    row = {
        "name": _uid("a"), "repo_url": "https://github.com/u/r.git", "branch": "main",
        "path": tempfile.mkdtemp(prefix="cp_b33_app_"), "start_cmd": "node x",
    }
    row.update(kw)
    return dbm.execute(
        "INSERT INTO apps(name,type,repo_url,branch,path,start_cmd) VALUES(?,'node',?,?,?,?)",
        (row["name"], row["repo_url"], row["branch"], row["path"], row["start_cmd"]),
    )


def _mkbk(kind: str, target, keep: int = 7) -> int:
    return dbm.execute(
        "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) VALUES(?,?,'manual',3,30,?,1)",
        (kind, str(target), keep),
    )


def _state(state: str, *, missing: bool = False, busy: bool = False):
    async def _unit_state(_unit):
        return {"state": state, "missing": missing, "busy": busy, "active": state == "active"}

    return _unit_state


class _Db(unittest.TestCase):
    """真库真表：这批测的是"抢锁的时机"，把 query_one 换成一刀切的替身会连守卫一起测掉。"""

    def setUp(self):
        self._apps: list[int] = []
        self._bks: list[int] = []
        self.bkdir = tempfile.mkdtemp(prefix="cp_b33_bk_")
        self.addCleanup(shutil.rmtree, self.bkdir, True)
        os.makedirs(self.bkdir, exist_ok=True)
        for mod in (B, R):
            p = mock.patch.object(mod, "BACKUP_DIR", self.bkdir)
            p.start()
            self.addCleanup(p.stop)
        for row in dbm.query("SELECT key FROM settings WHERE key LIKE ?", (B._MARK_PREFIX + "%",)):
            dbm.delete_setting(row["key"])

    def tearDown(self):
        for aid in self._apps:
            dbm.execute("DELETE FROM deployments WHERE app_id=?", (aid,))
            dbm.execute("DELETE FROM apps WHERE id=?", (aid,))
        for bid in self._bks:
            dbm.execute("DELETE FROM backups WHERE id=?", (bid,))
            dbm.delete_setting(B._mark_key(bid))
        for row in dbm.query("SELECT key FROM settings WHERE key LIKE ?", (B._MARK_PREFIX + "%",)):
            dbm.delete_setting(row["key"])

    def mkapp(self, **kw) -> int:
        aid = _mkapp(**kw)
        self._apps.append(aid)
        return aid

    def mkbk(self, kind: str, target, keep: int = 7) -> int:
        bid = _mkbk(kind, target, keep)
        self._bks.append(bid)
        return bid

    def artifact(self, bid: int, filename: str) -> str:
        """在本任务的备份目录下造一个可恢复的产物，返回 _backup_file_path 认的名字。"""
        name = f"bk{bid}-{filename}"
        Path(os.path.join(B.BACKUP_DIR, name)).write_bytes(b"")
        return name


# ----------------------------------------------------------------- 锁原语
class TestLockPrimitive(unittest.TestCase):
    def setUp(self):
        self.key = _uid("app")

    def reap(self, proc):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def hold_elsewhere(self, path: Path, secs: float = 30) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLD, str(path), str(secs)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.reap, proc)
        # 必须等子进程回执"我拿到锁了"再断言：抢在它 fcntl 之前测，测到的只是运气。
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_holder_telemetry_and_refusal_names_it(self):
        guard = locks.acquire(self.key, "部署", "alice")
        self.addCleanup(guard.release)
        holder = locks.holder_of(self.key)
        self.assertEqual((holder["action"], holder["actor"], holder["pid"]), ("部署", "alice", os.getpid()))
        with self.assertRaises(RuntimeError) as got:
            locks.acquire(self.key, "恢复", "bob")
        msg = str(got.exception)
        # 「请稍后再试」在这里不够用：运维要的是"这个应用凭什么不能动、谁在动、动了多久"。
        for frag in ("无法开始", "部署", "alice", "正在进行"):
            self.assertIn(frag, msg)

    def test_release_clears_the_holder(self):
        locks.acquire(self.key, "部署", "alice").release()
        self.assertIsNone(locks.holder_of(self.key), "留在文件里的持有者会让下一次读取还以为有人在跑")

    def test_release_frees_the_lock_for_the_next_action(self):
        with locks.hold(self.key, "同步"):
            with self.assertRaises(RuntimeError):
                locks.acquire(self.key, "回滚")
        locks.acquire(self.key, "回滚").release()

    def test_hold_releases_even_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with locks.hold(self.key, "同步"):
                raise RuntimeError("git fetch 失败")
        self.assertIsNone(locks.holder_of(self.key))
        locks.acquire(self.key, "回滚").release()

    def test_cross_process_exclusion(self):
        guard = locks.acquire(self.key, "部署", "web")
        self.addCleanup(guard.release)
        payload = _probe(self.key)
        self.assertFalse(payload.get("ok"), "另一个进程已持有目标锁，本进程不该拿得到")
        self.assertIn("部署", payload.get("msg", ""))

    def test_kernel_releases_when_the_holder_dies(self):
        """进程被 SIGKILL 之后锁必须自动没了——这是本批次选 flock 而不是数据库状态位的理由。"""
        proc = self.hold_elsewhere(locks.path_of(self.key))
        proc.kill()
        proc.wait(timeout=10)
        locks.acquire(self.key, "部署", "after-kill").release()

    def test_lock_file_is_owner_only(self):
        guard = locks.acquire(self.key, "部署", "alice")
        self.addCleanup(guard.release)
        # 锁文件里写着人名与动作，同机任何被攻破的业务进程都不该读得到
        self.assertEqual(stat.S_IMODE(os.stat(locks.path_of(self.key)).st_mode), 0o600)

    def test_keys_are_stable_and_names_are_sanitised(self):
        self.assertEqual(locks.key_app(5), "app-5")
        self.assertEqual(locks.key_app("5"), "app-5", "同一应用不能因为传 str/int 拿到两把锁")
        for bad in ("../../etc/passwd", "a/b\\c", "中文名", ""):
            key = locks.key_name(bad)
            self.assertTrue(key.startswith("name-"), key)
            # 名字键最终要变成一个文件名：分隔符必须被换掉，否则锁文件会写到 DATA_DIR 之外
            self.assertNotIn("/", key)
            self.assertNotIn("\\", key)
            self.assertEqual(os.path.basename(locks.path_of(key)), f"{key}.lock")
        self.assertLessEqual(len(locks.key_pg("x" * 500)), 83)

    def test_path_of_follows_live_data_dir(self):
        alt = tempfile.mkdtemp(prefix="cp_b33_data_")
        self.addCleanup(shutil.rmtree, alt, True)
        with mock.patch.object(config, "DATA_DIR", alt):
            self.assertEqual(locks.path_of("app-1"), Path(alt, "locks", "app-1.lock"))

    def test_no_holder_left_behind_when_the_dir_is_unwritable(self):
        """留痕写不进去不该让一次合法加锁失败，但抢不到时仍要说"有另一个操作在跑"。"""
        self.assertIn("无法开始", locks.refused(_uid("app"), "恢复"))


# ------------------------------------------- 一个目标一把锁：覆盖关系矩阵
class TestOneKeyPerTarget(_Db):
    def test_dir_and_sqlite_backups_of_one_app_share_a_key(self):
        name = _uid("vid")
        aid = self.mkapp(name=name)
        bk_dir = {"id": 1, "kind": "app", "target": name}
        bk_sql = {"id": 2, "kind": "sqlite", "target": str(aid)}
        self.assertEqual(B._restore_lock_key(bk_dir), locks.key_app(aid))
        self.assertEqual(B._restore_lock_key(bk_sql), locks.key_app(aid))

    def test_sqlite_key_survives_rename(self):
        """SQLite 的 target 存 app_id：应用改名后它仍解析到同一把锁（名字类才会散开）。"""
        aid = self.mkapp(name=_uid("vid"))
        before = B._restore_lock_key({"id": 1, "kind": "sqlite", "target": str(aid)})
        dbm.execute("UPDATE apps SET name=? WHERE id=?", (_uid("new"), aid))
        self.assertEqual(before, locks.key_app(aid))

    def test_missing_app_falls_back_to_name_key(self):
        """解析不出应用时退回名字键——这条分支必须在"应用已删除的历史备份"上真走到。"""
        gone = "999999"
        self.assertEqual(B._restore_lock_key({"id": 1, "kind": "sqlite", "target": gone}), locks.key_name(gone))
        self.assertEqual(B._restore_lock_key({"id": 1, "kind": "app", "target": "nope"}), locks.key_name("nope"))

    def test_lock_key_lookup_does_not_abort_the_restore(self):
        """挑键的那次查询不许替正文做校验：目录/应用存在性这类判词应由恢复本体给出。"""
        bid = self.mkbk("app", "nope")
        fn = self.artifact(bid, "x.tar.gz")
        with self.assertRaises(RuntimeError) as got:
            asyncio.run(B.restore_backup(bid, fn, "admin"))
        self.assertIn("备份目标应用已不存在", str(got.exception))

    def test_pg_uses_its_own_namespace(self):
        self.assertEqual(B._restore_lock_key({"id": 1, "kind": "pg", "target": "appdb"}), locks.key_pg("appdb"))
        self.assertNotEqual(
            B._restore_lock_key({"id": 1, "kind": "pg", "target": "appdb"}),
            B._restore_lock_key({"id": 1, "kind": "app", "target": "appdb"}),
        )

    def test_restore_refused_while_deploy_holds_the_app(self):
        name = _uid("vid")
        aid = self.mkapp(name=name)
        bid = self.mkbk("app", name)
        fn = self.artifact(bid, "x.tar.gz")
        guard = locks.acquire(locks.key_app(aid), "部署", "alice")
        self.addCleanup(guard.release)
        with mock.patch.object(B, "_restore_app", _never("抢不到锁就不许进正文")):
            with self.assertRaises(RuntimeError) as got:
                asyncio.run(B.restore_backup(bid, fn, "admin"))
        self.assertIn("部署", str(got.exception))
        # 抢不到锁就不许留下恢复标记：那条标记会让下一次重启说起一次从未开始的覆盖
        self.assertIsNone(dbm.get_setting(B._mark_key(bid)))

    def test_write_actions_refused_while_restore_holds_the_app(self):
        """恢复握着 app id 那把锁时，部署/同步/回滚/删除四个入口都得当场被挡住。"""
        aid = self.mkapp(name=_uid("vid"))
        guard = locks.acquire(locks.key_app(aid), "恢复 bk.tar.gz", "admin")
        self.addCleanup(guard.release)
        cases = {
            "deploy": lambda: apps_service.deploy_app(aid, "admin"),
            "sync": lambda: apps_service.sync_app(aid, "admin"),
            "rollback": lambda: apps_service.rollback_app(aid, "admin"),
            "delete": lambda: apps_service.delete_app(aid, False, "admin"),
        }
        for verb, call in cases.items():
            with self.subTest(verb=verb):
                with self.assertRaises(RuntimeError) as got:
                    asyncio.run(call())
                self.assertIn("恢复", str(got.exception))
        self.assertIsNotNone(dbm.query_one("SELECT id FROM apps WHERE id=?", (aid,)), "被拒绝的删除不许已经动过记录")
        self.assertIsNone(
            dbm.query_one("SELECT id FROM deployments WHERE app_id=? AND status='running'", (aid,)),
            "抢锁失败必须发生在 INSERT 之前，否则留下一条永远不清的 running 行",
        )

    def test_restore_refused_while_existing_deployment_running(self):
        """flock 之外的 running 行（手工插的、旧版本面板留下的）仍由第二道闸挡住。"""
        name = _uid("vid")
        aid = self.mkapp(name=name)
        bid = self.mkbk("app", name)
        fn = self.artifact(bid, "x.tar.gz")
        dep = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (aid,))
        self.addCleanup(dbm.execute, "DELETE FROM deployments WHERE id=?", (dep,))
        with self.assertRaises(RuntimeError) as got:
            asyncio.run(B.restore_backup(bid, fn, "admin"))
        self.assertIn("有部署正在进行", str(got.exception))

    def test_two_restores_of_one_app_via_different_backup_rows(self):
        """同一个应用的两条备份任务（目录类 + SQLite 类）并发恢复时，只有一份进正文。"""
        name = _uid("vid")
        aid = self.mkapp(name=name)
        bk_dir = self.mkbk("app", name)
        bk_sql = self.mkbk("sqlite", aid)
        fn_dir = self.artifact(bk_dir, "x.tar.gz")
        fn_sql = self.artifact(bk_sql, "y.sqlite.gz")
        live = {"now": 0, "peak": 0}

        async def slow(_bk, _src, log):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.03)
            live["now"] -= 1
            log.append("==> ok")

        async def go():
            return await asyncio.gather(
                B.restore_backup(bk_dir, fn_dir, "admin"),
                B.restore_backup(bk_sql, fn_sql, "admin"),
                return_exceptions=True,
            )

        with mock.patch.object(B, "_restore_app", slow), mock.patch.object(B, "_restore_sqlite", slow):
            res = asyncio.run(go())
        self.assertEqual(live["peak"], 1, "两条任务必须落在同一把锁上")
        self.assertEqual(sum(isinstance(r, dict) for r in res), 1, res)


# ------------------------------------------------------------ 恢复的持久标记
class TestRestoreMarker(_Db):
    def test_marker_lives_during_the_overwrite_and_gone_after(self):
        name = _uid("vid")
        self.mkapp(name=name)
        bid = self.mkbk("app", name)
        fn = self.artifact(bid, "x.tar.gz")
        seen = {}

        async def body(_bk, _src, log):
            raw = dbm.get_setting(B._mark_key(bid))
            seen["during"] = json.loads(raw) if raw else None
            log.append("==> 已解包")

        with mock.patch.object(B, "_restore_app", body):
            asyncio.run(B.restore_backup(bid, fn, "admin"))
        self.assertEqual(seen["during"]["kind"], "app")
        self.assertEqual(seen["during"]["actor"], "admin")
        self.assertIsNone(dbm.get_setting(B._mark_key(bid)), "恢复结束后标记必须清掉，否则下次重启误报")

    def test_marker_and_lock_cleared_when_the_body_fails(self):
        name = _uid("vid")
        aid = self.mkapp(name=name)
        bid = self.mkbk("app", name)
        fn = self.artifact(bid, "x.tar.gz")
        key = locks.key_app(aid)

        async def boom(_bk, _src, log):
            log.append("!! 半路失败")
            raise RuntimeError("tar 退出码 2")

        with mock.patch.object(B, "_restore_app", boom):
            with self.assertRaises(RuntimeError):
                asyncio.run(B.restore_backup(bid, fn, "admin"))
        self.assertIsNone(dbm.get_setting(B._mark_key(bid)))
        self.assertIsNone(locks.holder_of(key), "失败路径也要把锁放回去")

    def test_note_stopped_unit_marks_the_service_panel_stopped(self):
        bid = self.mkbk("app", _uid("vid"))
        B._mark_restore({"id": bid, "kind": "app", "target": "t", "_file": "/tmp/x.tar.gz"}, "admin")
        B._note_stopped_unit({"id": bid}, "panel-t.service")
        rec = json.loads(dbm.get_setting(B._mark_key(bid)))
        self.assertEqual(rec["unit"], "panel-t.service")
        self.assertTrue(rec["stopped_by_panel"], "启动对账要靠这一行判断能不能替它拉起来")

    def test_note_stopped_unit_does_not_resurrect_a_cleared_marker(self):
        bid = self.mkbk("app", _uid("vid"))
        B._note_stopped_unit({"id": bid}, "panel-t.service")
        self.assertIsNone(dbm.get_setting(B._mark_key(bid)))


# ---------------------------------------------------------------- 启动对账
class _ResumeBase(_Db):
    def write_marker(self, bid, **kw):
        rec = {"kind": "app", "target": "vid", "actor": "admin", "file": "bk.tar.gz"}
        rec.update(kw)
        dbm.set_setting(B._mark_key(bid), json.dumps(rec, ensure_ascii=False))

    def resume(self, state=None, start_code=0):
        calls = []

        async def run(_cmd, argv, **_kw):
            calls.append(list(argv))
            return {"code": start_code, "out": "Start request repeated too quickly" if start_code else "", "err": ""}

        async def reset_failed(unit):
            calls.append(["reset-failed", "--", unit])

        async def go():
            with (
                mock.patch.object(B.systemd_ops, "unit_state", state or _state("inactive")),
                mock.patch.object(B, "run", run),
                mock.patch.object(B.systemd_ops, "reset_failed", reset_failed),
            ):
                return await B.resume_interrupted_restores(), calls

        return asyncio.run(go())


class TestResumeInterrupted(_ResumeBase):
    def test_stopped_service_is_started_and_declared_half_written(self):
        bid = self.mkbk("app", _uid("vid"))
        self.write_marker(bid, unit="panel-vid.service", stopped_by_panel=True)
        lines, calls = self.resume(_state("inactive"))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("已重新拉起 panel-vid.service", lines[0])
        self.assertIn("恢复可能只写了一半", lines[0])
        self.assertIn(["start", "--", "panel-vid.service"], calls)
        self.assertIsNone(dbm.get_setting(B._mark_key(bid)), "同一条中断只该在启动时被说起一次")

    def test_failed_unit_is_reset_before_start(self):
        bid = self.mkbk("app", _uid("vid"))
        self.write_marker(bid, unit="panel-vid.service", stopped_by_panel=True)
        lines, calls = self.resume(_state("failed"))
        self.assertIn("已重新拉起", lines[0])
        self.assertIn("reset-failed", calls[0])

    def test_busy_unit_is_not_restarted(self):
        """activating/deactivating 时不能再下一道 start：那份现场正被人动着。"""
        bid = self.mkbk("app", _uid("vid"))
        self.write_marker(bid, unit="panel-vid.service", stopped_by_panel=True)
        lines, calls = self.resume(_state("activating", busy=True))
        self.assertIn("现在在跑", lines[0])
        self.assertEqual(calls, [])

    def test_missing_unit_is_reported_not_started(self):
        bid = self.mkbk("app", _uid("vid"))
        self.write_marker(bid, unit="panel-gone.service", stopped_by_panel=True)
        lines, calls = self.resume(_state("inactive", missing=True))
        self.assertIn("本机没有", lines[0])
        self.assertEqual(calls, [])

    def test_unknown_state_does_not_act(self):
        bid = self.mkbk("app", _uid("vid"))
        self.write_marker(bid, unit="panel-vid.service", stopped_by_panel=True)
        lines, calls = self.resume(_state("unknown"))
        self.assertIn("未擅自启动", lines[0])
        self.assertEqual(calls, [])

    def test_start_failure_is_not_painted_over(self):
        bid = self.mkbk("app", _uid("vid"))
        self.write_marker(bid, unit="panel-vid.service", stopped_by_panel=True)
        lines, _calls = self.resume(_state("inactive"), start_code=1)
        self.assertIn("启动失败", lines[0])
        self.assertIn("systemctl start panel-vid.service", lines[0])

    def test_service_not_stopped_by_panel_is_left_alone(self):
        """恢复没走到停服务那步（标记里没有 unit）时，不许启停别人的服务。"""
        bid = self.mkbk("app", _uid("vid"))
        self.write_marker(bid)
        lines, calls = self.resume()
        self.assertIn("不是面板停的", lines[0])
        self.assertEqual(calls, [])

    def test_broken_marker_json_is_still_cleared(self):
        bid = self.mkbk("app", _uid("vid"))
        dbm.set_setting(B._mark_key(bid), "{not json")
        lines, calls = self.resume()
        self.assertIn("不是面板停的", lines[0])
        self.assertIsNone(dbm.get_setting(B._mark_key(bid)))
        self.assertEqual(calls, [])

    def test_clean_boot_says_nothing(self):
        lines, calls = self.resume()
        self.assertEqual((lines, calls), ([], []))

    def test_lifespan_hooks_reconciliation_before_timer_sync(self):
        src = Path(BACKEND, "app", "main.py").read_text(encoding="utf-8")
        self.assertIn("resume_interrupted_restores", src)
        # 对账排在定时器同步之前：先把上一次没跑完的现场说明白，再谈这一轮要不要跑
        self.assertLess(src.index("resume_interrupted_restores"), src.index("sync_all_timers"))


# ------------------------------------------------------------ 产物命名与保留
class TestArtifactNaming(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b33_bk_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.stamp = "2026-01-01-00-00-00"

    def test_reservation_is_atomic_not_a_check(self):
        """占名必须是 O_CREAT|O_EXCL：先 exists 判断再返回名字，挡不住两个进程同时通过。"""
        first = R._free_path(7, self.stamp, ".tar.gz", self.dir)
        second = R._free_path(7, self.stamp, ".tar.gz", self.dir)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first), "返回的名字必须已经占住，否则两个 runner 会写进同一个文件")
        self.assertEqual(os.path.basename(second), f"bk7-{self.stamp}-2.tar.gz")
        self.assertIn("os.O_EXCL", inspect.getsource(R._free_path))
        # 文档里正好写着"不能 os.path.exists() 判断完再返回"，所以只看 docstring 之后的代码
        self.assertNotIn("os.path.exists", inspect.getsource(R._free_path).split('"""', 2)[-1])

    def test_reservation_retry_is_bounded(self):
        for i in range(100):
            name = f"bk7-{self.stamp}.tar.gz" if i == 0 else f"bk7-{self.stamp}-{i + 1}.tar.gz"
            Path(os.path.join(self.dir, name)).write_bytes(b"x")
        with self.assertRaises(RuntimeError):
            R._free_path(7, self.stamp, ".tar.gz", self.dir)

    def test_reservation_rejects_when_directory_is_gone(self):
        with self.assertRaises(RuntimeError):
            R._free_path(7, self.stamp, ".tar.gz", os.path.join(self.dir, "not-here"))

    def test_different_suffixes_do_not_steal_names(self):
        a = R._free_path(7, self.stamp, ".dump.gz", self.dir)
        b = R._free_path(7, self.stamp, ".tar.gz", self.dir)
        self.assertEqual(
            (os.path.basename(a), os.path.basename(b)),
            (f"bk7-{self.stamp}.dump.gz", f"bk7-{self.stamp}.tar.gz"),
        )

    def test_pre_restore_env_is_a_quota_holder(self):
        """崩在恢复中途留下的 -pre-restore.env 是一份凭据副本，它必须进轮换。"""
        names = [
            f"bk7-{self.stamp}-pre-restore.env",  # 最旧：一次被杀的恢复
            f"bk7-{self.stamp}-2.tar.gz",
            f"bk7-{self.stamp}-3.sqlite.gz",
            "bk7-2026-01-02-00-00-00.tar.gz",
            "bk7-manual-upload.tar.gz",  # 人工上传，由用户在界面上自己删
            "bk7-2026-01-03-00-00-00.sqlite.gz.tmp",  # 半成品，不该占名额
        ]
        for n in names:
            Path(os.path.join(self.dir, n)).write_bytes(b"x")
        with mock.patch.object(R, "BACKUP_DIR", self.dir):
            order = R.auto_files(7)
        self.assertEqual(order, names[:4], order)
        with mock.patch.object(R, "BACKUP_DIR", self.dir):
            R.prune({"id": 7, "keep": 2})
        left = set(os.listdir(self.dir))
        self.assertNotIn(names[0], left, "被杀恢复留下的 .env 必须随 keep 名额轮走")
        self.assertNotIn(names[1], left)
        self.assertTrue(names[2] in left and names[3] in left, left)
        self.assertIn("bk7-manual-upload.tar.gz", left)
        self.assertIn("bk7-2026-01-03-00-00-00.sqlite.gz.tmp", left)


# ------------------------------------------------------------ 备份进程入口
class TestBackupRunnerEntry(_Db):
    def setUp(self):
        super().setUp()
        self.bid = self.mkbk("app", _uid("t"))

    def _run_job(self, main):
        out = io.StringIO()
        with mock.patch.object(R, "main", main), contextlib.redirect_stdout(out):
            return R.run_job(self.bid), out.getvalue()

    def test_entry_takes_the_backup_lock_and_frees_it(self):
        seen = {}

        def main(_row):
            seen["holder"] = locks.holder_of(locks.key_backup(self.bid))

        code, out = self._run_job(main)
        self.assertEqual(code, 0)
        self.assertIn("backup ok", out)
        self.assertEqual(seen["holder"]["action"], f"备份任务 {self.bid}")
        self.assertIsNone(locks.holder_of(locks.key_backup(self.bid)))

    def test_overlap_skips_with_zero(self):
        """抢不到锁是"这一轮没必要再跑一遍"，不是失败：报失败会让 timer 的 journal 长期挂假故障。"""
        guard = locks.acquire(locks.key_backup(self.bid), "立即备份", "admin")
        self.addCleanup(guard.release)
        code, out = self._run_job(lambda _row: self.fail("抢到锁了才该跑正文"))
        self.assertEqual(code, 0)
        self.assertIn("backup skipped", out)
        self.assertIn("立即备份", out)

    def test_real_failure_still_exits_one(self):
        def boom(_row):
            raise RuntimeError("pg_dump 退出码 2")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out = self._run_job(boom)
        self.assertEqual(code, 1)
        self.assertIn("backup failed", err.getvalue())
        self.assertNotIn("backup ok", out)
        self.assertIsNone(locks.holder_of(locks.key_backup(self.bid)), "失败退出也要放锁")

    def test_missing_task_exits_two(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(R.run_job(9999999), 2)
        self.assertIn("backup not found", err.getvalue())

    def test_timer_and_manual_both_enter_run_job(self):
        """timer 的 unit 与"立即备份"都必须落进同一个带锁的进程入口，别在别处另开一份没有锁的跑法。"""
        self.assertIn("sys.exit(run_job(_bid))", inspect.getsource(R))
        self.assertIn("app.backup_runner", inspect.getsource(B.run_backup_now))
        b_src = Path(B.__file__).read_text(encoding="utf-8")
        self.assertIn("-m app.backup_runner", b_src, "定时备份的 ExecStart 不再指向 runner 入口")


# ------------------------------------------------------ 定时任务那一侧的重叠闸
class TestCronExecStart(unittest.TestCase):
    def setUp(self):
        self.tid = 4242

    def test_flock_prefix_when_available(self):
        flker = shutil.which("flock")
        echo = shutil.which("echo")
        line = C._exec_start(["echo", "hi"], self.tid)
        if not flker:
            self.assertEqual(line, f"ExecStart={echo} hi")  # 没有 flock 就退回裸 argv
            return
        self.assertTrue(line.startswith(f'ExecStart="{flker}" -n -E 0 '), line)
        self.assertIn(str(C._lock_path(self.tid)), line)
        self.assertTrue(line.endswith(f"-- {echo} hi") or line.endswith(f'-- "{echo}" hi'), line)

    def test_prefix_lock_path_is_the_one_run_now_flocks(self):
        """手动那一侧与定时那一侧必须锁同一个文件，否则这两个入口还是互相看不见。"""
        self.assertIn(str(C._lock_path(self.tid)), C._exec_start(["echo", "hi"], self.tid))
        self.assertIn("_lock_path", inspect.getsource(C.run_now))

    def test_no_flock_binary_falls_back_to_bare_argv(self):
        real = shutil.which

        def which(name):
            return None if name == "flock" else real(name)

        with mock.patch.object(C.shutil, "which", which):
            line = C._exec_start(["echo", "hi"], self.tid)
        self.assertEqual(line, f"ExecStart={real('echo')} hi", "宁可少一把锁，也不要 ExecStart 指向不存在的二进制")

    def test_unusable_lock_directory_falls_back(self):
        blocker = tempfile.NamedTemporaryFile(prefix="cp_b33_notadir_", delete=False)
        self.addCleanup(os.unlink, blocker.name)
        with mock.patch.object(C, "_lock_path", lambda tid: Path(blocker.name, "x.lock")):
            line = C._exec_start(["echo", "hi"], self.tid)
        self.assertNotIn("flock", line)

    def test_conflict_exit_is_zero_by_design(self):
        """`-E 0` 是本批最需要现场证据的一处：oneshot 非 0 退出会挂在 failed-units 这个 crit
        告警上，而命令自己的失败码不能被它吃掉。跑真 flock，两头都验。"""
        flker = shutil.which("flock")
        if not flker:
            self.skipTest("本机没有 flock")
        key = _uid("app")
        guard = locks.acquire(key, "立即备份", "admin")
        try:
            held = subprocess.run([flker, "-n", "-E", "0", str(locks.path_of(key)), "/bin/true"], capture_output=True)
            self.assertEqual(held.returncode, 0, "抢不到锁按成功退出，不挂 crit")
        finally:
            guard.release()
        own = subprocess.run(
            [flker, "-n", "-E", "0", str(locks.path_of(key)), "/bin/sh", "-c", "exit 5"], capture_output=True
        )
        self.assertEqual(own.returncode, 5, "命令自身失败必须原样传出，不能被 -E 0 粉饰成绿灯")

    def test_dollar_in_argument_is_escaped(self):
        self.assertIn("$$HOME", C._exec_start(["echo", "$HOME"], self.tid))


# ------------------------------------------------- 存量任务的 unit 在启动时被对账
class TestCronUnitResync(unittest.TestCase):
    """`sync_all_units`：升级面板不会重渲存量任务的 ExecStart，这一趟补上。

    unit 只在 create/update 落地，所以 flock 包装对装机时就存在的任务是"代码里写了、
    盘上没有"——正是批次㉝ 自己那处守卫的交付缺口。这里连启动钩子一起钉住。
    """

    def setUp(self):
        self.units = tempfile.mkdtemp(prefix="cp_b33_units_")
        self.addCleanup(shutil.rmtree, self.units, True)
        self.calls: list[list[str]] = []
        self.warns: list[str] = []
        p1 = mock.patch.object(C, "_unit_dir", lambda: self.units)
        p2 = mock.patch.object(C, "run", self._fake_run)
        p3 = mock.patch.object(C, "warn", lambda *parts: self.warns.append(" ".join(str(x) for x in parts)))
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)
        self._tasks: list[int] = []

    async def _fake_run(self, _cmd, argv=None, **_kw):
        self.calls.append(list(argv or []))
        return {"code": 0, "out": "", "err": ""}

    def task(self, *, enabled: int = 1, argv=None) -> int:
        """默认 argv 用 choyeonctl：这台机器上它一定有出处（`_resolve_binary` 认
        /usr/local/bin/choyeonctl，也认仓库内入口 `<仓库>/bin/choyeonctl`），所以任何跑
        测试的机器都给同一个答案。原先写的是 certbot——CI runner 上没装，`_resolve_binary`
        抛 BadInput，`sync_all_units` 如实报"这一行没落盘"，两条用例就一起变成
        0 not greater than or equal to 1（批次㊿D）。缺二进制这件事本身有下面的用例钉。"""
        tid = dbm.execute(
            "INSERT INTO cron_task(name,expr,argv,enabled) VALUES(?,?,?,?)",
            (_uid("t"), "0 3 * * *", json.dumps(argv or ["choyeonctl", "backup", "run"]), enabled),
        )
        self.addCleanup(dbm.execute, "DELETE FROM cron_task WHERE id=?", (tid,))
        self._tasks.append(tid)
        return tid

    def svc(self, tid: int) -> Path:
        return Path(self.units, f"panel-cron-{tid}.service")

    def test_enabled_row_gets_the_flock_prefix(self):
        if not shutil.which("flock"):
            self.skipTest("本机没有 flock，_exec_start 本来就退回裸 argv")
        tid = self.task()
        self.assertGreaterEqual(asyncio.run(C.sync_all_units()), 1)
        body = self.svc(tid).read_text(encoding="utf-8")
        line = next(ln for ln in body.splitlines() if ln.startswith("ExecStart="))
        self.assertIn("flock", line, "升级后存量任务必须拿到同一把 flock，否则这个方向的重叠照旧没人挡")
        self.assertIn(str(C._lock_path(tid)), line)
        self.assertIn(["daemon-reload"], self.calls)
        self.assertIn(["enable", "--now", f"panel-cron-{tid}.timer"], self.calls)

    def test_disabled_row_is_left_off_the_system(self):
        """禁用任务的 unit 本就不在盘上：对它跑一遍 disable/stop/reset-failed 是凭空发命令，
        还会把管理员在面板外手工启用的 timer 按库里的禁用状态压回去。"""
        tid = self.task(enabled=0)
        asyncio.run(C.sync_all_units())
        self.assertFalse(self.svc(tid).exists())
        self.assertEqual([c for c in self.calls if f"panel-cron-{tid}" in " ".join(c)], [])

    def test_one_bad_row_does_not_stop_the_others(self):
        bad = self.task(argv=["not-in-whitelist", "x"])
        good = self.task()
        self.assertGreaterEqual(asyncio.run(C.sync_all_units()), 1)
        self.assertTrue(self.svc(good).exists(), "一个坏任务不该把其余任务的对账一起停掉")
        self.assertFalse(self.svc(bad).exists())
        self.assertIn(str(bad), " ".join(self.warns))

    def test_whitelisted_but_absent_binary_is_warned_not_counted(self):
        """白名单里、本机却没装的命令：跳过它、点名它，且不记成"落过盘"。

        ㊿D 在 CI 上撞到的就是这个形状（runner 没有 certbot）。当时它只是一条红——现在它
        是行为契约：一台机器卸了 certbot，不能让启动对账整批停摆，也不能把没渲染成的 unit
        算进"已同步"的数目里。
        """
        if shutil.which("certbot"):
            self.skipTest("本机装了 certbot，模拟不出「白名单里但本机没有」")
        tid = self.task(argv=["certbot", "renew"])
        row = dbm.query("SELECT * FROM cron_task WHERE id=?", (tid,))[0]
        with mock.patch.object(C.dbm, "query", lambda *a, **k: [row]):
            self.assertEqual(asyncio.run(C.sync_all_units()), 0)
        self.assertFalse(self.svc(tid).exists(), "渲染不出来就不许留下半个 unit")
        self.assertIn(str(tid), " ".join(self.warns), "跳过必须留下能查的判词，不能静默吞掉")

    def test_lifespan_resyncs_cron_units_after_backup_timers(self):
        src = Path(BACKEND, "app", "main.py").read_text(encoding="utf-8")
        self.assertIn("sync_all_units", src, "服务层有对账函数而启动不调用它，等于升级后仍然没落地")
        self.assertLess(src.index("sync_all_timers"), src.index("sync_all_units"))


# ------------------------------------------------------------ 部署收尾放锁
class TestDeploymentGuardRelease(_Db):
    def test_body_failure_releases_the_guard(self):
        aid = self.mkapp()
        guard = locks.acquire(locks.key_app(aid), "部署", "admin")

        async def boom(_app, _dep):
            raise RuntimeError("git clone 失败")

        with mock.patch.object(apps_service, "_deployment_body", boom):
            with self.assertRaises(RuntimeError):
                asyncio.run(apps_service._run_deployment({"id": aid}, 1, guard))
        self.assertIsNone(locks.holder_of(locks.key_app(aid)))

    def test_cancellation_releases_the_guard(self):
        """部署被面板优雅退出取消时同样要放锁：否则这个应用被一把没有主人的锁钉住到重启。"""
        aid = self.mkapp()
        started = asyncio.Event()

        async def hang(_app, _dep):
            started.set()
            await asyncio.sleep(30)

        async def go():
            guard = locks.acquire(locks.key_app(aid), "部署", "admin")
            task = asyncio.create_task(apps_service._run_deployment({"id": aid}, 1, guard))
            await started.wait()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return locks.holder_of(locks.key_app(aid))

        with mock.patch.object(apps_service, "_deployment_body", hang):
            self.assertIsNone(asyncio.run(go()))

    def test_deploy_that_never_starts_leaves_no_running_row(self):
        """create_task 起不来时必须就地收尾：一条假 running 会把删除/回滚/改配置挡满最坏耗时上限。"""
        aid = self.mkapp()

        def boom(coro):
            coro.close()
            raise RuntimeError("事件循环正在关闭")

        with mock.patch.object(apps_service.asyncio, "create_task", boom):
            with self.assertRaises(RuntimeError):
                asyncio.run(apps_service.deploy_app(aid, "admin"))
        self.assertIsNone(locks.holder_of(locks.key_app(aid)))
        row = dbm.query_one("SELECT id,status FROM deployments WHERE app_id=?", (aid,))
        self.assertEqual(row["status"] if row else None, "failed", row)
        self.assertNotIn(row["id"], apps_service._RUNNING_DEPS)


def _never(why: str):
    async def body(_bk, _src, _log):
        raise AssertionError(why)

    return body


if __name__ == "__main__":
    unittest.main()
