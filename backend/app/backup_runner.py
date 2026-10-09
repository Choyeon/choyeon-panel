"""CLI 备份执行器：python3 -m app.backup_runner <backupId>（由 systemd timer 调用）。"""
import datetime
import gzip
import os
import re
import shutil
import sqlite3
import subprocess
import sys

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
    """
    found = []
    for root, dirs, files in os.walk(app_path):
        dirs[:] = [d for d in dirs if d not in _SQLITE_SKIP and not d.startswith(".")]
        for f in files:
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
                try:
                    os.unlink(p)
                except OSError:
                    pass
        raise
    # 成功也要删掉中间库：它是未压缩的整库副本，留在备份目录里既白占空间，
    # 又会按文件名排序挤占 keep 名额，把真正可用的一份备份裁掉。
    try:
        os.unlink(tmp)
    except OSError:
        pass


def main(bk: dict):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = stamp()
    if bk["kind"] == "pg":
        if bk["target"] == "all":
            file = _free_path(bk["id"], ts, ".sql.gz", BACKUP_DIR)
            producer = "su -s /bin/sh postgres -c 'pg_dumpall'"
        else:
            if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", bk["target"]):
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
        try:
            os.unlink(path)
        except OSError:
            pass


def prune(bk: dict):
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"bk{bk['id']}-"))
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
