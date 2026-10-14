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

from . import config
from . import database as dbm
from .config import BACKUP_DIR
from .util import sh_escape

EXCLUDES = ["node_modules", ".venv", ".next", ".output"]
_SQLITE_EXTS = (".db", ".sqlite", ".sqlite3")
_SQLITE_SKIP = set(EXCLUDES) | {"__pycache__", ".git", "dist", "build", ".cache"}


def run_sh(cmd: str) -> int:
    """bash + pipefail 执行，返回退出码（不抛异常，由调用方判定严重程度）。"""
    return subprocess.run(["/bin/bash", "-o", "pipefail", "-c", cmd], stdout=subprocess.DEVNULL).returncode


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
    """秒级时间戳仍可能撞（同一秒内两次），撞了就补序号，绝不覆盖已有备份。"""
    base = f"{backup_dir}/bk{bk_id}-{stamp}{suffix}"
    path = base
    n = 1
    while os.path.exists(path):
        n += 1
        path = f"{backup_dir}/bk{bk_id}-{stamp}-{n}{suffix}"
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
        code = run_sh(dump_cmd(producer, file))
        if code != 0:
            _fail("PG 备份", code)
        dbm.audit("system", "backup:pg", bk["target"])
    elif bk["kind"] == "app":
        app = dbm.query_one("SELECT path FROM apps WHERE name=?", (bk["target"],))
        if not app:
            raise SystemExit(f"应用 {bk['target']} 不存在")
        file = _free_path(bk["id"], ts, ".tar.gz", BACKUP_DIR)
        excl = " ".join(f"--exclude={e}" for e in EXCLUDES)
        producer = f"tar -C {sh_escape(app['path'])} {excl} -cf - ."
        code = run_sh(dump_cmd(producer, file))
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


def _prune_order(bid: int) -> list:
    """本任务自动生成的产物，按"写入顺序从旧到新"排好的文件名列表。

    排序键是 (时间戳, 同秒序号, mtime, 名字)，不能直接按名字排：同一秒内跑两次备份时
    `_free_path` 给第二份补 `-2`，而 `…-03-41-00-2-app.sqlite.gz` 的字典序**在**
    `…-03-41-00-app.sqlite.gz` 之前（`2` < `a`）——按名字排会把刚写完的那一份当"最旧"
    删掉，目录里留下的反而是更早的一份，界面却报"备份完成"。线上探针实测到的正是这一支。
    无序号一定是先写的那一份（`_free_path` 只在基础名已存在时才补号），所以缺省记 0。
    末尾必须落在 `.gz`/`.tgz` 上：`sqlite_snapshot` 的中间库叫 `….sqlite.gz.tmp`，崩在半路
    时它会留在目录里，不认扩展名就等于让一个读不了的半成品占掉一个保留名额。
    """
    auto = re.compile(
        rf"^bk{bid}-(\d{{4}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}})(?:-(\d+))?.*(?:\.gz|\.tgz)$"
    )
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
    files = _prune_order(bid)
    while len(files) > int(bk["keep"]):
        f = files.pop(0)
        try:
            os.unlink(f"{BACKUP_DIR}/{f}")
            print(f"pruned {f}")
        except OSError:
            pass


if __name__ == "__main__":
    try:
        bid = int(sys.argv[1])
    except (IndexError, ValueError):
        print("usage: python3 -m app.backup_runner <backupId>", file=sys.stderr)
        sys.exit(2)
    row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
    if not row:
        print("backup not found", file=sys.stderr)
        sys.exit(2)
    try:
        main(row)
        print("backup ok")
    except subprocess.CalledProcessError as e:
        print(f"backup failed: exit {e.returncode}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"backup failed: {e}", file=sys.stderr)
        sys.exit(1)
