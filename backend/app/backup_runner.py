"""CLI 备份执行器：python3 -m app.backup_runner <backupId>（由 systemd timer 调用）。"""
import contextlib
import datetime
import gzip
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

from . import config, locks
from . import database as dbm
from .config import BACKUP_DIR
from .services.files_service import _estimate_archive_bytes, assert_write_budget, write_budget
from .util import kill_group, redact_creds, sh_escape, timeout_note

EXCLUDES = ["node_modules", ".venv", ".next", ".output"]
_SQLITE_EXTS = (".db", ".sqlite", ".sqlite3")
_SQLITE_SKIP = set(EXCLUDES) | {"__pycache__", ".git", "dist", "build", ".cache"}

# 一次备份任务的时间预算只有一个出处：runner 里的每条命令和面板 API 等这个子进程的方式
# 都从这里取。改这一行之前先看清这三层原来各是什么：
# - `run_sh` 原来压根没有超时（`subprocess.run(...)` 不带 timeout），一条卡死的 pg_dump
#   就永远以 root 身份挂着；
# - 面板 API 那条路写死 1800 秒，只有走网页"立即备份"时才有上层来掐；
# - systemd timer 是第三条入口（`ExecStart=… -m app.backup_runner <id>`），本机实测
#   `systemctl show panel-backup-1.service -p TimeoutStartUSec --value` 回的是
#   **infinity**——oneshot 没有起停超时。
# 于是夜间定时备份卡住时没有任何一层管得住：flock 是在进程死亡时才由内核释放的，
# 活着的那一条把它一直攥着，之后每次 timer 到点都打印一句 "backup skipped" 并返回 0，
# journal 与面板一片祥和，直到真要恢复那天才发现磁盘上最近一份归档是三周前的。
JOB_TIMEOUT_SEC = 1800
# 单条命令比整场任务先收，才轮得到我们自己删半成品、把原因写进 stderr（外部 SIGKILL
# 做不到这两件事）。让出来的那 120 秒是给 sqlite 快照、prune 与审计记账的。
CMD_TIMEOUT_SEC = JOB_TIMEOUT_SEC - 120

# 执行留痕（backups.last_note）的字数上界：存的是给人读的一句话，不是日志。
# 与 `_push_offsite` 里那条 note 同一个上界，两处一起长会让告警正文被自己的原因挤掉。
RUN_NOTE_MAX = 200


