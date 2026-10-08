"""CLI 备份执行器：python3 -m app.backup_runner <backupId>（由 systemd timer 调用）。"""
import datetime
import os
import re
import subprocess
import sys

from . import database as dbm
from .config import BACKUP_DIR
from .util import sh_escape

EXCLUDES = ["node_modules", ".venv", ".next", ".output"]


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


def _stamp() -> str:
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


def main(bk: dict):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _stamp()
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
    prune(bk)


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
