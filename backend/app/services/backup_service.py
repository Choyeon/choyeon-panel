import gzip
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from contextlib import suppress
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from .. import backup_runner, config, locks
from .. import database as dbm
from ..log import warn
from ..util import (
    human_bytes,
    is_name,
    redact_creds,
    run,
    sh_escape,
    unit_env_value,
    unit_pct_escape,
    write_secret_atomic,
)
from . import systemd_ops
from .files_service import assert_write_budget, iso_ms, write_budget
from .pg_service import is_ident, psql

BACKUP_DIR = config.BACKUP_DIR
BACKEND_DIR = str(Path(config.BASE, "backend"))
_VENV_PY = str(Path(BACKEND_DIR, ".venv", "bin", "python"))
PYTHON_BIN = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable

# 与 backups.kind 的 CHECK 约束保持一致（database.py 建表 + 迁移都按这份）
KINDS = ("pg", "app", "sqlite")

# 备份产物的后缀（唯一出处）：backup_runner 真正写出的三类，加上早年直接扔进目录的裸文件。
# `choyeonctl backup list` 与自检项「备份」都读这一个常量：各写一份字面量时，CLI 只列 tar 包，
# 定时跑出来的 PG 备份在终端里像不存在一样。
ARTIFACT_SUFFIXES = (".tar.gz", ".sql.gz", ".dump.gz", ".sql", ".dump", ".gz")

# 手工上传产物时各类型接受的扩展名，必须与 _restoreable 一一对齐：
# 传了恢复不了的文件只会躺在备份目录里占名额，前端只能把按钮置灰。
_UPLOAD_SUFFIXES = {
    "pg": (".dump.gz",),
    "sqlite": (".sqlite.gz",),
    "app": (".tar.gz", ".tgz"),
}


def _unit_base(bid: int) -> str:
    return f"panel-backup-{bid}"


def _unit_dir() -> str:
    """单元目录取 config.UNIT_DIR（CP_UNIT_DIR 可改），和 apps_service 用同一个开关。

    之前这里写死 /etc/systemd/system：改了 CP_UNIT_DIR 的人，应用 unit 跟着走、
    备份 timer 还留在原地，两套位置谁也说不清哪个是权威。运行时读，方便测试重定向。
    """
    return config.UNIT_DIR


def _target_missing(b: dict, app_ids: set, app_names: set) -> bool:
    """app / sqlite 类备份的目标必须还在 apps 表里。

    应用被删除后备份行不会跟着走，timer 每晚跑一次 backup_runner 就 SystemExit 一次，
    而面板上这条任务依旧显示"已启用"——只有翻 journalctl 才发现备份早就不产出了。
    """
    if b["kind"] not in ("app", "sqlite"):
        return False
    t = str(b["target"] or "")
    if b["kind"] == "sqlite" and t.isdigit():
        return int(t) not in app_ids
    return t not in app_names


def list_backups() -> list:
    rows = dbm.query("SELECT * FROM backups ORDER BY id")
    # 目标存在性一次取回集合，取代每行 1-2 条 SELECT（备份任务多时列表就是 N 次查询）
    apps = dbm.query("SELECT id, name FROM apps")
    app_ids = {r["id"] for r in apps}
    app_names = {r["name"] for r in apps}
    # 异地目标名同理一次取回。为什么要给名字：任务行上的 `cloud_target_id` 在界面上
    # 只是一个数字，运维看到"3"并不知道那是哪个桶、能不能删；而列表页又要承担
    # "这条任务到底有没有异地副本"这个问题，只能把名字一起端出来。
    cloud = {r["id"]: r["name"] for r in dbm.query("SELECT id, name FROM cloud_targets")}
    listing = []
    if os.path.isdir(BACKUP_DIR):
        try:
            listing = sorted(os.listdir(BACKUP_DIR))
        except OSError:
            listing = []
    out = []
    for b in rows:
        b["dir"] = BACKUP_DIR
        b["target_missing"] = _target_missing(b, app_ids, app_names)
        tid = b.get("cloud_target_id")
        b["cloud_target_name"] = cloud.get(tid) if tid else None
        prefix = f"bk{b['id']}-"
        # 列表只端最近 30 个，免得任务一多整页都是文件清单；但份数与合计回答的是
        # 「这条任务现在攒了多少」，必须按全部产物算——keep 设到 100 的任务被显示成
        # 30 份、合计也少一大截，正是批次87 要修的那种把截断当总量。
        entries = []
        for f in reversed([f for f in listing if f.startswith(prefix)]):
            try:
                st = os.stat(f"{BACKUP_DIR}/{f}")
                entries.append({
                    "name": f,
                    "size": st.st_size,
                    "mtime": iso_ms(st.st_mtime),
                    # 面板数据目录的备份（bk0-/panel-）与 .sql.gz 走不了自动恢复，
                    # 前端据此把"恢复"按钮置灰，而不是让人点了才知道不行。
                    "restoreable": _restoreable(b["kind"], f),
                })
            except OSError:
                continue
        b["files_total"] = len(entries)
        b["files_bytes"] = sum(e["size"] for e in entries)
        b["files"] = entries[:30]
        b["files_omitted"] = len(entries) - len(b["files"])
        out.append(b)
    return out


_BELONGS_RE = re.compile(r"^bk(\d+)-")


def name_owner(name: str) -> int | None:
    """这个名字属于哪条备份任务（`bk<id>-` 前缀），认不出来回 None。

    本机列表、远端列表与拉回校验都要问同一句「这份产物是谁的」：各写一份判据的话，
    桶里那份 `bk1-manual-dump.sql` 会在本机列得出来、在云端列表里被当别人的对象藏起来，
    而恢复只能读本机的文件——看得见却拉不回，等于没有异地。
    带目录的键（`bk1-a/b.gz`）在这里就判 None：远端的键要先剥掉前缀、只留文件名才走到这一步。
    """
    n = str(name or "")
    if n != os.path.basename(n):
        return None
    m = _BELONGS_RE.match(n)
    return int(m.group(1)) if m else None


def list_files() -> dict:
    """所有任务的产物平铺成一页（备份文件管理页用），按时间倒序。

    任务列表里的 files 是"每条任务前 30 个"，看总量、按时间清理都不方便；
    这里反过来：以文件为主体，任务只是它的一个属性。
    任务行已被删除但文件还在的（job_missing）也列出来——那些正是没人管、
    只能靠手动删才腾得出来的空间。
    """
    files: list[dict] = []
    if not os.path.isdir(BACKUP_DIR):
        return {"dir": BACKUP_DIR, "files": files}
    jobs = {b["id"]: b for b in dbm.query("SELECT * FROM backups")}
    for name in os.listdir(BACKUP_DIR):
        bid = name_owner(name)
        if bid is None:
            continue
        try:
            st = os.stat(f"{BACKUP_DIR}/{name}")
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        bk = jobs.get(bid)
        kind = bk["kind"] if bk else ""
        files.append({
            "name": name,
            "job_id": bid,
            "job_missing": bk is None,
            "kind": kind,
            "target": (_sqlite_target_label(bk["target"]) if kind == "sqlite" else bk["target"]) if bk else "",
            "size": st.st_size,
            "mtime": iso_ms(st.st_mtime),
            "restoreable": _restoreable(kind, name) if bk else False,
        })
    files.sort(key=lambda f: (f["mtime"], f["name"]), reverse=True)
    return {"dir": BACKUP_DIR, "files": files}


def _sqlite_target_label(target) -> str:
    """sqlite 的 target 存的是 app_id（也可能是历史遗留的应用名），统一显示成应用名。"""
    t = str(target or "")
    row = dbm.query_one("SELECT name FROM apps WHERE id=?", (int(t),)) if t.isdigit() else None
    if not row:
        row = dbm.query_one("SELECT name FROM apps WHERE name=?", (t,))
    return row["name"] if row else f"#{t}"


# 任务在判词里的类型名（唯一出处）：告警正文、自检点名单和 CLI 都用这一个表，
# 同一个 #3 在页面叫「SQLite 备份 rosetta」、在邮件里叫「sqlite:3」就永远对不上号。
_KIND_LABEL = {"pg": "PG 备份", "app": "应用备份", "sqlite": "SQLite 备份"}


def run_label(b: dict) -> str:
    """把一条备份任务读成一句人话：类型 + 目标（sqlite 的 id 换成应用名）。"""
    kind = str(b.get("kind") or "")
    target = b.get("target")
    if kind == "sqlite":
        target = _sqlite_target_label(target)
    return f"{_KIND_LABEL.get(kind, kind or '?')} {target}"


def scheduled_runs() -> list[dict]:
    """所有「该由定时器跑」的备份任务及其执行留痕（enabled 且非 manual），带 label。

    巡检、自检与 CLI 都从这一个入口取：过滤条件（停用的不算、手动任务不算「该跑没跑」）
    写进 SQL 一处，两个读者才不会一个说「有 3 条失败」、另一个只点名 2 条。
    """
    rows = dbm.query(
        "SELECT id, kind, target, schedule, enabled, last_run, last_status, last_note "
        "FROM backups WHERE enabled=1 AND schedule<>'manual' ORDER BY id"
    )
    for r in rows:
        r["label"] = run_label(r)
    return rows