def run_sh(cmd: str, out_file: str | None = None, timeout: float = CMD_TIMEOUT_SEC) -> int:
    """bash + pipefail 执行，返回退出码（不抛异常，由调用方判定严重程度）。

    超时按**整个进程组**杀：`producer | gzip > f` 是 bash 再生出来的两个孩子，只 kill bash
    会留下 pg_dump 继续往一个已经没人认领的文件里写字，而 stdout 是 DEVNULL，那份写入
    从此无人过问。stderr 必须保持继承——pg_dump 的报错正文就是靠它进 journal 的，
    `_fail` 那句"详见 journalctl"指的就是这里。
    被 SIGKILL 时 `dump_cmd` 里那道 `|| { rm -f …; }` 没机会运行，半成品只能在这儿删：
    留着它，`list_files()` 会把一份截断的归档当"可恢复的备份"端上来，还要占掉 keep 名额
    里的一格（批次㊵同一条理由）。返回 124 与 util.run 同口径，调用方的 `code != 0`
    与 `code > 1` 分支因此照旧判失败。
    """
    proc = subprocess.Popen(["/bin/bash", "-o", "pipefail", "-c", cmd],
                            stdout=subprocess.DEVNULL, start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        print(f"backup: {timeout_note('备份命令', timeout)}", file=sys.stderr)
        if out_file:
            _cleanup(out_file)
            print(f"backup: 已删除未完成的产物 {out_file}", file=sys.stderr)
        return 124


def dump_cmd(producer: str, out_file: str) -> str:
    """`producer | gzip > f`，失败时删掉半成品但**保留原始退出码**。

    /bin/sh 在 Debian 系是 dash，只看管道最后一段的退出码：pg_dumpall 崩溃
    （磁盘满、权限、库正被删除）时 `pg_dumpall | gzip > f` 仍然返回 0，于是备份
    记为成功、文件却是只含 gzip 头的 20 字节空档 —— 等真要恢复时才发现里面没有东西。
    半成品必须删，否则它既占 keep 名额、又被当成可用备份。
    退出码要原样传出（tar 用 1 和 2 区分"部分文件变动"与"致命错误"），
    所以必须先 `rc=$?` 存好再 rm——`rm -f` 自己会把 $? 归零，写成 `exit $?` 就丢码了。
    """
    q = sh_escape(out_file)
    return f"{producer} | gzip > {q} || {{ rc=$?; rm -f {q}; exit $rc; }}"


def _fail(what: str, code: int):
    raise SystemExit(f"{what} 失败（退出码 {code}），详见 journalctl -u choyeon-panel")


def stamp() -> str:
    """备份文件名时间戳，精确到秒。

    原来只到分钟（%Y-%m-%d-%H-%M），实测同一分钟内跑两次（手动 run 撞上 timer 到点、
    或者连点两次"立即备份"）生成的是**同一个文件名**：后一份把前一份原地覆盖，
    目录里只剩一个文件，那个时间点之前的历史就此消失，而列表上看不出任何异常。
    scripts/common.sh 的 backup_data 早就是秒级（date +%Y%m%d-%H%M%S），这里对齐。
    没有任何代码反解这个时间戳（列表与 prune 只按 `bk<id>-` 前缀筛，doctor 只按后缀），
    所以补两位不影响既有行为。
    """
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d-%H-%M-%S")
    return ts.replace(":", "-")


def _free_path(bk_id, stamp: str, suffix: str, backup_dir: str) -> str:
    """秒级时间戳仍可能撞（同一秒内两次），撞了就补序号，绝不覆盖已有备份。

    占名必须是 O_CREAT|O_EXCL 的**原子创建**，不能 `os.path.exists()` 判断完再把名字
    返回：定时器和手动备份、或者两个恢复请求，会在同一瞬间都看到"这个名字还空着"，
    于是其中一个把另一个刚写好的归档原地覆盖，而两边都报告成功——目录里只剩一份，
    那个时间点的一份历史无声消失。存在性检查挡不住两个进程同时通过，只有创建本身
    原子才行。空文件就是本次的产物，写失败时由 `dump_cmd` 的 `rm -f` 与
    `sqlite_snapshot` 的清理分支各自删掉；留在原地也认得出形状，会随 keep 轮替淘汰。
    补号循环要有上界：`backup_dir` 里若已堆了一个同秒的目录（异常情形），无限递增
    会把调用方挂死，不如如实报错。
    """
    base = f"{backup_dir}/bk{bk_id}-{stamp}{suffix}"
    path = base
    n = 1
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            n += 1
            if n > 100:
                raise RuntimeError(f"同一秒内 {base} 的同名产物已排到 {n}，拒绝继续找名字") from None
            path = f"{backup_dir}/bk{bk_id}-{stamp}-{n}{suffix}"
            continue
        except OSError as e:
            raise RuntimeError(f"无法在备份目录里占名：{e}") from None
        os.close(fd)
        return path


def find_sqlite_dbs(app_path: str) -> list:
    """应用目录下的候选 SQLite 主库，按体积降序（排除 -wal/-shm/journal 与依赖目录）。

    只扫主库文件：WAL 与 SHM 是在途日志，单独备份它们恢复不出任何可用数据库。

    扫描有上界，超了是**报错**而不是返回半份结果：调用方靠"目录里有没有和备份同名的库"
    （旧格式靠"最大的那个库"）定位恢复目标，半份结果会把目标认错，把备份覆盖到另一个库上——
    那是数据丢失，不是慢一点。uploads/、media/ 这类目录不在排除名单里（它们确实可能装着
    业务库），几百万元素的树只能靠上界兜住。
    """
    found = []
    visited = 0
    deadline = time.monotonic() + config.SQLITE_SCAN_BUDGET_SEC
    for root, dirs, files in os.walk(app_path):
        dirs[:] = [d for d in dirs if d not in _SQLITE_SKIP and not d.startswith(".")]
        for f in files:
            visited += 1
            if visited > config.SQLITE_SCAN_MAX_ENTRIES or time.monotonic() > deadline:
                raise RuntimeError(
                    f"{app_path} 下文件过多或磁盘响应过慢：已扫 {visited} 个条目仍未走完"
                    f"（上限 {config.SQLITE_SCAN_MAX_ENTRIES} 个 / {config.SQLITE_SCAN_BUDGET_SEC} 秒）。"
                    "恢复需要在目录里按名字定位目标库，半份扫描结果会认错库，所以这里直接失败；"
                    "可清理依赖与资产目录，或调大 CP_SQLITE_SCAN_MAX_ENTRIES / CP_SQLITE_SCAN_BUDGET_SEC"
                )
            low = f.lower()
            if not low.endswith(_SQLITE_EXTS):
                continue
            if re.search(r"-(wal|shm|journal)$", low):
                continue
            p = os.path.join(root, f)
            try:
                found.append((os.path.getsize(p), p))
            except OSError:
                continue
    found.sort(reverse=True)
    return [p for _, p in found]


def _db_stem(db_path: str) -> str:
    """把源库文件名压成可安全放进备份文件名的标识，恢复时靠它定位该写回哪个库。

    只留时间戳是不够的：一个项目里可能有多个 .db（主库 + 测试库），
    不带源文件名的备份在恢复时只能靠猜，猜错就是拿旧数据覆盖另一个库。
    """
    stem = os.path.splitext(os.path.basename(db_path))[0]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)[:40]
    return stem or "db"


