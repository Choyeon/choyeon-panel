import os
import secrets
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path

from . import config
from .util import redact_creds

os.makedirs(config.DATA_DIR, exist_ok=True)
# 数据目录里放着 panel.db（users.pass_hash、settings.jwt_secret）和头像。默认权限是
# 0755：同机任何一个普通用户都能把库整个读走——离线爆破 scrypt 哈希是一回事，
# 直接拿 jwt_secret 签一枚 admin token 是另一回事，后者等于这台风控面板替攻击者
# 把 root 终端递过去。目录收到 0700，db 文件收到 0600（WAL/SHM 靠目录权限一起挡住）。
# chmod 失败不拦启动：开发机上 DATA_DIR 可能是别人建的目录，此时照常服务，
# 由 doctor 的权限项如实报出来，而不是让面板起不来。
with suppress(OSError):
    os.chmod(config.DATA_DIR, 0o700)

_lock = threading.RLock()
conn = sqlite3.connect(f"{config.DATA_DIR}/panel.db", check_same_thread=False, timeout=15)
with suppress(OSError):
    os.chmod(f"{config.DATA_DIR}/panel.db", 0o600)
conn.row_factory = sqlite3.Row
# WAL + busy_timeout：面板进程与 backup_runner（独立进程）可安全并发读写
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA synchronous = NORMAL")
conn.execute("PRAGMA busy_timeout = 15000")
conn.execute("PRAGMA foreign_keys = ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pass_hash TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS apps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('node','python')),
  repo_url TEXT NOT NULL,
  branch TEXT NOT NULL DEFAULT 'main',
  path TEXT NOT NULL,
  port INTEGER,
  domain TEXT,
  install_cmd TEXT,
  start_cmd TEXT NOT NULL,
  env TEXT NOT NULL DEFAULT '[]',
  unit_override TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS deployments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  log TEXT NOT NULL DEFAULT '',
  started_at TEXT DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS backups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('pg','app','sqlite')),
  target TEXT NOT NULL,
  schedule TEXT NOT NULL DEFAULT 'daily',
  hour INTEGER NOT NULL DEFAULT 3,
  minute INTEGER NOT NULL DEFAULT 30,
  keep INTEGER NOT NULL DEFAULT 7,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