def is_failed_run(b: dict) -> bool:
    """这一条留痕是不是「最近一次没成」。

    空串＝从没跑过：刚建的任务报故障是冤枉它，界面该显示「未执行」而不是红色；
    `ok`＝最近一次成功；其余（`fail`、`exit N`）才是真失败。
    """
    status = str(b.get("last_status") or "")
    return bool(status) and status != "ok"


def failing_tasks() -> list[dict]:
    """启用的定时备份任务里最近一次没成的那些——自检点名单与 CLI 用。"""
    return [b for b in scheduled_runs() if is_failed_run(b)]


def _write_if_changed(path: str, content: str):
    if os.path.exists(path) and Path(path).read_text() == content:
        return
    # 原子写：这两个文件是定时器 unit，就地截断重写被打断留下的半截 unit，会被之后
    # 任何一次 daemon-reload 读进去——报出来的语法错误和这次改动看着毫无关系。
    write_secret_atomic(path, content)


async def _sync_timer(b: dict):
    u = _unit_base(b["id"])
    unit_dir = _unit_dir()
    svc = f"{unit_dir}/{u}.service"
    tmr = f"{unit_dir}/{u}.timer"
    if b["schedule"] == "manual" or not b["enabled"]:
        await run("systemctl", ["disable", "--now", f"{u}.timer"], timeout=30)
        for f in (svc, tmr):
            if os.path.exists(f):
                os.unlink(f)
        await run("systemctl", ["daemon-reload"])
        return
    # 时区名和 cron 任务定时器共用一个出处（systemd_ops.timezone_name）：两处各推一遍，
    # 同一台机器上的两类定时器早晚会一个按本地时间、一个按 UTC 跑。
    tz = systemd_ops.timezone_name()
    cal = f"{'Mon ' if b['schedule'] == 'weekly' else ''}{int(b['hour']):02d}:{int(b['minute']):02d} {tz}"
    data = config.DATA_DIR
    # 生成 unit 时的三处转义，都是沙箱真 systemd 实测出来的问题（CP_* 路径来自 .env / 环境变量，
    # 后端不做字符白名单，装了带空格或 % 的目录就会静默中招）：
    # - WorkingDirectory 不能加引号（加了直接报 "path is not absolute" 起不来），
    #   不加引号的空格反而没事；但 `%X` 会被展开成说明符，目录就不存在了 → unit_pct_escape。
    # - ExecStart 的二进制含空格、不加引号 → 203 起不来；引号包住即可，参数照常分词。
    # - Environment= 不加引号时值在第一个空格处截断（/tmp/sp dir/sub → /tmp/sp，
    #   备份写到别的目录且毫无提示），整条赋值用双引号包住；引号挡不住 `%` 展开
    #   （实测 /a%b 变成 /a<启动ID>），所以值还要 unit_env_value（转义 " 与 \、% 翻倍）。
    _wd = unit_pct_escape(BACKEND_DIR)
    _py = unit_pct_escape(PYTHON_BIN)
    _data = unit_env_value(data)
    _write_if_changed(
        svc,
        f"[Unit]\nDescription=choyeon-panel backup #{b['id']}\n[Service]\nType=oneshot\n"
        f"WorkingDirectory={_wd}\n"
        f'ExecStart="{_py}" -m app.backup_runner {b["id"]}\n'
        f'Environment="CP_DATA_DIR={_data}"\n',
    )
    _write_if_changed(
        tmr,
        f"[Unit]\nDescription=choyeon-panel backup timer #{b['id']}\n[Timer]\n"
        f"OnCalendar={cal}\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
    )
    await run("systemctl", ["daemon-reload"])
    r = await run("systemctl", ["enable", "--now", f"{u}.timer"], timeout=30)
    if b["enabled"] and r["code"] != 0:
        raise RuntimeError(r["out"])


def _validate_target(kind: str, target, *, app_must_exist: bool = True):
    """kind/target 的配对校验，create 与 update 必须共用同一份。

    旧写法只在 create_backup 里校验，update_backup 只查了 schedule/hour/minute/keep，
    于是 `PATCH {"target": "app;DROP DATABASE x;--"}` 能一路写进库。
    这个 target 后面会原样拼进 `su - postgres -c 'pg_dump -Fc <target>'`
    （backup_runner 里另有一道校验挡住了执行侧），但真正的问题是：
    坏值进了库，timer 每次跑都在 runner 里 SystemExit，面板上却显示"任务已配置"。
    kind 同样能在 PATCH 里改成表 CHECK 之外的值——那时抛的是 sqlite3.IntegrityError，
    面板路由的宽 except 会把它兜成 400，可是任何直接调服务层的地方（sync_all_timers、
    以后的 CLI）拿到的是一个未捕获的数据库异常，而不是业务错误。

    `app_must_exist` 是"目标应用这一行还在不在"的开关，只有把 target **换成了别的值**
    时才该查（create 总是，update 见调用处）。线上探针实测：
    `kind='app'` 过去连格式都过、名字不存在也照样建（返回 200，跑一次备份才
    「应用 x 不存在」退出码 1），写错一个字母就变成一条每晚失败的任务。
    但反过来，把存在性校验无条件套在 update 上会拆掉 ⑤L 的 `target_missing`：
    应用删掉之后那条任务连"改备份时间"和"停用"都做不了（sqlite 类就是这样，
    `merged target` 永远是非空的历史 id），只剩删除一条路，而用户想留产物、
    先把计划关掉。所以：名字写错当场就挡，指向已删应用的老行仍然可以改配置。
    pg 库名只查格式不查存在性——面板自己能建库，"先配好备份计划再建库"是
    一条合理路径，而库不存在时 pg_dump 的失败在备份日志里看得见。
    """
    if kind == "pg" and target != "all" and not is_ident(target or ""):
        raise RuntimeError("PG 库名不合法")
    if kind == "app":
        t = str(target or "")
        if not is_name(t):
            raise RuntimeError("应用名不合法")
        if app_must_exist and not dbm.query_one("SELECT 1 FROM apps WHERE name=?", (t,)):
            raise RuntimeError(f"应用 {t} 不存在")
    if kind == "sqlite":
        # sqlite 的 target 存 app_id（应用改名后 name 型 target 会指向不存在的目录），
        # 同时兼容按应用名配置的历史写法。
        t = str(target or "")
        if t.isdigit():
            if app_must_exist and not dbm.query_one("SELECT 1 FROM apps WHERE id=?", (int(t),)):
                raise RuntimeError("应用不存在")
        elif not is_name(t) or (app_must_exist and not dbm.query_one("SELECT 1 FROM apps WHERE name=?", (t,))):
            raise RuntimeError("SQLite 备份目标应用不存在")


def rename_app(old_name: str, new_name: str) -> None:
    """应用改名后，把按应用名存目标的备份任务一起改名。

    `kind='app'` 的 target 存的就是应用名——`backup_runner` 按
    `SELECT path FROM apps WHERE name=?` 找目录，`_restore_app` 也一样。应用改名时
    这一行不会跟着动，于是它指向一个已经不存在的名字：timer 每晚跑一次 runner 就
    SystemExit 一次（"应用 x 不存在"），面板上这条任务还是"已启用"，只有翻
    journalctl 才发现备份早就不产出了。⑤L 加的 `target_missing` 只是把坏状态**显示**
    出来，改名这件事本身应该让任务继续跟着应用走。

    `kind='sqlite'` 现在按 app_id 存（改名不影响它），但 `_validate_target` 明写了
    "兼容历史的名型写法"，库里真有这种行，所以名型的那一份也一起带走。
    匹配用精确等值而不是 LIKE：应用名允许连字符，模糊匹配会误伤别的任务。
    """
    dbm.execute(
        "UPDATE backups SET target=? WHERE kind IN ('app','sqlite') AND target=?",
        (new_name, old_name),
    )


_UNSET = object()


def _cloud_target_id(raw, keep=None) -> int | None:
    """把请求里的 `cloud_target_id` 折算成要写进库的值。

    三种写法各有意义：缺省＝不改动（PATCH），`null`/`0`＝解除关联（本机留档、不再推异地），
    正整数＝关联那个目标。为什么必须查目标存在：链接到一个不存在的目标，
    runner 每次备份都会为它跑一次注定失败的推送，而 `cloud_targets.last_error`
    根本没有这一行可写——用户在备份页看到的仍然是「备份完成」，
    那份异地副本却从来没有出现过（⑤O 定过的同一条规矩：写的时候就要指对东西）。
    """
    if raw is _UNSET:
        return keep
    # bool 先判：`False == 0` 为真，若先走"0＝解除关联"那一支，请求体里写
    # `cloud_target_id: false` 会被当成"取消关联"，把这台机器上最后一份异地副本的
    # 链接静默解开；`true` 则报类型错。同一个 JSON 形态两个结果，是查不出来的那种故障。
    if isinstance(raw, bool):
        raise RuntimeError("cloud_target_id 要是异地目标的 id、0 或 null")
    if raw is None or raw == 0:
        return None
    if not isinstance(raw, int) or raw < 0:
        raise RuntimeError("cloud_target_id 要是异地目标的 id、0 或 null")
    if not dbm.query_one("SELECT id FROM cloud_targets WHERE id=?", (raw,)):
        raise RuntimeError(f"异地目标 {raw} 不存在")
    return raw