def sqlite_snapshot(src: str, dest: str) -> None:
    """SQLite 在线备份 API 写临时库，再 gzip 成最终文件。

    直接 cp 正在被服务写入的库会拿到页不一致的快照（恢复时报 database disk image is
    malformed），而 sqlite3.Connection.backup() 走的是页级一致性拷贝，还会把已提交在
    WAL 里的内容一并合并进目标，读锁很短、不会打断应用请求。
    失败时删掉半成品，避免它占 keep 名额又被当成可用备份。
    """
    tmp = f"{dest}.tmp"
    try:
        # 这份快照要写多少字节由**源库**决定，而不是由谁给的尺寸决定，所以判据还是
        # 落盘余量（`write_budget`，出处是设置页的磁盘警戒线）。上界取 2× 源库：中间库
        # 是未压缩整份副本（≈ 源库），gz 产物在数据本来就压不动时≈ 中间库，而中间库要
        # 等 gz 写完才删——峰值是两份同时在盘上。
        # 它放在 try 里面而不是外面：调用方可能已经用 `_free_path` 占了名（0 字节的 dest
        # 就在盘上了），被拒时得走下面那条清理分支，别留一个长得像备份的空文件占 keep 名额。
        assert_write_budget(os.path.dirname(dest), os.path.getsize(src) * 2, "给这份 SQLite 快照留的位置")
        src_conn = sqlite3.connect(src, timeout=15)
        try:
            dst_conn = sqlite3.connect(tmp)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        with open(tmp, "rb") as f, gzip.open(dest, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1024 * 1024)
    except BaseException:
        for p in (tmp, dest):
            if os.path.exists(p):
                with contextlib.suppress(OSError):
                    os.unlink(p)
        raise
    # 成功也要删掉中间库：它是未压缩的整库副本，留在备份目录里白占一倍空间，
    # 而且在备份文件页只会显示成一个"仅可下载"的读不了的文件。
    with contextlib.suppress(OSError):
        os.unlink(tmp)