-- 告警历史：run_checks 每轮都把结论写进来（含恢复 ok），面板重启后不再失忆。
CREATE TABLE IF NOT EXISTS alerts_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL CHECK(level IN ('ok','warn','crit')),
  code TEXT NOT NULL,
  message TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '',
  acked INTEGER NOT NULL DEFAULT 0
);
-- 定时任务中心：表达式只存 cron 五段，渲染成 systemd timer 由 cron_service 负责。
CREATE TABLE IF NOT EXISTS cron_task(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  expr TEXT NOT NULL,
  argv TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run TEXT,
  last_status TEXT NOT NULL DEFAULT '',
  log_tail TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
-- 指标历史：ts 是 unix 秒；gran=raw 为 30 秒采样（留 24h），hour 为小时降采样（留 7d）。
CREATE TABLE IF NOT EXISTS metrics(
  ts INTEGER NOT NULL,
  gran TEXT NOT NULL DEFAULT 'raw',
  cpu REAL, mem_used INTEGER, mem_total INTEGER,
  net_rx INTEGER, net_tx INTEGER, disk_used INTEGER, disk_total INTEGER,
  load1 REAL, io_r INTEGER, io_w INTEGER, procs INTEGER,
  PRIMARY KEY(gran, ts)
);
"""

MAX_DEPLOY_LOG = 256 * 1024  # 单条部署日志上限，防止失控增长


def _columns(table: str) -> list[str]:
    return [r["name"] for r in query(f"PRAGMA table_info({table})")]


def migrate() -> None:
    with _lock:
        conn.executescript(SCHEMA)
        cols = _columns("users")
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        if "nickname" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN nickname TEXT NOT NULL DEFAULT ''")
        if "avatar" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT NOT NULL DEFAULT ''")
        # 两步验证的密钥。空串＝没开；不另设 enabled 位，因为那位一旦和密钥不同步就会
        # 出现"显示已开启但登录不索要口令"或反之，判据只能有一个。
        if "totp_secret" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT NOT NULL DEFAULT ''")
        acols = _columns("apps")
        if "db_names" not in acols:
            conn.execute("ALTER TABLE apps ADD COLUMN db_names TEXT")
        if "unit_template" not in acols:
            conn.execute("ALTER TABLE apps ADD COLUMN unit_template TEXT")
        if "parent_id" not in acols:
            conn.execute("ALTER TABLE apps ADD COLUMN parent_id INTEGER")
        if "component_role" not in acols:
            conn.execute("ALTER TABLE apps ADD COLUMN component_role TEXT NOT NULL DEFAULT ''")
        dcols = _columns("deployments")
        if "commit_sha" not in dcols:
            conn.execute("ALTER TABLE deployments ADD COLUMN commit_sha TEXT")
        # SQLite 备份任务：kind 的 CHECK 约束无法 ALTER，而 `CREATE TABLE IF NOT EXISTS`
        # 对已存在的旧表是空操作——旧库会一直只认 pg/app，新建 sqlite 任务时抛
        # IntegrityError（面板路由兜成 400，用户看到的是"参数不合法"而不是"该版本支持"）。
        # 所以按现有 migrate 风格重建表结构迁移，列完全一致、数据原样搬过去。
        bt = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='backups'").fetchone()
        if bt and bt["sql"] and "sqlite" not in bt["sql"]:
            conn.executescript(
                """
                BEGIN;
                CREATE TABLE backups_v2(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind TEXT NOT NULL CHECK(kind IN ('pg','app','sqlite')),
                  target TEXT NOT NULL,
                  schedule TEXT NOT NULL DEFAULT 'daily',
                  hour INTEGER NOT NULL DEFAULT 3,
                  minute INTEGER NOT NULL DEFAULT 30,
                  keep INTEGER NOT NULL DEFAULT 7,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO backups_v2(id,kind,target,schedule,hour,minute,keep,enabled,created_at)
                  SELECT id,kind,target,schedule,hour,minute,keep,enabled,created_at FROM backups;
                DROP TABLE backups;
                ALTER TABLE backups_v2 RENAME TO backups;
                COMMIT;
                """
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deployments_app ON deployments(app_id)")
        # audit.id 是 INTEGER PRIMARY KEY，本身就是 rowid：`ORDER BY id DESC LIMIT n` 走的是
        # B-tree 反向遍历，再建一个 (id) 索引只会拖慢写入。全仓没有按 created_at 过滤审计的
        # 查询（保留策略 database.py 也是按 id 删），所以这里只留 (username, id) 这个真被用的。
        conn.execute("DROP INDEX IF EXISTS idx_audit_created")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit(username, id)")
        # deployments 的 running 查询是应用列表每 15 秒都要做的，用**部分索引**只装 running
        # 那几行，比全量 (app_id,status) 小一个数量级。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deployments_running ON deployments(app_id) WHERE status='running'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_backups_enabled ON backups(enabled, id)")
        # 告警历史只按时间倒序翻页，metrics 只按 (gran, ts) 区间取窗口——两个都是覆盖查询的入口。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts_log(acked, ts)")
        conn.commit()


def query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params=()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()):
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def create_first_admin(username: str, pw_hash: str) -> str:
    """setup 唯一的写库入口：在同一把锁里确认「users 表为空」并落库。

    返回 ok / not-empty / name-taken。拆成"路由先查、查完再插"时，两个未登录的并发
    请求会同时通过检查，于是两个人都拿到 200 和两枚 admin token——第一个装完机的
    管理员永远不知道自己不是唯一的管理员。setup 是全仓唯一免认证的写入口，
    这个窗口不需要抢，脚本化地重试几十次就能命中。

    判据是"整张表为空"而不是"没有 admin"：管理员被 CLI 删光而机器上还留着 viewer 时，
    按 admin 计数等于把"第一个点到 /api/auth/setup 的人成为 root"重新对公网打开——
    那不需要猜任何东西，限速也拦不住。这种残局的正解是登服务器用
    `choyeonctl user create`（doctor 的 fix 早就这么写），不是让公网先抢到算谁的。
    """
    with _lock:
        n = query_one("SELECT COUNT(*) c FROM users")
        if n and n["c"] > 0:
            return "not-empty"
        try:
            conn.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)", (username, pw_hash, "admin"))
        except sqlite3.IntegrityError:
            # 表是空的却撞了 UNIQUE：只可能是 CLI 在另一个进程里同时建号（WAL 共享同一份库）。
            return "name-taken"
        conn.commit()
        return "ok"


def totp_state(username: str) -> str:
    """读一个账号的两步验证密钥；账号不存在返回空串。"""
    row = query_one("SELECT totp_secret FROM users WHERE username=?", (str(username or ""),))
    return str((row or {}).get("totp_secret") or "")


def set_totp(username: str, secret: str) -> int:
    """写入（或清空）两步验证密钥的唯一入口，返回真正被改动的行数。

    列名只出现在这里：路由开启/关闭、CLI 逃生口、登录索要口令三处都要动它，抄三次 SQL
    就会有第四处漏掉。传空串表示关闭——`totp_secret=''` 就是"没开"的唯一判据，没有第二个
    enabled 位可写。

    不走 `execute()`：它返回的是 `lastrowid`，UPDATE 之后那个数与"改了几行"无关，
    而这里要拿 rowcount 判"账号还在不在"。
    """
    with _lock:
        cur = conn.execute(
            "UPDATE users SET totp_secret=? WHERE username=?",
            (str(secret or ""), str(username or "")),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def _read_setting(key: str) -> str | None:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row and row["value"] is not None else None


def set_setting(key: str, value) -> None:
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "" if value is None else str(value)),
    )
    with _lock:
        _auth_cache.pop(key, None)


# 删不得的键：token_epoch[:<用户>] 是 token 吊销墓碑，删掉它同名重建的用户会让上一个
# 人的旧 token 原地复活（见 tests/test_hardening.py::TestDeletedUserNameReuse）；
# jwt_secret 是签名密钥，删了全员掉线。
_UNDELETABLE_KEYS = ("jwt_secret", "token_epoch")


def delete_setting(key: str) -> None:
    """删除一个 settings 键；清理按名字编码的残留标记只能走这里。

    为什么要包一层而不是让调用方自己写 SQL：对象删掉之后，它按**名字**编的标记键
    （`sent:probe-<名>` / `streak:probe:<名>` / `notified:probe:<名>` / `probe_expect:<名>`）
    就挂在再也没有人消费的地方，而同名新建的对象会直接继承旧状态——实测删掉应用后重建
    同名应用，`sent:` 仍是今天，于是新应用当天第一次无响应一条通知都发不出去。该清。
    但同一句"扫 settings 清孤儿键"顺手把 `token_epoch:<用户>` 也清掉就是权限复活事故，
    所以墓碑在这个函数里硬挡，而不是靠写代码的人记住。
    """
    k = str(key or "")
    if k in _UNDELETABLE_KEYS or k.startswith("token_epoch:"):
        raise ValueError(f"禁止删除该 settings 键：{k}")
    # 不需要失效 _auth_cache：进缓存的只有 _AUTH_KEYS 与 token_epoch:*，而这两类都在上面被拒了。
    execute("DELETE FROM settings WHERE key=?", (k,))


# 认证路径（每个 /api 请求都要验 token）读的这几个 key 走进程内缓存。
# 以前每请求两次带全局锁的 SELECT，仪表盘 3 秒轮询把它放大成常驻开销。
# 缓存必须带 TTL，不能只靠 set_setting 精确失效：`choyeonctl user reset/delete`
# 是**另一个进程**在写 settings 表，它 pop 不到本进程的字典。没有 TTL 的话，
# CLI 吊销的旧 token 会一直有效到服务重启（实测删号后仍返回 200）。
# 缓存与 DB 共用同一把可重入锁——两把锁会因 jwt_secret（先缓存锁后 DB 锁）与
# set_setting（先 DB 锁后缓存锁）反序而死锁。
_AUTH_KEYS = ("jwt_secret", "token_epoch")
_AUTH_TTL = 3.0
# 值为 (内容, 写入时刻)。时刻用 monotonic：系统时钟回拨不会让缓存提前过期，
# NTP 校时也不会把吊销窗口拉长。
_auth_cache: dict[str, tuple[str | None, float]] = {}


def _auth_key(key: str) -> bool:
    return key in _AUTH_KEYS or key.startswith("token_epoch:")


def get_setting(key: str) -> str | None:
    if not _auth_key(key):
        return _read_setting(key)
    now = time.monotonic()
    with _lock:
        hit = _auth_cache.get(key)
        if hit is not None and now - hit[1] < _AUTH_TTL:
            return hit[0]
        val = _read_setting(key)
        _auth_cache[key] = (val, now)
        return val


def jwt_secret() -> str:
    # 生成也在锁内：并发首次调用若各自生成再各自写库，先写那个签发的 token
    # 会被后写入的密钥判为无效，用户表现为「刚登录就掉线」。
    with _lock:
        s = get_setting("jwt_secret")
        if not s:
            # 缓存里的 None 可能只是 TTL 内的一次空读（读失败/竞态），库里其实有真值：
            # 直接再生成会覆盖旧密钥、把所有已签发 token 一起作废，所以先绕过缓存直读。
            s = _read_setting("jwt_secret")
            if s:
                _auth_cache["jwt_secret"] = (s, time.monotonic())  # 顺手把被污染的 None 刷掉
        if not s:
            s = secrets.token_hex(32)
            set_setting("jwt_secret", s)
            # 写库后必须同步刷缓存：否则缓存残留的 None 活满 _AUTH_TTL，这 3 秒内
            # 每次读取都当空处理——实测表现为刚签发的 token 当场验签失败，且
            # 全量测试红绿随机器速度漂（前序用例的真实读取落在缓存窗口内就侥幸绿）。
            _auth_cache["jwt_secret"] = (s, time.monotonic())
        return s


def new_epoch() -> str:
    """单调且唯一的版本号。用 time_ns + 随机后缀，避免同一次调用内多次 bump 产生相同值。"""
    return f"{time.time_ns():x}.{secrets.token_hex(4)}"


def token_epoch() -> str:
    """密码版本号：改密 / 删除用户后自增，使已签发 token 立即失效。"""
    v = get_setting("token_epoch")
    if not v:
        v = new_epoch()
        set_setting("token_epoch", v)
    return v


def bump_token_epoch() -> str:
    v = new_epoch()
    set_setting("token_epoch", v)
    return v


def audit(username, action: str, detail=None) -> None:
    # 三个字段都要截断：action/detail 有一部分调用方直接把请求体拼进去
    # （如 f"file:{body['action']}"），一条 1MB 的输入就能把审计表撑大、
    # 并把 /api/audit 页面变成自伤式 DoS。username 同理来自 token 声明。
    #
    # 同时在这一处统一脱敏：detail 里相当多的写法是 f"...(失败: {e})"，而异常原文
    # 常带子进程回显的 `PGPASSWORD=…`、certbot 输出、连接串。审计表虽然只有 admin
    # 能读，但它是**长期留存**，且整份进备份与导出；指望每个调用点都记得遮，
    # ⑤C/⑤O 已经实测漏掉三个出口，收在唯一写入口才是收口。
    if detail is not None:
        detail = redact_creds(str(detail))[:500]
    execute(
        "INSERT INTO audit(username,action,detail) VALUES(?,?,?)",
        (str(username or "")[:64], redact_creds(str(action or ""))[:64], detail),
    )


def append_deploy_log(dep_id: int, text: str) -> None:
    """追加部署日志并裁剪到上限，避免大输出撑爆数据库。"""
    with _lock:
        row = conn.execute("SELECT log FROM deployments WHERE id=?", (dep_id,)).fetchone()
        current = row["log"] if row else ""
        merged = f"{current}\n{text}\n"
        if len(merged) > MAX_DEPLOY_LOG:
            merged = "...(已截断)...\n" + merged[-MAX_DEPLOY_LOG:]
        conn.execute("UPDATE deployments SET log=? WHERE id=?", (merged, dep_id))
        conn.commit()


def prune_audit(keep: int | None = None) -> int:
    """保留最近 N 条审计记录，返回清理条数。"""
    keep = keep or config.AUDIT_KEEP
    with _lock:
        cur = conn.execute(
            "DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        conn.commit()
        return cur.rowcount or 0


def prune_deployments(keep_per_app: int = 20) -> int:
    # 除了"每个应用只留最近 N 条"，还要清掉**应用已不存在**的记录：
    # 部署列表与详情都按 app_id 过滤，这些行没有任何入口能读到，但每行都带完整部署日志，
    # 而按 app_id 分组的保留策略会永远给孤儿留着位置（实测线上残留 10 条）。
    with _lock:
        cur = conn.execute(
            "DELETE FROM deployments WHERE app_id NOT IN (SELECT id FROM apps) OR id NOT IN ("
            "  SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY id DESC) rn FROM deployments)"
            "  WHERE rn <= ?)",
            (keep_per_app,),
        )
        conn.commit()
        return cur.rowcount or 0


def prune_alerts(days: int = 30, keep: int = 2000) -> int:
    """告警历史按时间过期，但未确认的告警一律留着（清了就等于把待办藏起来）。"""
    with _lock:
        cur = conn.execute(
            "DELETE FROM alerts_log WHERE acked=0 AND id NOT IN ("
            "  SELECT id FROM alerts_log WHERE acked=0 ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        cur2 = conn.execute(
            "DELETE FROM alerts_log WHERE acked=1 AND ts < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        conn.commit()
        return (cur.rowcount or 0) + (cur2.rowcount or 0)


def prune_metrics(raw_seconds: int = 86400, hour_seconds: int = 7 * 86400) -> int:
    with _lock:
        now = int(time.time())
        a = conn.execute("DELETE FROM metrics WHERE gran='raw' AND ts < ?", (now - raw_seconds,))
        b = conn.execute("DELETE FROM metrics WHERE gran='hour' AND ts < ?", (now - hour_seconds,))
        conn.commit()
        return (a.rowcount or 0) + (b.rowcount or 0)


def db_path() -> str:
    return str(Path(config.DATA_DIR) / "panel.db")


migrate()