def _validate_fields(i: dict):
    if i.get("schedule") is not None and i["schedule"] not in ("manual", "daily", "weekly"):
        raise RuntimeError("schedule 必须是 manual/daily/weekly")
    if i.get("hour") is not None and (not isinstance(i["hour"], int) or not 0 <= i["hour"] <= 23):
        raise RuntimeError("hour 必须在 0-23")
    if i.get("minute") is not None and (not isinstance(i["minute"], int) or not 0 <= i["minute"] <= 59):
        raise RuntimeError("minute 必须在 0-59")
    if i.get("keep") is not None and (not isinstance(i["keep"], int) or not 1 <= i["keep"] <= 365):
        raise RuntimeError("keep 必须在 1-365")


async def create_backup(i: dict) -> dict:
    if i.get("kind") not in KINDS:
        raise RuntimeError("kind 必须是 pg/app/sqlite")
    _validate_target(i["kind"], i.get("target"))
    _validate_fields(i)
    cloud = _cloud_target_id(i.get("cloud_target_id", _UNSET))
    res = dbm.execute(
        "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,cloud_target_id) VALUES(?,?,?,?,?,?,?,?)",
        (
            i["kind"],
            i.get("target"),
            i.get("schedule") or "daily",
            i.get("hour") if i.get("hour") is not None else 3,
            i.get("minute") if i.get("minute") is not None else 30,
            i.get("keep") if i.get("keep") is not None else 7,
            # 与 update_backup 同一套真值判断。旧写法 `is False` 只认真假的 False：
            # 实测 create 传 {"enabled": 0} 会被记成 1，PATCH 传同样 payload 记成 0，
            # 同一个请求体在 POST/PATCH 下语义相反（JSON 里没有 Python 的 False 时，
            # 数字 0、空串是常见的"关"写法）。缺省仍然视为开启。
            1 if (i.get("enabled") is None or i["enabled"]) else 0,
            cloud,
        ),
    )
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (res,))
    if not row:
        raise RuntimeError("备份任务创建后未能读回，请检查数据库")
    try:
        await _sync_timer(row)
    except Exception as e:  # noqa: BLE001
        # timer 没落地却把行留在库里，列表就会显示一条"已启用但永远不会跑"的任务，
        # 而且用户完全没有线索。撤销这条半成品再报错。
        await _discard(row["id"])
        raise RuntimeError(f"备份计划落地失败，任务已撤销：{e}") from e
    return row


async def _discard(bid: int):
    """删掉一条 timer 未成功落地的备份任务（systemd 侧残留一并清）。"""
    # 清理尽力而为，最终以库里的行被删掉为准
    with suppress(Exception):
        await delete_backup(bid)
    dbm.execute("DELETE FROM backups WHERE id=?", (bid,))


async def update_backup(bid: int, i: dict) -> dict:
    cur = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not cur:
        raise RuntimeError("备份任务不存在")
    _validate_fields(i)
    merged = {
        "kind": i["kind"] if i.get("kind") is not None else cur["kind"],
        "target": i["target"] if i.get("target") is not None else cur["target"],
    }
    if merged["kind"] not in KINDS:
        raise RuntimeError("kind 必须是 pg/app/sqlite")
    # create 有、update 没有——这道不对称就是上面 _validate_target 说的漏洞，补齐。
    # 存在性只在 target **真的被换了** 时才查：换一个目标就得指对东西。
    # 编辑弹窗是把当前行整份回传的（Backups.vue 的 form 一直带着 target），
    # 若按"请求体有没有 target"判断，⑤L 的 target_missing 行就会连"改备份时间"
    # 和"停用"都做不了——只剩删除一条路，而用户可能想先留产物、把计划关掉。
    _validate_target(
        merged["kind"], merged["target"], app_must_exist=str(merged["target"]) != str(cur["target"])
    )
    cloud = _cloud_target_id(i.get("cloud_target_id", _UNSET), cur["cloud_target_id"])
    dbm.execute(
        "UPDATE backups SET kind=?,target=?,schedule=?,hour=?,minute=?,keep=?,enabled=?,cloud_target_id=? WHERE id=?",
        (
            merged["kind"],
            merged["target"],
            i["schedule"] if i.get("schedule") is not None else cur["schedule"],
            i["hour"] if i.get("hour") is not None else cur["hour"],
            i["minute"] if i.get("minute") is not None else cur["minute"],
            i["keep"] if i.get("keep") is not None else cur["keep"],
            cur["enabled"] if i.get("enabled") is None else (1 if i["enabled"] else 0),
            cloud,
            bid,
        ),
    )
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not row:
        raise RuntimeError("备份任务更新后未能读回，请检查数据库")
    try:
        await _sync_timer(row)
    except Exception as e:  # noqa: BLE001
        # 库里已是新值、systemd 还是旧 timer，两边不一致且界面上看不出来：
        # 把库还原成改动前那一行，再按同一口径报错。
        dbm.execute(
            "UPDATE backups SET kind=?,target=?,schedule=?,hour=?,minute=?,keep=?,"
            "enabled=?,cloud_target_id=? WHERE id=?",
            (
                cur["kind"], cur["target"], cur["schedule"], cur["hour"], cur["minute"],
                cur["keep"], cur["enabled"], cur["cloud_target_id"], bid,
            ),
        )
        # 还原时连 timer 也同步不上就是环境问题，别盖掉原始报错
        with suppress(Exception):
            await _sync_timer(cur)
        raise RuntimeError(f"备份计划更新失败，已还原为改动前的配置：{e}") from e
    return row


async def delete_backup(bid: int):
    # 不存在就报错，和 update_backup / delete_app 同一口径。旧写法对 999999 静默走
    # 一遍 systemctl disable + daemon-reload 再 DELETE 0 行，对外照样返回 {"ok": true}：
    # 链接失效、另一标签页已删，调用方都会以为删掉了。
    if not dbm.query_one("SELECT id FROM backups WHERE id=?", (bid,)):
        raise RuntimeError("备份任务不存在")
    u = _unit_base(bid)
    unit_dir = _unit_dir()
    await run("systemctl", ["disable", "--now", f"{u}.timer"], timeout=30)
    for f in (f"{unit_dir}/{u}.service", f"{unit_dir}/{u}.timer"):
        if os.path.exists(f):
            os.unlink(f)
    await run("systemctl", ["daemon-reload"])
    dbm.execute("DELETE FROM backups WHERE id=?", (bid,))
    # 推送台账跟着删：任务没了，这些行既不会被 `already_pushed` 再查到（它按任务取产物名），
    # 又会让异地目标的 `pushed_count` 一直虚高——用户看到的是"这个目标推过 40 份"，
    # 而目录里那条任务已经不存在，谁都无从核对。**远端的对象一个都不删**：
    # 删配置不等于删数据，这一点和 delete_target 是同一条规矩。
    dbm.execute("DELETE FROM backup_pushes WHERE backup_id=?", (bid,))
    # 告警标记跟着清（见 alerts_service.forget_backup）：巡检按 `backup:<id>` 编 code，
    # 任务删了这两个键就再没人读。局部导入是必须的——alerts_service 顶部就 import 了本模块。
    from . import alerts_service

    alerts_service.forget_backup(bid)


def save_upload(bid: int, filename: str, data: bytes, overwrite: bool = False) -> dict:
    """把外部产物（从另一台机器拷来的 dump）登记进某个备份任务名下。

    文件名必须补上 `bk<id>-` 前缀：恢复与删除都靠这个前缀判断归属（见 _backup_file_path），
    原样保存用户上传的名字会让文件躺在目录里却点不动恢复按钮。
    """
    job = dbm.query_one("SELECT id, kind, target FROM backups WHERE id=?", (bid,))
    if not job:
        raise RuntimeError("备份任务不存在")
    suffixes = _UPLOAD_SUFFIXES.get(job["kind"]) or ()
    if not suffixes:
        raise RuntimeError(f"不支持给 {job['kind']} 类任务上传产物")
    base = os.path.basename(str(filename or "")).strip()
    if not base or base.startswith(".") or base in (".", ".."):
        raise RuntimeError("文件名不合法")
    name = base if base.startswith(f"bk{bid}-") else f"bk{bid}-manual-{base}"
    if not name.endswith(suffixes):
        raise RuntimeError(f"{job['kind']} 类备份文件名要以 {'、'.join(suffixes)} 结尾")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise RuntimeError(f"文件超过 {config.MAX_UPLOAD_BYTES // 1024 // 1024}MB，请走服务器拷贝")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, name)
    existed = os.path.lexists(dest)
    if os.path.islink(dest):
        # 一律拒绝。旧写法靠 O_NOFOLLOW 打开链接时得到 ELOOP 来兑现这句拒绝；
        # 改成先写临时文件再 rename 之后必须显式挡在这里——replace 会把链接换成一个
        # 真文件，等于替用户把"这份备份其实躺在别处"的线索抹掉。
        raise RuntimeError("目标是符号链接，为避免写到别处已拒绝")
    if existed and not overwrite:
        raise RuntimeError(f"同名文件已存在：{name}")
    # 落盘余量：这是面板里最大宗的按声明值写入（整库 dump 常有几百 MB），
    # 线仍是设置页那条 alert_disk_pct（同一个 write_budget 读者）。
    assert_write_budget(BACKUP_DIR, len(data), "上传这份备份产物")
    # 同目录临时文件 + os.replace：旧写法是 O_TRUNC 就地改写，一次中途失败（盘满、
    # 进程被取消）会把上一份完好的备份改成半份，而半份文件的名字照样带 `bk<id>-` 前缀，
    # 列表列得出来、恢复按钮点得动——用户是在"以为这里有一份好备份"的状态下点恢复的。
    fd, tmp = tempfile.mkstemp(dir=BACKUP_DIR, prefix=f".{name}.", suffix=".tmp")
    try:
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except OSError as e:
            raise RuntimeError(f"写入失败：{e.strerror or e}") from None
    finally:
        # replace 成功后这个 unlink 只会碰到"路径已经不在了"，suppress 掉即可；
        # 真正的残留只发生在进程被 SIGKILL 掐在两步之间，那点字节由下一次清理目录的人处理。
        with suppress(OSError):
            os.unlink(tmp)
    return {"name": name, "size": len(data), "overwrote": existed}