def main(bk: dict):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = stamp()
    if bk["kind"] == "pg":
        if bk["target"] == "all":
            file = _free_path(bk["id"], ts, ".sql.gz", BACKUP_DIR)
            producer = "su -s /bin/sh postgres -c 'pg_dumpall'"
        else:
            # fullmatch：`$` 在 re.match 下允许尾部一个换行过关，"appdb\n" 会走到下一行
            # 直接被拼进 `sh -c 'pg_dump -Fc appdb\n'`——换行在 shell 里断行，等价于往
            # 那条 root→postgres 的命令串里多塞一个参数/一条命令。
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", bk["target"]):
                raise SystemExit("库名不合法")
            file = _free_path(bk["id"], ts, ".dump.gz", BACKUP_DIR)
            producer = f"su -s /bin/sh postgres -c 'pg_dump -Fc {bk['target']}'"
        # pg_dump/pg_dumpall 非 0 即数据不完整，必须判失败
        code = run_sh(dump_cmd(producer, file), file)
        if code != 0:
            _fail("PG 备份", code)
        dbm.audit("system", "backup:pg", bk["target"])
    elif bk["kind"] == "app":
        app = dbm.query_one("SELECT path FROM apps WHERE name=?", (bk["target"],))
        if not app:
            raise SystemExit(f"应用 {bk['target']} 不存在")
        if not os.path.isdir(app["path"]):
            # 先说清目录不在：否则下面 tar 的退出码 2 只能报"应用备份失败（退出码 2）"，
            # 而尺寸预检在这种情形会数出 0 字节、毫无阻拦地放行。
            raise SystemExit(f"应用目录不存在：{app['path']}")
        # 归档要写多少字节由目录内容决定，判据还是落盘余量（keep 份旧归档本来就在这块盘上，
        # statvfs 早已把它们算成"已用"，所以这里不用再自己扣一遍）。排除名单与 tar 传同一份：
        # node_modules 不进归档，把它们算进尺寸会把正当备份挡在门外。
        # 顺序要紧：这句必须在 `_free_path` 之前。`_free_path` 是原子占名，会留下一个
        # 0 字节的 `bk<id>-<时间戳>.tar.gz`——它长得像一份备份、会被 prune 认成 keep 名额
        # 里的一份，而这次其实一个字节都没写。
        need = _estimate_archive_bytes([app["path"]], write_budget(BACKUP_DIR)["room"], skip=tuple(EXCLUDES))
        assert_write_budget(BACKUP_DIR, need, f"给应用 {bk['target']} 的备份留的位置")
        file = _free_path(bk["id"], ts, ".tar.gz", BACKUP_DIR)
        excl = " ".join(f"--exclude={e}" for e in EXCLUDES)
        producer = f"tar -C {sh_escape(app['path'])} {excl} -cf - ."
        code = run_sh(dump_cmd(producer, file), file)
        if code > 1:
            # tar 的 1 只是"读取期间文件有变动/权限不足跳过部分文件"，归档仍可用；
            # 2 及以上才是致命错误。这里若不区分，活跃应用目录的备份会被全部误删。
            _fail("应用备份", code)
        elif code == 1:
            print("app backup: tar 报告部分文件变动，归档已保留")
        dbm.audit("system", "backup:app", bk["target"])
    elif bk["kind"] == "sqlite":
        app = _sqlite_target_app(bk["target"])
        cands = find_sqlite_dbs(app["path"])
        if not cands:
            raise SystemExit(f"应用 {app['name']} 目录下没有找到 SQLite 数据库文件（*.db / *.sqlite*）")
        src = cands[0]
        if len(cands) > 1:
            print(f"sqlite: 发现 {len(cands)} 个候选库，备份体积最大的 {src}；其余：" + ", ".join(cands[1:4]))
        file = _free_path(bk["id"], ts, f"-{_db_stem(src)}.sqlite.gz", BACKUP_DIR)
        try:
            sqlite_snapshot(src, file)
        except sqlite3.Error as e:
            raise SystemExit(f"SQLite 备份失败：{e}") from e
        size = os.path.getsize(file) if os.path.exists(file) else 0
        if size <= 50:
            # gzip 空文件约 20 字节；小于 50 说明快照没写进任何页（库文件被移走、只读到头部）
            _cleanup(file)
            raise SystemExit(f"SQLite 备份产出异常（{size} 字节），已删除空档")
        print(f"sqlite snapshot: {src} -> {os.path.basename(file)} ({size} bytes)")
        dbm.audit("system", "backup:sqlite", f"{app['name']}:{os.path.basename(src)}")
    else:
        raise SystemExit(f"未知备份类型 {bk['kind']}")
    prune(bk)


def _sqlite_target_app(target):
    """sqlite 任务的 target 存 app_id（字符串）。

    用 id 而不是应用名：应用改名后 name 型 target 会指向不存在的目录，
    而 id 是稳定外键；同时兼容历史写法（传应用名）避免线上已有任务直接失读。
    """
    t = str(target or "")
    app = None
    if t.isdigit():
        app = dbm.query_one("SELECT id,name,path FROM apps WHERE id=?", (int(t),))
    if not app:
        app = dbm.query_one("SELECT id,name,path FROM apps WHERE name=?", (t,))
    if not app:
        raise SystemExit(f"备份目标应用 {t} 不存在")
    if not os.path.isdir(app["path"]):
        raise SystemExit(f"应用目录不存在：{app['path']}")
    return app


def _cleanup(path: str):
    if os.path.exists(path):
        with contextlib.suppress(OSError):
            os.unlink(path)


def auto_pattern(bid: int) -> str:
    r"""本任务自动生成的产物名的正则（字符串形式，两个读者共用）。

    远端 prune（`services/cloud_backup.py`）要在桶里认出同一批对象，判据必须和这里
    逐字相同；各写一份的话，带同秒序号的那一份（`bk1-…-03-41-00-2-…`）会在本机被认、
    在异地不被认，于是远端越留越多而界面写着"按 keep 轮替"。写成函数返回模式串，
    是为了让 sqlite 的键、本机目录、远端前缀三处都从同一行规则派生。
    """
    return rf"^bk{bid}-(\d{{4}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}})(?:-(\d+))?.*(?:\.gz|\.tgz|\.env)$"


def auto_files(bid: int) -> list:
    """本任务自动生成的产物，按"写入顺序从旧到新"排好的文件名列表。

    排序键是 (时间戳, 同秒序号, mtime, 名字)，不能直接按名字排：同一秒内跑两次备份时
    `_free_path` 给第二份补 `-2`，而 `…-03-41-00-2-app.sqlite.gz` 的字典序**在**
    `…-03-41-00-app.sqlite.gz` 之前（`2` < `a`）——按名字排会把刚写完的那一份当"最旧"
    删掉，目录里留下的反而是更早的一份，界面却报"备份完成"。线上探针实测到的正是这一支。
    无序号一定是先写的那一份（`_free_path` 只在基础名已存在时才补号），所以缺省记 0。
    末尾必须落在 `.gz`/`.tgz`/`.env` 上：`sqlite_snapshot` 的中间库叫 `….sqlite.gz.tmp`，崩在半路
    时它会留在目录里，不认扩展名就等于让一个读不了的半成品占掉一个保留名额。
    `.env` 也要认：`_restore_app` 把应用当前的 `.panel.env` 暂存成
    `bk<id>-<时间戳>-pre-restore.env`（含口令的副本，写在 root 0700 的备份目录里），
    正常路径在 finally 里回写后就地删除，但进程被 OOM/升级杀在半程时它就留下了——
    旧写法只认 `.gz`/`.tgz`，于是每次崩掉的恢复都在备份目录里存一份永不轮替的凭据副本，
    既占磁盘又扩大口令的可读面。认进形状里，它就和真备份一起按 keep 淘汰。
    """
    auto = re.compile(auto_pattern(bid))
    held = []
    for name in os.listdir(BACKUP_DIR):
        m = auto.match(name)
        if not m:
            continue
        try:
            mtime = os.stat(f"{BACKUP_DIR}/{name}").st_mtime
        except OSError:
            mtime = 0.0
        held.append(((m.group(1), int(m.group(2) or 0), mtime, name), name))
    held.sort()
    return [name for _, name in held]