async def delete_file(bid: int, filename: str) -> dict:
    """只删一个备份产物，不动任务、不动 timer。

    prune 只数自动生成的产物（按写入顺序删最旧的），手动删掉一份自动备份等于给下一轮
    腾出名额，而删掉上传的产物只是腾磁盘、不影响名额；
    备份目录不在 data/ 下，也没有别的接口能删它，所以这里必须自己把名字记进审计，
    事后才查得到是谁在什么时候清了哪一份。
    """
    path = _backup_file_path(bid, filename)
    os.unlink(path)
    return {"ok": True, "name": os.path.basename(path)}


_SQLITE_NAME_RE = re.compile(r"^bk\d+-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?-(.+)\.sqlite\.gz$")


def _restoreable(kind: str, name: str) -> bool:
    """这个备份文件能不能走自动恢复。

    pg 的 .sql.gz 是 pg_dumpall 的整库文本档，恢复意味着重建集群里所有库和角色，
    绝不能由一个按钮触发；面板自身数据库（bk0- 之类）也不该被服务覆盖。
    """
    if kind == "sqlite":
        return name.endswith(".sqlite.gz")
    if kind == "app":
        return name.endswith((".tar.gz", ".tgz"))
    if kind == "pg":
        return name.endswith(".dump.gz")
    return False


def _backup_file_path(bid: int, filename: str) -> str:
    """恢复只允许读取"本任务名下、真实落在备份目录里"的普通文件。

    filename 完全来自请求体：直接拼路径就是任意文件读取（`../../etc/shadow`），
    而且它会成为 tar 解包与 pg_restore 的输入。这里同时管住目录穿越与软链接跳出。
    """
    if not filename or filename != os.path.basename(filename):
        raise RuntimeError("备份文件名不合法")
    if not filename.startswith(f"bk{bid}-"):
        raise RuntimeError("该文件不属于这个备份任务")
    real = os.path.realpath(os.path.join(BACKUP_DIR, filename))
    if os.path.dirname(real) != os.path.realpath(BACKUP_DIR):
        raise RuntimeError("备份文件名不合法")
    if not os.path.isfile(real):
        raise RuntimeError("备份文件不存在（可能已被保留策略清理）")
    return real


def _sqlite_app(target):
    t = str(target or "")
    app = None
    if t.isdigit():
        app = dbm.query_one("SELECT * FROM apps WHERE id=?", (int(t),))
    if not app:
        app = dbm.query_one("SELECT * FROM apps WHERE name=?", (t,))
    if not app:
        raise RuntimeError("备份目标应用已不存在")
    if not os.path.isdir(app["path"]):
        raise RuntimeError(f"应用目录不存在：{app['path']}")
    return app


async def _sh_script(script: str, timeout: int = backup_runner.JOB_TIMEOUT_SEC) -> dict:
    """bash + pipefail 执行整段脚本，返回 {code, out}（pipefail 才不会吞管道左侧失败）。

    默认值是备份任务的时间预算（backup_runner.JOB_TIMEOUT_SEC），和 timer 那条路同源：
    这里以前是个孤立的 1800，runner 里的 `run_sh` 却是"没有超时"，同一个 pg_dump
    从网页点和从夜里跑，一个最多等 30 分钟、一个可以等到天荒地老。
    """
    return await run("/bin/bash", ["-o", "pipefail", "-c", script], timeout=timeout)


# pg_restore -l 的条目行形如 `3420; 0 18851 TABLE DATA public t postgres`。
# 对象名本身可能是两个词（TABLE DATA / MATERIALIZED VIEW / FOREIGN KEY …），
# 所以不去切第 4 个字段，直接按行首前缀数：数错一项就等于把"归档里没数据"
# 说成"有数据"，而那正是本次要拦的事。
_PG_TOC_ENTRY = re.compile(r"^\d+; \d+ \d+ \S", re.M)
_PG_TOC_DATA = re.compile(r"^\d+; \d+ \d+ TABLE DATA ", re.M)


async def _pg_preflight(src_file: str) -> tuple[int, int, str | None]:
    """在动目标库之前把产物读一遍，返回 (对象条目数, TABLE DATA 条目数, 错误说明)。

    旧流程是"先 dump 留档 → drop/create → gzip -dc | pg_restore"，坏产物要在这之后
    才被发现：此刻库已经被 drop 了，只能靠留档重建（留档自己也可能失败，那时库就是空壳）。
    而 `pg_restore -l` 只读头部就能判掉三类坏产物（实测退出码均为 1）：
    截断的 gzip（`could not read from input file: end of file`）、空 gz
    （`input file is too short`）、以及被当归档上传的纯文本 dump
    （`input file appears to be a text format dump`）。

    退出码只取 pg_restore 那一侧：`-l` 拿到目录就收工，不再把 stdin 读完，gzip 于是
    被 SIGPIPE 打死。本机（PG 17.11）对一份合法备份实测 `left=141 right=0`，而
    `_sh_script` 开着 pipefail —— 按整条管道的退出码判，**每一份正常备份都会被当成坏产物
    拒掉**，恢复功能当场全废。gzip 那侧真坏（截断/CRC 不符）时 pg_restore 自己必然报错，
    所以只看它是安全的：同一写法下三类坏产物仍全部退出码 1（线上实测）。

    还有一层要说清：`-l` 证明的是"归档目录读得出来"，不是"数据一个字节不缺"
    （把 -Fc 截到 60% 再重新 gzip，`-l` 照样回 0）。所以覆盖前留档和 restore 阶段的
    --exit-on-error 一个都不能省。
    """
    script = f"gzip -dc {sh_escape(src_file)} | pg_restore -l; exit ${{PIPESTATUS[1]}}"
    r = await _sh_script(script, timeout=120)
    if r["code"] != 0:
        return 0, 0, ((r["out"] or "").strip()[-300:] or f"exit {r['code']}")
    out = r["out"] or ""
    return len(_PG_TOC_ENTRY.findall(out)), len(_PG_TOC_DATA.findall(out)), None


def _gzip_isize(p: str) -> int:
    """gzip 末尾 4 字节声明的原始尺寸（模 2^32）；读不到或为 0 视为"没有声明"。

    这是**声明值**，不是保证：伪造的尾部会让它偏小，所以真实字节还得边解边数；
    反过来，超过 4GiB 的正当库会把它绕回一个小数字，所以它只能用来做开头那道拒绝，
    不能当解压上界（拿它当上界会把 5GiB 的库恢复判成 1GiB、在写到 1GiB 时误杀）。
    """
    try:
        with open(p, "rb") as f:
            f.seek(-4, os.SEEK_END)
            raw = f.read(4)
    except OSError:
        return 0
    return int.from_bytes(raw, "little") if len(raw) == 4 else 0


def _gunzip_to(src_file: str, dest: str) -> None:
    """把 gz 解到 dest，落盘字节按 `write_budget()` 设两道界。

    产物本身受上传上限（`CP_MAX_UPLOAD_MB`，默认 8MB）约束，而 gzip 压缩比能到上千倍：
    一个 8MB 的包可以解出 8GB。落点更不是面板自己的备份目录，而是**目标库所在的那个
    应用目录**（暂存与真库同目录才能 `os.replace`）——root 进程把那块盘写满，代价是
    这台机器上所有业务一起写不进去。所以两道都要：
    - 开头按 gzip 声明值判一次：明显塞不下就一个字节都不写（挡住"这盘本来就快满了"）。
    - 边解边数真实字节，超过余量就停手：声明值可以是假的（挡住压缩炸弹）。
    中途停下是安全的：`dest` 是本次恢复的暂存文件，`_restore_sqlite` 收到异常就删掉它，
    而目标库还没被换、服务还没被停（`_stop_for_overwrite` 在更后面）。
    """
    d = os.path.dirname(dest)
    declared = _gzip_isize(src_file)
    if declared:
        assert_write_budget(d, declared, "解压这份备份")
    b = write_budget(d)
    written = 0
    with gzip.open(src_file, "rb") as g, open(dest, "wb") as f:
        while True:
            chunk = g.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > b["room"]:
                raise RuntimeError(
                    f"解压这份备份已经写了 {human_bytes(written)}，超过 {d} 所在分区按磁盘警戒线 "
                    f"{b['line']}% 只能再容纳的 {human_bytes(b['room'])}（实际剩余 {human_bytes(b['avail'])}）："
                    "产物声明的尺寸与实际不符（疑似压缩炸弹），已中止；暂存文件会被删除，目标库未被改动"
                )
            f.write(chunk)
        # 目标是线上库文件，os.replace 只保证"重命名"这一步原子，不保证内容已经
        # 离开页缓存：中间断电的话库里留下的就是一个截断文件，而面板已经报了成功。
        f.flush()
        os.fsync(f.fileno())


def _quick_check(path: str) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _sqlite_stats(path: str) -> tuple[bool, int | None]:
    """(是不是真正的 SQLite 文件, 用户表数)；表数拿不到时为 None。

    单看 `PRAGMA quick_check` 挡不住空产物：**0 字节的文件在 SQLite 眼里就是一个合法的
    空库**，quick_check 直接回 ok（线上实测），于是恢复会拿一个空文件把有数据的库换掉，
    还报「数据已写回」。所以覆盖之前必须自己认头部。
    用只读 URI 打开：以读写方式打开一个空文件，SQLite 会当场把它初始化成空库，
    "检查"这一步本身就改了产物。
    """
    try:
        with open(path, "rb") as f:
            if f.read(16) != _SQLITE_MAGIC:
                return False, None
        con = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    except (OSError, sqlite3.Error):
        return False, None
    try:
        n = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        return True, int(n)
    except sqlite3.Error:
        return True, None
    finally:
        con.close()


async def _stop_for_overwrite(unit: str, log: list) -> tuple[bool, bool]:
    """覆盖文件/数据库之前把服务停干净，返回 (本次停过, 恢复完要不要拉起)。

    这里问的是"此刻有没有进程活着或正要起来"（systemd_ops.BUSY_STATES），不是列表页
    那个"健康运行中"：Type=simple 起立的那几秒、Type=oneshot 正在执行的那一轮，
    ActiveState 都是 activating，按老口径判成"没在跑"就跳过停止——备份是直接盖在一个
    正要 exec 的进程上的，SQLite 那侧还会把旧连接的 -wal/-shm 当成残留删掉。
    - 查不到状态（systemctl 报错/超时）不能当成"没在跑"：照发 stop，真没在跑它是空操作，
      systemctl 本身坏了就在这里抛错，恢复在动手之前就停下，而不是盖完再补一句"我其实不知道"。
    - failed（上次启动失败/崩溃循环）不属于"运维特意停着"，而恢复数据库/目录正是冲它来的，
      所以结束后要拉起；特意 stop 着的（inactive）保持停止，尊重现状。
    返回的两个旗标供调用方决定收尾，任何一条路都不能"什么都不说"。
    """
    if not systemd_ops.has_systemd():
        log.append(f"==> 本机没有 systemd（容器/WSL），{unit} 不由面板管理，只写文件")
        return False, False
    st = await systemd_ops.unit_state(unit)
    if st["busy"]:
        await systemd_ops.service_action(unit, "stop")
        log.append(f"==> 已停止 {unit}")
        return True, True
    if st["state"] == "unknown":
        await systemd_ops.service_action(unit, "stop")
        log.append(f"==> {unit} 状态查不到（systemctl 没有回话），按最坏情况处理：已发出停止")
        return True, True
    if st["missing"]:
        log.append(f"!! 本机没有 {unit} 这个 unit（未安装或已改名），只写文件，不会尝试启动")
        return False, False
    if st["state"] == "failed":
        log.append(f"==> {unit} 处于 failed（上一次启动失败），本次恢复后会重新拉起")
        return False, True
    log.append(f"==> {unit} 未在运行（{st['state'] or '未知'}），跳过停止")
    return False, False


async def _start_after_restore(unit: str, log: list, *, from_failed: bool = False) -> None:
    if from_failed:
        # failed 的 unit 攒着启动失败计数，不清就直接 start 会被 systemd 拒掉，
        # 用户看到的只有"服务启动失败"，猜不到还差这一步。
        await systemd_ops.reset_failed(unit)
    try:
        await systemd_ops.service_action(unit, "start")
        log.append(f"==> 已启动 {unit}")
    except Exception as e:  # noqa: BLE001
        log.append(f"!! 服务启动失败，请手工启动 {unit}：{e}")


async def _restore_sqlite(bk: dict, src_file: str, log: list) -> None:
    from . import apps_service

    app = _sqlite_app(bk["target"])
    # 恢复流程会停掉再启动这个 unit，部署也在同一时刻按自己的快照启停同一个 unit，
    # 两边交错出来的"服务是谁停的、该不该起来"没有任何一份日志解释得清。
    apps_service._ensure_no_deployment(app["id"], "恢复数据库")
    unit = apps_service.unit_name(app)
    ts = backup_runner.stamp()

    # 恢复会把目标库整份覆盖，出事时用户唯一能退回的就是覆盖前的那一版，
    # 所以这一步不能省、也不能放在覆盖之后。用在线备份 API 而不是 cp：
    # 正在被写入的库直接复制出来的文件恢复时会报 database image is malformed。
    # 整目录 walk 在几千文件的 node_modules 上要几百毫秒，不该压着事件循环。
    cands = await run_in_threadpool(backup_runner.find_sqlite_dbs, app["path"])
    if not cands:
        raise RuntimeError(f"{app['path']} 下没有找到数据库文件（*.db / *.sqlite*）")
    m = _SQLITE_NAME_RE.match(os.path.basename(src_file))
    if m:
        stem = m.group(1)
        # 留档名带来源库的名字再加 -pre-restore 后缀（见下方 pre 的命名），
        # 这样点它的"恢复"能退回被本次覆盖掉的那一版，而不是报找不到库。
        if stem.endswith("-pre-restore"):
            stem = stem[: -len("-pre-restore")]
        dst = next((c for c in cands if os.path.splitext(os.path.basename(c))[0] == stem), None)
        if not dst:
            raise RuntimeError(f"目录里找不到与备份同名的数据库 {stem}.db；现有候选：{', '.join(cands[:4])}")
    else:
        # 旧格式（文件名不带源库名）只能按"最大的那个库"定位，如实说明而不是假装精确
        dst = cands[0]
        log.append(f"!! 备份文件名未记录源库名，按目录内最大库定位：{dst}")

    # 留档写在备份目录里、用 bk<id>-<时间戳>-<库名>-pre-restore 命名：备份文件页只列这个
    # 目录，界面又写着"可退回本次自动留存的 pre-restore 快照"——快照落在应用目录里
    # 就等于用户看不见它，恢复失败后既找不到也点不动。名字带完整时间戳，落在 prune
    # 认得的形状里（按写入顺序排在末尾），要等真实备份轮替过 keep 名额才会被清掉。
    # 必须走 _free_path 而不是直接拼名：从留档回滚时两次的时间戳可能落在同一秒，
    # 而留档名与源文件名同形——把留档写在源文件上等于先把它覆盖成当前内容，
    # 于是"退回上一版"变成原地不动，且唯一的回滚数据就此消失。
    db_stem = os.path.splitext(os.path.basename(dst))[0]
    pre = backup_runner._free_path(bk["id"], ts, f"-{db_stem}-pre-restore.sqlite.gz", os.path.dirname(src_file))
    # 下面三步都是百 MB 级的同步读改写（在线备份 + gzip、解压、全库 quick_check），
    # 直接在事件循环里跑会让整个面板在恢复期间停响应，所以全部丢给工作线程。
    await run_in_threadpool(backup_runner.sqlite_snapshot, dst, pre)
    log.append(f"==> 已留存当前库：{os.path.basename(pre)}")

    # 暂存必须写在**目标库所在的同一个目录**里。旧写法写在备份目录
    # （f"{src_file}.restoring-<ts>"），而下一步是 os.replace(tmp, dst)——
    # 跨文件系统时它直接抛 OSError(EXDEV)。把 CP_BACKUP_DIR 放到另一块盘或 NFS
    # 上是很常见的配置（备份目录的存在意义就是和数据分开），于是恢复偏偏在
    # 最危险的那一步失败：此刻服务已经停了、目标库的 -wal/-shm 已经删了，
    # 用户拿到一句 "Cross-device link"，还要自己猜数据现在是什么状态。
    # hidden + .tmp 的名字不会被 find_sqlite_dbs 认成候选库（它按 *.db/*.sqlite*
    # 的后缀筛），异常残留时也不会再躺进备份文件页。
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".cp-restore-", suffix=".tmp")
    os.close(fd)
    try:
        await run_in_threadpool(_gunzip_to, src_file, tmp)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    mode = os.stat(dst).st_mode & 0o777
    try:
        ok = await run_in_threadpool(_quick_check, tmp)
    except sqlite3.Error as e:
        os.unlink(tmp)
        raise RuntimeError(f"快照文件无法打开，未做任何改动：{e}") from e
    if ok != "ok":
        os.unlink(tmp)
        raise RuntimeError(f"快照完整性检查未通过（{ok}），已放弃恢复")
    valid, tables_in_snap = await run_in_threadpool(_sqlite_stats, tmp)
    if not valid:
        # 体积要在删除之前取：先 unlink 再 getsize 会让这句说明变成 FileNotFoundError，
        # 用户看到的不是"产物不合法、目标库未动"，而是一条看不懂的 OS 错误。
        size = os.path.getsize(tmp)
        os.unlink(tmp)
        raise RuntimeError(
            f"快照不是合法的 SQLite 数据库（文件为空或头部不符，{size} 字节），"
            "已放弃恢复，目标库未被改动"
        )
    _, tables_now = await run_in_threadpool(_sqlite_stats, dst)
    shown_snap = "?" if tables_in_snap is None else str(tables_in_snap)
    shown_now = "?" if tables_now is None else str(tables_now)
    if tables_in_snap == 0 and tables_now:
        # 空快照换掉有数据的库，quick_check 一样回 ok（它只保证结构自洽，不保证有内容），
        # 这一步不拦——用户可能确实要退回一个空库；但必须让界面知道本次恢复的结果是"空"。
        log.append(
            f"!! 快照里 0 张表，而当前库有 {tables_now} 张：本次恢复会把库清空，"
            f"如需退回请恢复 {os.path.basename(pre)}"
        )
    else:
        log.append(f"==> 产物预检通过：快照 {shown_snap} 张表 / 当前库 {shown_now} 张")

    stopped, restart = await _stop_for_overwrite(unit, log)
    if stopped:
        # 面板自己停的服务，要在持久记录里留下名字：进程被杀在覆盖中途时，
        # 启动对账靠这一行判断"这个 unit 是恢复流程停的，可以替它拉回来"。
        _note_stopped_unit(bk, unit)
    try:
        # 残留的 WAL/SHM 属于旧库：留着它们，服务一打开就会把旧帧套到刚写回的库上
        for ext in ("-wal", "-shm", "-journal"):
            if os.path.exists(dst + ext):
                os.unlink(dst + ext)
        os.replace(tmp, dst)
        os.chmod(dst, mode)
        log.append(f"==> 数据已写回 {dst}")
    except Exception:
        for p in (tmp,):
            if os.path.exists(p):
                with suppress(OSError):
                    os.unlink(p)
        raise
    finally:
        # 只有真的停过、或恢复前就是崩掉的 failed 才去启动：service_action("stop") 自己
        # 失败时服务从未被我们动过，这时补一个 start 等于把运维特意停着的服务悄悄拉起来
        # （与 _restore_app 保持一致）。
        if stopped or restart:
            await _start_after_restore(unit, log, from_failed=restart and not stopped)
        else:
            log.append(f"==> {unit} 未被面板停止，保持原状态")