def prune(bk: dict):
    """按 keep 名额删这个任务**自动生成**的最旧备份。

    筛选必须认时间戳形状，不能只认 `bk<id>-` 前缀。旧写法把前缀下的所有文件一起
    排序，人工上传的产物（`bk<id>-manual-xxx`，见 backup_service.save_upload）也在其中，
    而 'manual' 的 'm' 排在时间戳的 '2' 之后 = "最新"：keep=1 时刚写完的真备份会被当成
    "最旧那一份"当场删掉，界面报「备份完成」而目录里永远只剩那个上传文件。
    同理，一次崩溃留下的 `.restoring-*` 之类非时间戳名也不该占掉保留名额。
    上传的产物由用户在备份文件页自己删。
    """
    bid = int(bk["id"])
    files = auto_files(bid)
    while len(files) > int(bk["keep"]):
        f = files.pop(0)
        try:
            os.unlink(f"{BACKUP_DIR}/{f}")
            print(f"pruned {f}")
        except OSError:
            pass


def _push_offsite(bk: dict) -> None:
    """本机产物落盘后把该推的几份推到异地；推不动只报警，不把这次备份改判成失败。

    为什么这一步在 `main()` 之后：本机已经有一份可用归档了，异地没上去是"少了一份副本"，
    而不是"备份没做"——把它算成失败会让 timer 的 journal、备份页、自检三处一起报一条
    和本机数据无关的错，而真正该盯的是 `cloud_targets.last_error`。
    为什么不在 `main()` 之前：先推后写，对端半途失败会在桶里留下一个本地没有的对象，
    远端的 prune 认它、恢复的人找不到它。
    """
    if not bk.get("cloud_target_id"):
        return
    from .services import cloud_backup  # 函数级导入：cloud_backup 反过来要认这里的名正则

    try:
        res = cloud_backup.push_pending(bk)
    except Exception as e:  # noqa: BLE001 - 异地失败不影响本机结论，但必须留下三处痕迹
        note = redact_creds(str(e))[:200]
        print(f"offsite: 推送失败：{note}", file=sys.stderr)
        dbm.audit("system", "backup:push-fail", f"{bk['id']}({note})")
        return
    if res.get("pushed"):
        print(f"offsite: 已推送 {len(res['pushed'])} 份：" + "、".join(p["key"] for p in res["pushed"]))
        if res.get("pruned"):
            print(f"offsite: 按 keep 删掉远端最旧的 {len(res['pruned'])} 份")
        if res.get("prune_skipped"):
            # 副本没减是"远端还在长"，不是"剪枝做完了"：这句要进 stderr 也要进审计，
            # 否则管理员只在网页上看到"已推送 N 份"，桶涨了几年都不知道为什么。
            print(f"offsite: 远端没有剪枝：{res['prune_skipped']}", file=sys.stderr)
            dbm.audit("system", "backup:push-prune-skipped", f"{bk['id']}({res['prune_skipped']})")
        dbm.audit("system", "backup:push", f"{bk['id']}({len(res['pushed'])} 份)")
    elif res.get("skipped"):
        print(f"offsite: {res['skipped']}")