def _tar_checked(tf: tarfile.TarFile) -> list:
    """整份归档索引 → 可解包的成员列表，不安全的一律拒绝。

    链接型条目会把写入引到目录之外（备份里一个 node_modules 软链就能覆盖系统文件），
    所以只放行普通文件与目录。
    """
    members = []
    for m in tf.getmembers():
        name = m.name.replace("\\", "/")
        parts = [p for p in name.split("/") if p]
        if name.startswith("/") or ".." in parts or not parts:
            raise RuntimeError(f"归档含不安全路径：{m.name}")
        if not (m.isfile() or m.isdir()):
            raise RuntimeError(f"归档含不支持的条目类型：{m.name}")
        members.append(m)
    return members


def _reject_symlink_ancestors(dest: str, members: list) -> None:
    """解包前挡掉「落点穿过已存在软链」的归档。

    `_tar_checked` 管的是归档里条目的类型，管不了目标目录里本来就有软链：应用目录里
    躺着一条 `data -> /var/lib/app`（安装脚本把可变数据挪出仓库的常见写法），而解包
    按名字落盘，于是 `data/x.json` 写进的是 /var/lib/app。面板以 root 运行，一份手工
    上传的归档就能借这条链接往目录外任意位置落文件——正是"备份页上传即 RCE"之外
    更安静的一条越权路径。
    判据按每一级路径做 lstat（含文件本身：现存的 config.json 若是软链，写它就是写
    链接目标），命中就整份拒绝。宁可拒一份可疑产物，也不要"解了一半"的目录。
    """
    seen: set[str] = set()
    for m in members:
        acc = dest
        for p in m.name.replace("\\", "/").split("/"):
            if not p or p == ".":
                continue
            acc = os.path.join(acc, p)
            if acc in seen:
                continue
            seen.add(acc)
            if os.path.islink(acc):
                raise RuntimeError(
                    f"目标目录里 {acc.removeprefix(dest)} 是符号链接，为避免写到 {dest} 之外已拒绝整份归档"
                )


def _strip_setuid(dest: str, members: list) -> int:
    """解包后收平 setuid/setgid/sticky，返回被收平的条目数。

    恢复刻意用 filter="tar"（要按归档里的属主与权限原样落盘），于是归档里的 4755
    也照原样落盘：面板以 root 解包，解出来的 setuid-root 二进制躺在应用目录里，
    同机任何一个被攻破的应用进程就白拿一个提权落点。面板自己产的归档不会有这些位
    （tar 的 mode 来自磁盘真实文件，应用目录里本就不该有 setuid 程序），
    所以这一步只可能动到手工上传的第三方归档——那正是该挡的对象。
    """
    n = 0
    for m in members:
        if not m.isfile() or not (m.mode & 0o7000):
            continue
        p = os.path.join(dest, m.name)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        # 复核 lstat：链接型条目已被 _tar_checked 拒掉，这里再确认落点仍是普通文件，
        # 免得有人在解包与收权限之间塞一个链接进来。
        if not stat.S_ISREG(st.st_mode) or not (st.st_mode & 0o7000):
            continue
        with suppress(OSError):
            os.chmod(p, stat.S_IMODE(st.st_mode) & ~0o7000)
            n += 1
    return n


async def _restore_app(bk: dict, src_file: str, log: list) -> None:
    from . import apps_service

    app = dbm.query_one("SELECT * FROM apps WHERE name=?", (bk["target"],))
    if not app:
        raise RuntimeError("备份目标应用已不存在")
    # 恢复不能和部署并发：部署在开始时拿了 app 字典快照，最后用它写回 unit 并重启服务；
    # 恢复在同一目录上停服务、解包、再启动。两边交错会出现"部署结束时把 unit 指回旧路径、
    # 目录里却是新内容"这种谁也解释不了的现场。删除/回滚/改配置都已经过这道闸（
    # apps_service._ensure_no_deployment），恢复是最后一个能改写应用目录的入口。
    apps_service._ensure_no_deployment(app["id"], "恢复应用目录")
    dest = app["path"]
    if not os.path.isdir(dest):
        raise RuntimeError(f"应用目录不存在：{dest}")
    unit = apps_service.unit_name(app)
    ts = backup_runner.stamp()

    env = os.path.join(dest, ".panel.env")
    env_keep = None
    if os.path.exists(env):
        # 归档里的 .panel.env 是备份当时的环境变量，而口令/端口可能早就改过了，
        # 覆盖回去会让服务突然起不来且毫无提示。先留存现值，解包后再还原现值。
        # 暂存写在备份目录（root 0700）而不是应用目录里：那里是一份含口令的副本，
        # 落在应用可读路径上、恢复完还一直躺着，prune 与备份页也都看不见它。
        env_keep = backup_runner._free_path(bk["id"], ts, "-pre-restore.env", os.path.dirname(src_file))
        await run_in_threadpool(shutil.copy2, env, env_keep)

    # 读索引和解包都是整份归档量级的同步读写（一个 node_modules 就是几百 MB），
    # 在循环上跑会让整个面板在恢复期间停响应。三步共用同一个 TarFile 句柄：
    # 成员条目的数据偏移量是按它自己的文件对象算的，换个实例再解包就会读错位置。
    tf = await run_in_threadpool(tarfile.open, src_file, "r:gz")
    try:
        members = await run_in_threadpool(_tar_checked, tf)
        if not members:
            # 面板自己产的归档至少含 `.` 这一条（tar -C <app> -cf - .），一个条目都没有
            # 就说明这份不是完整备份。应用目录恢复**没有**整份快照可退（见 BackupFiles.vue
            # 的说明），所以在停服务之前必须硬挡下来，而不是解出 0 个文件后报成功。
            raise RuntimeError(
                f"归档里没有任何条目（{os.path.getsize(src_file)} 字节），未停止服务、未改动目录；"
                "这份产物不是完整备份，请改用更早的一份"
            )
        # 落点检查必须在停服务之前：拒绝一份穿越软链的归档不该顺带把线上服务停掉。
        await run_in_threadpool(_reject_symlink_ancestors, dest, members)
        # 余量检查同样必须在停服务之前。恢复用的是「还能往这块盘写多少」这条界
        # （出处：设置页的磁盘警戒线），而不是文件页那对固定上限 —— 这台机器上真实应用
        # 目录就是 992M/上万文件，CP_MAX_EXTRACT_MB=512 与 5000 条会把正当恢复全挡在
        # 门外；但一份声明要写 40GB 的归档在快满的盘上解包，代价是整机业务一起停。
        assert_write_budget(dest, sum(m.size for m in members if m.isfile()), "恢复这份归档")
        stopped = restart = False
        restored = False
        try:
            stopped, restart = await _stop_for_overwrite(unit, log)
            if stopped:
                # 面板自己停的服务，要在持久记录里留下名字：进程被杀在覆盖中途时，
                # 启动对账靠这一行判断"这个 unit 是恢复流程停的，可以替它拉回来"。
                _note_stopped_unit(bk, unit)
            # filter="tar" 是把当前行为钉住，不是放行风险：不安全条目（绝对路径、..、
            # 链接型）已由 _tar_checked 逐条拒掉，这里要的正是"按归档里的属主/权限原样落盘"。
            # 不写出来的话 Python 3.14 会默认改用 data filter，恢复出来的文件属主当场变掉。
            await run_in_threadpool(tf.extractall, dest, members, filter="tar")
            restored = True
            suid = await run_in_threadpool(_strip_setuid, dest, members)
            log.append(f"==> 已解包 {len(members)} 个条目到 {dest}")
            if suid:
                log.append(f"!! {suid} 个条目带 setuid/setgid/sticky 位，已收平为普通权限")
        finally:
            if env_keep:
                # 不管解包成没成，环境变量都得回到现值：半程失败时归档里那份
                # 旧 .panel.env 可能已经落下去了，留着它服务下次起来用的是别的口令。
                # 这句回写可能整个失败（磁盘满、权限），而旧写法无条件记
                # 「保留当前 .panel.env」——那句话在此刻是假的，用户会以为生效的
                # 还是现值，实际用的是归档里备份当时的口令。按结果如实记。
                env_kept = False
                with suppress(Exception):
                    await run_in_threadpool(shutil.copy2, env_keep, env)
                    env_kept = True
                with suppress(OSError):
                    os.unlink(env_keep)
                if env_kept:
                    log.append("==> 保留当前 .panel.env（未使用归档内的环境变量）")
                else:
                    log.append(
                        f"!! 当前 .panel.env 回写失败，归档里那份（备份当时的值）已生效："
                        f"启动前请核对 {env} 里的口令与端口"
                    )
            if restored and (stopped or restart):
                await _start_after_restore(unit, log, from_failed=restart and not stopped)
            elif stopped or restart:
                # 半恢复的目录里可能缺几百个文件，把它启动起来等于让服务在残骸上
                # 写数据、跑迁移。宁可停在停止态并说清楚，也不要"看起来恢复了"。
                log.append(f"!! 解包中断：{dest} 可能处于半恢复状态，服务保持停止；请重跑恢复或改用更早的备份")
    finally:
        tf.close()


async def _restore_pg(bk: dict, src_file: str, log: list) -> None:
    db = bk["target"]
    if db == "all":
        raise RuntimeError("整库备份（pg_dumpall）请手工恢复：gzip -dc 文件 | sudo -u postgres psql -v ON_ERROR_STOP=1")
    if not is_ident(db):
        raise RuntimeError("库名不合法")
    ts = backup_runner.stamp()
    # 预检放在最前面，连留档都还没写：坏产物此刻不该碰任何东西。
    toc_objs, data_objs, toc_err = await _pg_preflight(src_file)
    if toc_err:
        raise RuntimeError(
            f"备份产物 {os.path.basename(src_file)} 读不出归档目录，未做任何改动：{toc_err}\n"
            "（.dump.gz 必须是 pg_dump -Fc 再 gzip 的产物；纯文本 dump 请用 psql 手工恢复）"
        )
    log.append(f"==> 产物预检通过：归档含 {toc_objs} 个对象（其中 TABLE DATA {data_objs} 项）")
    # 文件名保持 bk<id>-<时间戳>-pre-restore.dump.gz：这份留档要能被 prune 认出来
    # （它占 keep 名额、按写入顺序排在末尾，之后随轮替一起淘汰）；写成
    # bk<id>-pre-restore-... 不在 prune 的时间戳形状里，等于每次恢复都留下一个永不轮替的
    # 文件把名额吃掉。
    pre = backup_runner._free_path(bk["id"], ts, "-pre-restore.dump.gz", BACKUP_DIR)
    # 恢复会 drop 目标库，先给当前内容做一份同目录的 dump 作为退路
    code = (await _sh_script(backup_runner.dump_cmd(f"su -s /bin/sh postgres -c 'pg_dump -Fc {db}'", pre)))["code"]
    if code != 0:
        raise RuntimeError(f"当前库快照失败（exit {code}），未做任何改动")
    log.append(f"==> 已留存当前库：{os.path.basename(pre)}")

    owner = ""
    with suppress(Exception):
        owner = (await psql(f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{db}';")).strip()
    if not is_ident(owner):
        owner = "postgres"

    # PG 15+ 的 dropdb --force 会先断开现有连接；旧版本没有这个参数，退回普通 dropdb，
    # 这时若报 "other users are connected" 就是真有人在用，如实报错而不是强杀。
    # set -e 保证 drop 没成功就不会 createdb/restore（否则会在残留库上叠加恢复内容）。
    def su(inner: str) -> str:
        return f"su -s /bin/sh postgres -c {sh_escape(inner)}"

    def drop_create() -> str:
        """drop → createdb 一段。set -e 保证 drop 没成功就不会建库。

        原来 drop/createdb/restore 与恢复数据写在同一段脚本里，一次非零退出既可能是
        "drop 被占用的人挡住、现库完好"，也可能是"现库已删、新内容只 restore 了一半"，
        而这两种情况该做的事完全相反。拆成两段就分得清：第一段失败没碰过库，
        第二段失败库已经是空壳，必须用留档重建。
        """
        return "\n".join(
            [
                "set -e",
                f"{su(f'dropdb --if-exists --force {db}')} || {su(f'dropdb --if-exists {db}')}",
                su(f"createdb -O {owner} {db}"),
            ]
        )

    def load(restore_src: str, exit_on_error: bool = True) -> str:
        eo = " --exit-on-error" if exit_on_error else ""
        return f"gzip -dc {sh_escape(restore_src)} | {su(f'pg_restore -d {db} --no-privileges{eo}')}"

    # 先换成空库，再灌数据：分成两段才知道现库到底有没有被碰过
    r = await _sh_script(drop_create(), timeout=600)
    if r["code"] != 0:
        raise RuntimeError(f"重建空库失败（退出码 {r['code']}），当前库未被改动：{r['out'][-500:]}")

    r = await _sh_script(load(src_file), timeout=3600)
    if r["code"] != 0:
        # 此刻库已被 drop 且 restore 只走了一半：只报"失败"等于留下一个没人接管的空壳。
        # 恢复前那份留档就是为这一步准备的，先尽力装回去，再如实报告两次退出码。
        rb = await _sh_script(drop_create() + "\n" + load(pre, exit_on_error=False), timeout=3600)
        if rb["code"] == 0:
            raise RuntimeError(
                f"恢复失败（退出码 {r['code']}），已用 {os.path.basename(pre)} 把恢复前的库重建回去：{r['out'][-500:]}"
            )
        raise RuntimeError(
            f"恢复与回退均失败（{r['code']}/{rb['code']}），库 {db} 现为空壳或不完整；"
            f"请手工执行 gzip -dc {os.path.basename(pre)} | sudo -u postgres pg_restore -d {db} "
            f"--clean --if-exists：{r['out'][-300:]} / {rb['out'][-300:]}"
        )
    log.append(f"==> 库 {db} 已从 {os.path.basename(src_file)} 恢复（属主 {owner}）")
    if data_objs == 0:
        # 空库的 -Fc 产物灌进刚建好的空库，pg_restore 的退出码是 0（线上实测），
        # 单看退出码就等于把"库被清空了"报成"恢复完成"。
        log.append(
            f"!! 归档里没有任何表数据（TABLE DATA 0 项），库 {db} 现在是无数据状态；"
            f"如需退回本次覆盖前的内容，请恢复 {os.path.basename(pre)}"
        )


_MARK_PREFIX = "restore:inflight:"


def _mark_key(bid) -> str:
    return f"{_MARK_PREFIX}{bid}"


def _restore_target_app(bk: dict):
    """恢复目标指向的那条 apps 记录，查不到就返回 None，不抛。

    这里只是给锁挑一个键、给守卫看一眼有没有部署，不是恢复的前置条件：
    `_sqlite_app`/`_restore_app` 才是干活时的校验入口，它们要目录存在才肯往下走。
    用会抛的那个来挑键有两个坏处——应用已删除时锁还没拿到就报"应用目录不存在"，
    报错的人以为自己点了恢复的正文，其实连锁都没上；而 `key_name` 那条回退分支
    永远走不到，等于给"记录已不在的历史备份"配了一把不存在的锁。
    """
    target = str(bk["target"] or "")
    if bk["kind"] == "sqlite":
        if target.isdigit():
            app = dbm.query_one("SELECT id FROM apps WHERE id=?", (int(target),))
            if app:
                return app
        return dbm.query_one("SELECT id FROM apps WHERE name=?", (target,))
    if bk["kind"] == "app":
        return dbm.query_one("SELECT id FROM apps WHERE name=?", (target,))
    return None


def _ensure_no_deployment_for_restore(bk: dict) -> None:
    """恢复不能和进行中的部署并发（批次㉘ 的守卫，㉝ 之后它是第二道闸）。

    flock 已经挡住"同一个应用上正在跑的部署"，这一层挡的是锁之外的人：手工往
    deployments 表插的 running 行、旧版本面板留下的行。pg 类恢复动的是数据库而不是
    应用工作区，和部署不在同一个对象上，所以不拦。
    """
    app = _restore_target_app(bk)
    if app and dbm.query_one(
        "SELECT id FROM deployments WHERE app_id=? AND status='running'", (app["id"],)
    ):
        raise RuntimeError(f"应用 {bk['target']} 有部署正在进行，不能恢复")


def _restore_lock_key(bk: dict) -> str:
    """恢复锁的键 = 要覆盖的那个对象，不是备份任务行上写的字符串。

    批次㉝ 之前键是 `kind:target`，于是同一个应用的两条备份任务——目录类（target 存
    应用名）和 SQLite 类（target 存 app_id）——拿到两把锁，两次恢复可以同时
    stop 同一个 unit、同时往同一个目录和同一个库上写。解析成 app id 之后它们
    是同一把锁，而且和部署/同步/回滚/删除用的是同一把（apps_service 也按 app id 锁）。
    """
    if bk["kind"] == "pg":
        return locks.key_pg(bk["target"])
    app = _restore_target_app(bk)
    return locks.key_app(app["id"]) if app else locks.key_name(bk["target"])


def _mark_restore(bk: dict, actor: str) -> None:
    """把"正在覆盖现场"这件事写成持久记录，供面板被杀之后的启动对账读取。

    为什么不能只靠那把 flock：锁由内核在进程死亡时自动释放，恰好意味着死后的现场
    什么都不留下——而恢复在停服务与覆盖文件之间被杀掉，留下的就是一个停止的服务
    加一个半新半旧的目录，这两件事都需要人知道。"""
    dbm.set_setting(
        _mark_key(bk["id"]),
        json.dumps(
            {
                "kind": bk["kind"],
                "target": bk["target"],
                "actor": actor,
                "file": os.path.basename(str(bk.get("_file") or "")),
            },
            ensure_ascii=False,
        ),
    )


def _note_stopped_unit(bk: dict, unit: str) -> None:
    """记下"面板为了这次恢复把 {unit} 停了"——这是启动对账敢不敢替它再拉起来的依据。"""
    raw = dbm.get_setting(_mark_key(bk["id"]))
    if not raw:
        return
    try:
        rec = json.loads(raw)
    except ValueError:
        return
    rec["unit"] = unit
    rec["stopped_by_panel"] = True
    dbm.set_setting(_mark_key(bk["id"]), json.dumps(rec, ensure_ascii=False))


async def resume_interrupted_restores() -> list[str]:
    """启动时清掉遗留的恢复标记，并把能救的救回来（main.py 的 lifespan 调用）。

    每行都要说话，沉默的启动等于"上次什么都没发生"：面板被升级/OOM/重启打断在
    stop 与覆盖之间时，应用服务就那样停着，而界面上没有任何地方显示它是因为一次
    没跑完的恢复停的。判词沿用批次㉜ 的口径——查不到状态就是查不到，不擅自启动。
    """
    lines: list[str] = []
    for row in dbm.query("SELECT key,value FROM settings WHERE key LIKE ?", (f"{_MARK_PREFIX}%",)):
        key = str(row["key"])
        bid = key.removeprefix(_MARK_PREFIX)
        try:
            rec = json.loads(row["value"] or "{}")
        except ValueError:
            rec = {}
        # 先删标记：这条记录只该在启动时被说起一次，留着会让每次重启都报同一件事。
        dbm.delete_setting(key)
        unit = str(rec.get("unit") or "")
        head = f"恢复 #{bid}（{rec.get('kind') or '?'} {rec.get('target') or '?'}）在面板运行中被中断"
        if not rec.get("stopped_by_panel") or not unit:
            lines.append(f"{head}：目标服务不是面板停的，未做处理；请核对目标内容是否完整")
            continue
        st = await systemd_ops.unit_state(unit)
        if st["missing"]:
            lines.append(f"{head}：本机没有 {unit} 这个 unit，未做处理")
        elif st["busy"]:
            lines.append(f"{head}：{unit} 现在在跑，但恢复可能只写了一半，请核对内容")
        elif st["state"] == "unknown":
            lines.append(f"{head}：查不到 {unit} 的状态（systemctl 没有回话），未擅自启动，请手工确认")
        else:
            if st["state"] == "failed":
                await systemd_ops.reset_failed(unit)
            r = await run("systemctl", ["start", "--", unit], timeout=60)
            lines.append(
                f"{head}：已重新拉起 {unit}，但恢复可能只写了一半，请核对内容"
                if r["code"] == 0
                else f"{head}：{unit} 启动失败，请手工 systemctl start {unit}：{(r['out'] or '').strip()[:160]}"
            )
        dbm.audit("system", "backup:restore-interrupted", f"{bid}:{unit or '-'}")
    return lines


async def restore_backup(bid: int, filename: str, actor: str = "system") -> dict:
    """用指定备份文件把目标覆盖回备份当时的状态（admin 手动触发，不可逆操作）。"""
    bk = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not bk:
        raise RuntimeError("备份任务不存在")
    path = _backup_file_path(bid, filename)
    if not _restoreable(bk["kind"], os.path.basename(path)):
        raise RuntimeError("该备份类型不支持自动恢复，请手工恢复")
    # 同一个目标不叠两次恢复，也不和部署/同步/回滚/删除叠在一起：锁按解析后的 app id 分，
    # 与 apps_service 用的是同一把（见 _restore_lock_key 与 app/locks.py）。两个 admin 各开
    # 一个页面各点一次「恢复」是很常见的现场，而两次恢复都先 stop 服务、再按各自的归档
    # 覆盖同一目录、再 start：后一次会把前一次的成果整份换掉，先返回的那句「恢复完成」
    # 当场变成假的。
    bk["_file"] = path
    key = _restore_lock_key(bk)
    _ensure_no_deployment_for_restore(bk)
    guard = locks.acquire(key, f"恢复 {os.path.basename(path)}", actor)
    try:
        _mark_restore(bk, actor)
        log: list[str] = [f"==> 恢复 {os.path.basename(path)}（kind={bk['kind']}, target={bk['target']}）"]
        try:
            if bk["kind"] == "sqlite":
                await _restore_sqlite(bk, path, log)
            elif bk["kind"] == "app":
                await _restore_app(bk, path, log)
            else:
                await _restore_pg(bk, path, log)
        except Exception as e:  # noqa: BLE001
            # 半途失败时 log 里那句「解包中断：目录可能处于半恢复状态，服务保持停止」是用户
            # 唯一能照着处理的现场说明。旧写法只把异常往上抛，这段说明连同整个 log 一起丢了，
            # 界面上一行 "恢复失败: …"，没人知道服务其实被恢复流程停掉了、目录是半新的。
            # 和成功分支一样先遮一次凭据再往上抛：这条消息会被路由原样写进审计、也原样回给
            # 浏览器（routers/backups.py 的 `失败: {e}`），而失败的那几步恰恰最容易把带连接串的
            # 命令行留在 log 里——成功分支遮了、失败分支不遮，泄露就只在出事的时候发生。
            raise RuntimeError(redact_creds(f"{e}\n\n执行记录：\n" + "\n".join(log))) from e
        # actor 由路由传当前登录用户：覆盖线上数据这种事记成 "system" 等于没记
        dbm.audit(actor, "backup:restore", f"{bid}:{os.path.basename(path)}")
        # 数据确实装上了，但日志里有 `!!` 行 = 有一件事要人接手（服务没起来、按最大库定位、
        # 归档环境变量生效）。这些行过去只藏在 log 里，而界面收到 ok:true 就一律报
        # 「恢复完成」——绿色成功提示正好把要处理的那行盖住。单独带出来给前端定调性。
        # 整段遮一次凭据：pg_restore/tar 的原文里带过连接串，而这段文字要进浏览器。
        masked = redact_creds("\n".join(log))
        return {"ok": True, "warn": [line for line in masked.splitlines() if line.startswith("!!")], "log": masked}
    finally:
        # 顺序要紧：先清标记再放锁。反过来会有一个窗口——锁已释放、标记还在，
        # 此时另一个进程抢到锁并开始恢复，而启动对账把上一次的标记当成它的现场。
        dbm.delete_setting(_mark_key(bid))
        guard.release()


async def run_backup_now(bid: int) -> dict:
    # 预算与 runner 内部同源（JOB_TIMEOUT_SEC）：runner 里的单条命令按 CMD_TIMEOUT_SEC
    # 先收，这层拿到的是一个干净的失败退出码，而不是把整个进程连它的清理一起 SIGKILL。
    r = await run(PYTHON_BIN, ["-m", "app.backup_runner", str(bid)],
                  cwd=BACKEND_DIR, timeout=backup_runner.JOB_TIMEOUT_SEC)
    return {"code": r["code"], "out": r["out"]}


async def sync_all_timers():
    for b in dbm.query("SELECT * FROM backups"):
        try:
            await _sync_timer(b)
        except Exception as e:  # noqa: BLE001
            warn(f"backup timer #{b['id']} sync failed: {e}")