def _record_run(bid: int, status: str, note: str = "") -> None:
    """把这一次执行写回 backups（last_run / last_status / last_note）。

    为什么必须由 runner 来写：timer 的 `ExecStart=` 起的是独立进程，没有 API 调用方，
    也没有人替它记账。库里没有这一行时，一条每晚都 SystemExit 的任务在面板上永远显示
    "已启用"，只有翻 journalctl 才发现早就不产出了（批次㉔同一条理由：判词光有数不够，
    还得点得出是谁没成）。
    时间格式与 cron_service.run_now 同一条 `strftime`：两者都要和 `_local_ts` 读回的
    systemd 时间戳比先后，格式不一致时前端 fmtDateTime 就得分两种来源解析。
    """
    try:
        dbm.execute(
            "UPDATE backups SET last_run=?, last_status=?, last_note=? WHERE id=?",
            (time.strftime("%Y-%m-%d %H:%M:%S"), status,
             redact_creds(str(note))[:RUN_NOTE_MAX], bid),
        )
    except Exception as e:  # noqa: BLE001 - 留痕写不进去不能把一次成功的备份改判成失败
        print(f"backup: 执行留痕写入失败：{type(e).__name__}: {e}", file=sys.stderr)


def run_job(bid: int) -> int:
    """进程入口：按备份任务抢一把跨进程锁，再跑 `main()`。返回退出码。

    锁为什么在这一层而不是 `main()` 里面：timer 的 `ExecStart=`、界面上的"立即备份"
    （`backup_service.run_backup_now`）和 `choyeonctl backup run` 三路都是
    `python -m app.backup_runner <id>` 起独立进程，只共用这一个入口；而 `main()` 还要被
    测试和将来的进程内调用直接调，不该平白带上文件锁副作用。
    没有这把锁时，"手动备份"撞上"定时器到点"是两个进程在同一个 `bk<id>-<同一秒>`
    名字上各写一份：`_free_path` 现在会补号，产物不至于互相覆盖，但两份 tar 同时读同一个
    正在被部署改写的目录，多出来的那一份既占 keep 名额又没人知道它从哪来。

    抢不到锁返回 0 并打印跳过原因，不返回失败：同一个任务同时跑两次时，放弃后到的那一次
    是**正确行为**（前一份马上就要落盘），把它报成失败会让 timer 的 journal 和 unit 状态
    长期挂着一条假故障，而真正该失败的（pg_dump 崩了、磁盘满）就被淹没了。
    同理，这一条路径**不写**执行留痕：跳过不是这一轮的结论，写了会把上一次真失败盖成
    "刚刚也失败过"，也会把上一次的成功盖成失败。

    SystemExit 单独接一条：`_fail()` 与各处校（库名、应用目录）抛的就是它，而它是
    BaseException，`except Exception` 接不住——以前它一路穿出 `__main__`，进程照样退 1、
    原因照样进 journal，只是留痕会漏掉最常态的那一类失败。
    """
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not row:
        print("backup not found", file=sys.stderr)
        return 2
    try:
        guard = locks.acquire(locks.key_backup(bid), f"备份任务 {bid}", "system")
    except RuntimeError as e:
        print(f"backup skipped: {e}")
        return 0
    code = 0
    try:
        main(row)
        # 本机归档已经落盘了才推异地：`_push_offsite` 自己吞掉推送异常，
        # 所以这一步不会把一次成功的备份改判成失败（理由见它的 docstring）。
        _push_offsite(row)
        print("backup ok")
        _record_run(bid, "ok")
    except subprocess.CalledProcessError as e:
        print(f"backup failed: exit {e.returncode}", file=sys.stderr)
        _record_run(bid, f"exit {e.returncode}", str(e))
        code = 1
    except SystemExit as e:
        reason = e.code if isinstance(e.code, str) else "备份进程提前退出"
        print(f"backup failed: {reason}", file=sys.stderr)
        _record_run(bid, "fail", reason)
        code = e.code if isinstance(e.code, int) and e.code else 1
    except Exception as e:  # noqa: BLE001
        print(f"backup failed: {e}", file=sys.stderr)
        _record_run(bid, "fail", str(e))
        code = 1
    finally:
        guard.release()
    return code


if __name__ == "__main__":
    try:
        _bid = int(sys.argv[1])
    except (IndexError, ValueError):
        print("usage: python3 -m app.backup_runner <backupId>", file=sys.stderr)
        sys.exit(2)
    sys.exit(run_job(_bid))
