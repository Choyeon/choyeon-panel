import asyncio
import json
import os
import re
import time

from .. import database as dbm
from ..util import kill_group, run
from . import redis_client

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def is_ident(s: str) -> bool:
    return bool(IDENT_RE.match(s or ""))


def _quote_pass(p: str) -> str:
    if not re.match(r"^[\x20-\x7e]{1,128}$", p or ""):
        raise RuntimeError("密码只允许可见 ASCII 字符且不超过 128 位")
    return "'" + p.replace("'", "''") + "'"


PSQL_BASE = "psql -X -A -t -v ON_ERROR_STOP=1"
# standard_conforming_strings=on 必须由服务端保证：关掉时反斜杠参与转义，
# \' 就能提前结束字符串，_sql_lit 的双写单引号挡不住。走 PGOPTIONS 而不是
# 在脚本里 `SET ...;`——psql 会把这条 utility 语句的命令标签 "SET" 打到 stdout，
# 混进 RETURNING 的结果里，实测让 affected 多算 1 行、让"没匹配到行"变成假成功。
PGOPTS = "PGOPTIONS='-c standard_conforming_strings=on' "


async def psql(sql: str, db: str | None = None, quiet: bool = False) -> str:
    """执行 SQL，返回 stdout。db 必须由调用方先过 is_ident 校验。

    ON_ERROR_STOP=1 是这里的关键：psql 默认对每条语句独立报错但整体退出码仍是 0，
    实测 `SELECT 1; SELECT * FROM nope; SELECT 2` 在不加该参数时
    返回码 0、stdout 只有 "1\\n2"、ERROR 只在 stderr——面板因此把
    "drop_role 目标不存在""SQL 控制台中途报错"一律显示为成功（假成功）。
    加上之后出错语句之后的内容不再执行，退出码 3，错误能真正上抛。

    quiet=True 只给写入路径用：`-t` 不抑制命令标签，实测
    `UPDATE ... RETURNING 1` 未命中时 stdout 是 "UPDATE 0"，按行数统计影响条数
    会得出 1，把"没改到任何行"报成成功。加 `-q` 后只剩 RETURNING 的真实行。
    """
    base = PGOPTS + PSQL_BASE + (" -q" if quiet else "") + (f" -d {db}" if db else "")
    proc = await asyncio.create_subprocess_exec(
        "su", "-s", "/bin/sh", "postgres", "-c", base,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # 与 util.run 一致：独立进程组，超时时能连 psql/postgres 后端一起收掉
        start_new_session=True,
    )

    async def _write_stdin():
        proc.stdin.write(sql.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    async def _drain(stream, limit: int) -> bytes:
        chunks = []
        size = 0
        while True:
            part = await stream.read(64 * 1024)
            if not part:
                break
            size += len(part)
            if size > limit:
                kill_group(proc)
                raise RuntimeError("输出过大（>512KB），请加 LIMIT")
            chunks.append(part)
        return b"".join(chunks)

    async def _collect():
        # 三个方向必须并发：先写完再读，遇到「SQL 很长 + 输出撑满 64KB 管道」时
        # 双方互等，只会以一句没头没尾的「psql 超时」收场（stderr 同理）。
        _, out, err = await asyncio.gather(
            _write_stdin(), _drain(proc.stdout, 512 * 1024), proc.stderr.read()
        )
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(_collect(), timeout=30)
    except TimeoutError:
        kill_group(proc)
        # from None：超时原因已经明确，不需要把 TimeoutError 的上下文再叠加上去
        raise RuntimeError("psql 超时") from None
    if proc.returncode == 0:
        return out.decode(errors="replace").strip()
    text = err.decode(errors="replace").strip()
    if proc.returncode == 3:
        # ON_ERROR_STOP 触发：把出错那条语句说清楚，别只甩一段 SQLSTATE
        raise RuntimeError(text.split("\n")[0] or "SQL 执行出错，后续语句已中止")
    raise RuntimeError(text or f"psql exit {proc.returncode}")


async def psql_json(sql: str, db: str | None = None) -> list:
    """sql 必须是 `SELECT json_agg(...)::text`，返回解析后的数组。"""
    out = await psql(sql, db)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"无法解析数据库返回内容：{e}") from None
    return data if isinstance(data, list) else []


def _json_agg(inner: str, order: str) -> str:
    """把内层查询包成"单行 JSON 数组"，空结果集也返回 '[]'。

    ORDER BY 写在 json_agg(...) 里而不是内层子查询上：子查询的 ORDER BY
    不保证传递到聚合的输入顺序，显式排序才是可靠写法。
    """
    return f"SELECT coalesce(json_agg(t ORDER BY {order}), '[]'::json)::text FROM ({inner}) t"


async def list_dbs() -> list:
    rows = await psql_json(_json_agg(
        "SELECT d.datname AS name, pg_get_userbyid(d.datdba) AS owner,\n"
        "        pg_size_pretty(pg_database_size(d.datname)) AS size,\n"
        "        (SELECT count(*) FROM pg_stat_activity a WHERE a.datname=d.datname) AS conns\n"
        "   FROM pg_database d WHERE d.datistemplate=false AND d.datallowconn=true",
        "t.name",
    ))
    return [
        {"name": r.get("name"), "owner": r.get("owner"), "size": r.get("size"),
         "conns": r.get("conns") or 0}
        for r in rows if isinstance(r, dict)
    ]


async def list_roles() -> list:
    rows = await psql_json(_json_agg(
        "SELECT rolname AS name, rolsuper AS super, rolcanlogin AS login,\n"
        "        rolcreaterole AS createrole, rolcreatedb AS createdb\n"
        "   FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%'",
        "t.name",
    ))
    return [
        {"name": r.get("name"), "super": bool(r.get("super")), "login": bool(r.get("login")),
         "createrole": bool(r.get("createrole")), "createdb": bool(r.get("createdb"))}
        for r in rows if isinstance(r, dict)
    ]



async def create_db(name: str, owner: str | None = None):
    if not is_ident(name):
        raise RuntimeError("数据库名只允许小写字母/数字/下划线")
    if owner and not is_ident(owner):
        raise RuntimeError("owner 名不合法")
    await psql(f"CREATE DATABASE {name}" + (f" OWNER {owner}" if owner else "") + ";")


async def drop_db(name: str):
    if not is_ident(name):
        raise RuntimeError("数据库名不合法")
    if name in ("postgres", "template0"):
        raise RuntimeError("禁止删除该库")
    await psql(f"DROP DATABASE {name};")


async def create_role(name: str, password: str):
    if not is_ident(name):
        raise RuntimeError("用户名不合法")
    await psql(f"CREATE ROLE {name} LOGIN PASSWORD {_quote_pass(password)};")


async def set_role_password(name: str, password: str):
    if not is_ident(name):
        raise RuntimeError("用户名不合法")
    await psql(f"ALTER ROLE {name} LOGIN PASSWORD {_quote_pass(password)};")


async def drop_role(name: str):
    if not is_ident(name):
        raise RuntimeError("用户名不合法")
    if name == "postgres":
        raise RuntimeError("禁止删除 postgres")
    await psql(f"DROP OWNED BY {name}; DROP ROLE {name};")


async def grant(name: str, db: str):
    if not is_ident(name) or not is_ident(db):
        raise RuntimeError("参数不合法")
    await psql(f"GRANT ALL PRIVILEGES ON DATABASE {db} TO {name};")


async def query_sql(sql: str, db: str | None = None) -> str:
    if not sql.strip():
        raise RuntimeError("空查询")
    return await psql(sql, db)


# redis_password() 在每条 Redis 命令的热路径上（key 浏览器翻一页就发好几条），旧实现
# 每次都做一遍「全局锁 SELECT + 读整份 /etc/redis/redis.conf」。改成带 TTL 的进程内缓存：
# 面板里的写入只有 set_redis_password 一处，它精确失效缓存；配置文件是运维手工改的，
# 没人通知进程，所以靠 TTL 兜陈旧，而不是永久缓存。
_REDIS_PW_TTL = 60.0
# 哨兵区分「没查过」和「查过、确实没配口令」：后者也是一次有效读取，不缓存就每条
# 命令都回到盘上，而"没设口令"恰恰是最常见的状态。
_PW_UNSET = object()
_redis_pw_cache: tuple[float, object] = (0.0, _PW_UNSET)


def _read_redis_password():
    p = dbm.get_setting("redis_password")
    if p:
        return p
    if os.path.exists("/etc/redis/redis.conf"):
        try:
            with open("/etc/redis/redis.conf", encoding="utf8", errors="replace") as fh:
                m = re.search(r"^requirepass\s+(\S+)", fh.read(), re.M)
            if m:
                return m.group(1)
        except OSError:
            pass
    return None


def redis_password():
    global _redis_pw_cache
    ts, cached = _redis_pw_cache
    if cached is not _PW_UNSET and time.monotonic() - ts < _REDIS_PW_TTL:
        return cached
    p = _read_redis_password()
    _redis_pw_cache = (time.monotonic(), p)
    return p


def forget_redis_password() -> None:
    global _redis_pw_cache
    _redis_pw_cache = (0.0, _PW_UNSET)


async def redis_info() -> dict:
    """Redis 概览（INFO + DBSIZE）。

    优先用进程内 RESP 客户端：一次往返拿两条命令，不再为打开 Redis 标签页 fork 两个
    redis-cli。只有 TCP 建不起来（Redis 只监听 unix socket、强制 TLS，或干脆没起）才
    退回子进程路线——那条路能给出 redis-cli 自己的失败正文，对用户更有用。
    """
    pass_ = redis_password()
    try:
        reps = await redis_client.pipeline([["INFO"], ["DBSIZE"]], **_conn_kw())
    except redis_client.RedisConnectionError:
        return await _redis_info_cli(pass_)
    except redis_client.RedisError as e:
        raise RuntimeError(str(e)) from None
    info_rep, size_rep = reps[0], reps[1]
    if isinstance(info_rep, redis_client.RedisError):
        raise RuntimeError(_redis_auth_text(info_rep, pass_))
    info: dict[str, str] = {}
    # INFO 的回包是 bulk string（bytes），这里必须过 _text：直接 str() 会得到
    # "b'# Server\\r\\nredis_version...'"，整段变成一行，除了第一个字段全解析不出来。
    for line in _text(info_rep).replace("\r\n", "\n").split("\n"):
        i = line.find(":")
        if i > 0 and not line.startswith("#"):
            info[line[:i]] = line[i + 1:].strip()
    # DBSIZE 出错时返回 None 而不是错误正文：前端是 {{ redis.dbsize ?? '—' }}，
    # 任何非空值都会被当成"Key 总数"原样显示，等于把报错渲染成一个数字。
    dbsize = None if isinstance(size_rep, redis_client.RedisError) else size_rep
    return {"info": info, "dbsize": dbsize, "authed": bool(pass_)}


def _redis_auth_text(rep, pass_: str | None) -> str:
    """INFO 的服务端错误回复 → 能看的中文；错误正文为空时兜底，不能让前端弹空框。"""
    text = str(rep)
    if text.startswith("NOAUTH") or "authentication" in text.lower():
        return "Redis 需要密码，请在下方设置"
    return text or "Redis 返回了错误（INFO 失败）"


async def _redis_info_cli(pass_: str | None) -> dict:
    """redis-cli 子进程路线，仅作 RESP 连不上 TCP 时的回退。口令只进环境变量。"""
    env = dict(os.environ)
    if pass_:
        env["REDISCLI_AUTH"] = pass_
    r = await run("redis-cli", ["INFO"], env=env)
    if r["code"] != 0 or r["out"].startswith("NOAUTH"):
        if r["out"].startswith("NOAUTH"):
            raise RuntimeError("Redis 需要密码，请在下方设置")
        # redis-cli 不存在时 util.run 返回 code=127，正文是 Python 的 FileNotFoundError 文本；
        # 连接被拒时正文里有 "Connection refused"。两者都不能空着抛——空字符串会让前端
        # 显示一个没有任何原因的红色错误框。
        raise RuntimeError(r["out"] or "无法执行 redis-cli（未安装或 Redis 未运行）")
    info: dict[str, str] = {}
    for line in r["out"].split("\n"):
        i = line.find(":")
        if i > 0 and not line.startswith("#"):
            info[line[:i]] = line[i + 1:].strip()
    keys = await run("redis-cli", ["DBSIZE"], env=env)
    return {"info": info, "dbsize": keys["out"] if keys["code"] == 0 else None,
            "authed": bool(pass_)}


def set_redis_password(p: str):
    # 空值要能真的清空：旧写法 `if p:` 让"填错口令后清掉重填"做不到——
    # 接口返回 ok、前端提示已保存，但 redis_password() 仍返回旧值，
    # 面板从此卡在 NOAUTH，唯一出路是手工改数据库。
    p = (p or "").strip()
    if p and not re.match(r"^\S{1,512}$", p):
        raise RuntimeError("Redis 口令不能包含空白字符，且不超过 512 位")
    dbm.set_setting("redis_password", p)
    # 缓存不失效就是"保存成功但仍卡在 NOAUTH 60 秒"，正是下面注释里那条老 bug 的翻版。
    forget_redis_password()
    return {"ok": True}


# ---------- 网页数据浏览（数据库页的表/行浏览器与 Redis key 浏览器） ----------

async def list_tables(db: str) -> list:
    if not is_ident(db):
        raise RuntimeError("库名不合法")
    return await psql_json(_json_agg(
        "SELECT c.relname::text AS name, n.nspname::text AS schema,\n"
        "        coalesce(s.n_live_tup,0)::bigint AS row_estimate,\n"
        "        pg_size_pretty(pg_total_relation_size(c.oid)) AS size\n"
        "   FROM pg_class c\n"
        "   JOIN pg_namespace n ON n.oid=c.relnamespace\n"
        "   LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid\n"
        "  WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema')",
        "t.schema, t.name",
    ), db=db)


CTID_RE = re.compile(r"^\(\d+,\d+\)$")
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
MAX_CELL_CHARS = 4 * 1024 * 1024


def _q(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _sql_lit(v) -> str:
    """前端标量 → SQL 字面量。

    只有两种形态：None → NULL，其余 → 单引号字面量（' 翻倍）。写入语句前会
    `SET standard_conforming_strings = on`，反斜杠因此不参与转义，这是本函数
    安全的前提（否则 \' 就能提前结束字面量）。Postgres 把未知类型的字面量按
    目标列类型隐式转换，所以 int/bool/jsonb/timestamp/数组都走同一条路径。
    """
    if v is None:
        return "NULL"
    s = v if isinstance(v, str) else str(v)
    if "\x00" in s:
        raise RuntimeError("值中包含空字符（NUL），Postgres 不允许写入")
    if len(s) > MAX_CELL_CHARS:
        raise RuntimeError(f"单个值超过 {MAX_CELL_CHARS // 1048576}MB 上限")
    return "'" + s.replace("'", "''") + "'"


def _count_affected(out: str) -> int:
    return len([l for l in (out or "").split("\n") if l != ""])


def _row_predicate(key, pk: list) -> str:
    """把前端回传的行标识变成 WHERE 条件。

    优先主键；没有主键的表退到 ctid（table_data 每行都带 _ctid）。ctid 在
    VACUUM FULL / 分区表上会失效，所以只作为兜底，且严格校验格式。
    """
    if not isinstance(key, dict) or not key:
        raise RuntimeError("缺少行定位条件")
    if "_ctid" in key:
        c = str(key["_ctid"])
        if not CTID_RE.match(c):
            raise RuntimeError("ctid 格式不合法")
        return f"ctid = '{c}'::tid"
    if not pk:
        raise RuntimeError("该表没有主键，请改用 SQL 控制台")
    if sorted(str(k) for k in key) != sorted(pk):
        raise RuntimeError("定位条件必须给出完整主键列")
    parts = []
    for c in pk:
        v = key[c]
        parts.append(f"{_q(c)} IS NULL" if v is None else f"{_q(c)} = {_sql_lit(v)}")
    return " AND ".join(parts)


def _clean_values(values, allowed: dict) -> dict:
    if not isinstance(values, dict) or not values:
        raise RuntimeError("没有要提交的字段")
    if len(values) > 200:
        raise RuntimeError("一次提交的字段过多（上限 200）")
    out: dict = {}
    for k, v in values.items():
        if k not in allowed:
            raise RuntimeError(f"未知列：{str(k)[:64]}")
        out[str(k)] = None if v is None else (v if isinstance(v, str) else str(v))
    return out


async def table_meta(db: str, schema: str, table: str) -> dict:
    """列元数据 + 主键。既给前端渲染只读详情/编辑表单，也是写入时的列白名单。"""
    for v, what in ((db, "数据库"), (schema, "schema"), (table, "表")):
        if not is_ident(v):
            raise RuntimeError(f"{what}名不合法（仅支持小写字母/数字/下划线）")
    cols = await psql_json(_json_agg(
        "SELECT ordinal_position::int AS pos, column_name::text AS name,\n"
        "        data_type::text AS type, udt_name::text AS udt,\n"
        "        is_nullable='YES' AS nullable, coalesce(column_default,'') AS \"default\",\n"
        "        (coalesce(column_default,'') LIKE 'nextval%'\n"
        "          OR coalesce(is_identity,'NO')='YES'\n"
        "          OR coalesce(identity_generation,'')<>'') AS auto\n"
        f"   FROM information_schema.columns\n"
        f"  WHERE table_schema='{schema}' AND table_name='{table}'",
        "t.pos",
    ), db=db)
    pk = await psql_json(_json_agg(
        "SELECT a.attname::text AS name, k.ord::int AS ord\n"
        "   FROM pg_index i,\n"
        "        unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord),\n"
        "        pg_attribute a\n"
        f"  WHERE i.indrelid = '{_q(schema)}.{_q(table)}'::regclass AND i.indisprimary\n"
        "    AND a.attrelid = i.indrelid AND a.attnum = k.attnum",
        "t.ord",
    ), db=db)
    return {
        "columns": [c for c in cols if isinstance(c, dict)],
        "pk": [p.get("name") for p in pk if isinstance(p, dict) and p.get("name")],
    }


async def write_row(db: str, schema: str, table: str, action: str, key, values) -> dict:
    """按主键（或 ctid）更新/删除单行，或插入单行。

    列名一律先过 table_meta 白名单再双引号包裹，值只走 _sql_lit 字面量，
    标识符本身由 is_ident 把关——三条路都封住才敢让浏览器直接写业务库。
    """
    if schema in SYSTEM_SCHEMAS or table.startswith("pg_"):
        raise RuntimeError("禁止直接修改系统表")
    meta = await table_meta(db, schema, table)
    allowed = {c["name"]: c for c in meta["columns"] if c.get("name")}
    if not allowed:
        raise RuntimeError("读不到这张表的列信息")
    quoted = f"{_q(schema)}.{_q(table)}"

    if action == "insert":
        items = _clean_values(values, allowed)
        col_sql = ", ".join(_q(k) for k in items)
        val_sql = ", ".join(_sql_lit(v) for v in items.values())
        ret = ", ".join(f"{_q(c)}::text" for c in meta["pk"]) or "ctid::text"
        out = await psql(f"INSERT INTO {quoted} ({col_sql}) VALUES ({val_sql}) RETURNING {ret};", db, quiet=True)
        first = (out or "").split("\n")[0]
        got = first.split("|") if first else []
        newpk = dict(zip(meta["pk"], got)) if meta["pk"] and len(got) == len(meta["pk"]) else {}
        return {"affected": 1, "pk": newpk}

    pred = _row_predicate(key, meta["pk"])
    if action == "update":
        sets = ", ".join(f"{_q(k)} = {_sql_lit(v)}" for k, v in _clean_values(values, allowed).items())
        n = _count_affected(await psql(f"UPDATE {quoted} SET {sets} WHERE {pred} RETURNING 1;", db, quiet=True))
        if n == 0:
            raise RuntimeError("没有匹配到该行（内容可能已被改动），请刷新后重试")
        return {"affected": n}
    if action == "delete":
        n = _count_affected(await psql(f"DELETE FROM {quoted} WHERE {pred} RETURNING 1;", db, quiet=True))
        if n == 0:
            raise RuntimeError("没有匹配到该行，可能已被删除")
        return {"affected": n}
    raise RuntimeError("未知操作")


_ROW_COUNT_TTL = 15.0
_row_counts: dict = {}


async def _table_count(db: str, schema: str, table: str, quoted: str) -> int:
    """count(*) 按表缓存 15 秒：翻页时总数不变，没必要每页重新全表扫描。

    缓存只影响翻页显示的总数，误差上限就是一个刷新周期；
    写入路径（insert/update/delete）会让前端重新拉第一页，最多多看 15 秒的旧总数。
    """
    key = (db, schema, table)
    hit = _row_counts.get(key)
    now = time.time()
    if hit and now - hit[0] < _ROW_COUNT_TTL:
        return hit[1]
    total = int((await psql(f"SELECT count(*)::text FROM {quoted}", db)) or 0)
    _row_counts[key] = (now, total)
    return total


async def table_data(db: str, schema: str, table: str, page: int, size: int) -> dict:
    meta = await table_meta(db, schema, table)
    names = [c["name"] for c in meta["columns"] if c.get("name")]
    if not names:
        raise RuntimeError("读不到这张表的列信息")
    size = max(1, min(size, 200))
    page = max(1, page)
    quoted = f'"{schema}"."{table}"'
    total = await _table_count(db, schema, table, quoted)
    # _ctid 随行返回：无主键的表靠它定位要改哪一行，前端只把它当标识、不渲染成列。
    # 有主键时显式 ORDER BY：不带排序的 LIMIT/OFFSET 翻页可能重复或漏行。
    order = f" ORDER BY {', '.join(_q(c) for c in meta['pk'])}" if meta["pk"] else ""
    rows_raw = await psql(
        "SELECT coalesce(json_agg(row_to_json(t))::text,'[]'::text) FROM ("
        f"SELECT ctid::text AS _ctid, * FROM {quoted}{order} LIMIT {size} OFFSET {(page - 1) * size}) t",
        db,
    )
    return {
        "columns": names,
        "cols": meta["columns"],
        "pk": meta["pk"],
        "rows": json.loads(rows_raw or "[]"),
        "total": total,
        "page": page,
        "size": size,
    }


# redis-cli 的错误回应（raw 模式）没有统一前缀，只能按 Redis 的错误 token 认。
_RERR_RE = re.compile(
    r"^(?:\(\w+\)\s*)?(?:ERR\b|WRONGTYPE\b|NOAUTH\b|WRONGPASS\b|NOPERM\b|MOVED\b|ASK\b"
    r"|CLUSTERDOWN\b|BADDB\b|BADFORMAT\b|WRONGNUMTYPE\b|INVALIDOBJ\b|BUSYKEY\b|NOSCRIPT\b"
    r"|MASTERDOWN\b|LOADING\b|READONLY\b|DENYS\b|OOM\b)"
)


async def _rcli(args: list) -> str:
    env = dict(os.environ)
    p = redis_password()
    if p:
        env["REDISCLI_AUTH"] = p
    r = await run("redis-cli", ["--no-auth-warning"] + args, env=env, timeout=20)
    out = r["out"] or ""
    if r["code"] != 0:
        raise RuntimeError(out or f"redis-cli 执行失败（code {r['code']}，未安装或 Redis 未运行）")
    if _RERR_RE.match(out):
        # redis-cli 出错时退出码仍是 0（实测 WRONGTYPE/NOAUTH 都返回 0），
        # 只能按错误回应的首 token 判；不判就会把 "(error)" 文本当数据返回给上层，
        # 于是 HSET 失败被当成成功、错误正文被当成 key 的值显示出来。
        raise RuntimeError(out)
    return out


def _lines(out: str) -> list:
    return [l for l in out.split("\n") if l != ""]


async def _rcli_exact(args: list) -> str:
    """与 _rcli 相同的调用，但保留 stdout 原样（只去掉 redis-cli 自己补的结尾换行）。

    util.run 会 strip() 输出：编辑 string key 的表单若用 strip 过的值回填，
    用户点一次「保存」就把值末尾的换行静默吃掉了——读来改数据必须用原始字节。
    """
    env = dict(os.environ)
    p = redis_password()
    if p:
        env["REDISCLI_AUTH"] = p
    proc = await asyncio.create_subprocess_exec(
        "redis-cli", "--no-auth-warning", *args, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except TimeoutError:
        kill_group(proc)
        raise RuntimeError("redis-cli 超时") from None
    text = out.decode(errors="replace")
    if proc.returncode or text.startswith(("NOAUTH", "WRONGPASS", "(error)")):
        raise RuntimeError(text.strip() or "redis-cli 执行失败（未安装或 Redis 未运行）")
    return text[:-1] if text.endswith("\n") else text


def _check_key(k: str) -> str:
    k = str(k or "")
    # 上限要 ≥ 列表侧能返回的 key 长度，否则 SCAN 把长 key 列出来了，
    # 点进去却固定报"key 不合法"，那些 key 在页面上永远不可查看。
    # 2048 是详情走 query string（URL 编码后约 6KB）还能安全过 nginx 的量级。
    if not k or len(k) > 2048:
        raise RuntimeError("key 不合法")
    return k


# ---------- RESP 客户端接入：一次往返拿全，redis-cli 只作显式回退 ----------

# 与不带 -h/-p/-n 的 redis-cli 默认目标一致。现有部署没有第二处连接配置可读，
# 就不新增配置项；口令沿用 redis_password()（面板设置优先，其次 /etc/redis/redis.conf）。
REDIS_HOST = redis_client.DEFAULT_HOST
REDIS_PORT = redis_client.DEFAULT_PORT
REDIS_DB = redis_client.DEFAULT_DB


def _conn_kw() -> dict:
    return {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": redis_password()}


def _text(v) -> str:
    """RESP 回复 → 旧 redis-cli 文本形态（非 UTF-8 用替换符，与 _rcli_exact 的解码一致）。"""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def _shape(v, kind: str):
    """客户端回复 → kind 指定的形态；错误回复原样返回，由调用方决定抛还是忽略。"""
    if isinstance(v, redis_client.RedisError):
        return v
    if kind == "int":
        # 旧实现对非数字文本（"(nil)"、异常正文）一律给 None，这里保持同样的宽容度：
        # 详情页的 TTL/内存是可选显示项，取值失败不该让整页报错。
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    if kind == "text":
        return _text(v)
    if kind == "lines":
        return [_text(x) for x in (v or [])]
    if kind == "scan":
        head = v[0] if isinstance(v, (list, tuple)) and v else "0"
        members = v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else []
        return _text(head), [_text(x) for x in (members or [])]
    if kind == "scanlist":
        # HSCAN/SSCAN 的回包是 [cursor, [成员...]]，上层只关心成员那一段。
        members = v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else []
        return [_text(x) for x in (members or [])]
    return v


async def _r_cli(cmd: list, kind: str):
    """redis-cli 子进程路线：把 stdout 文本还原成与客户端路径同样的形态。"""
    if kind == "exact":
        return await _rcli_exact(cmd)
    out = await _rcli(cmd)
    if kind == "int":
        t = out.strip()
        if t in ("", "(nil)"):
            return None
        return int(t) if t.lstrip("-").isdigit() else None
    if kind == "lines":
        return _lines(out)
    if kind == "scan":
        lines = out.split("\n")
        return (lines[0].strip() if lines else "0"), _lines("\n".join(lines[1:]))
    if kind == "scanlist":
        return _lines(out)[1:]
    return out


async def _rp(cmds: list, kinds: list | None = None) -> list:
    """一条流水线执行多条命令（一次往返）；套接字建不起来时逐条退回 redis-cli 子进程。

    kinds 只服务回退路径——子进程只有文本输出，需要知道该还原成什么形态。
    服务端错误正文原样抛 RuntimeError（旧 _rcli 的行为）：写入路径必须抛，
    否则 HSET 失败会被当成成功、错误正文会被当成数据返回给上层。
    """
    if not cmds:
        return []
    kinds = list(kinds or ["value"] * len(cmds))
    if len(kinds) != len(cmds):
        raise RuntimeError("Redis 命令数量与回复形态数量不一致")
    # 参数必须是标量、每条命令必须是参数列表：把整条命令当成一个参数传进来时，
    # redis-cli 回退路径会把它拼成一条看着像空操作的怪命令（实测 SCAN 因此静默返回
    # 空列表），客户端路径则抛 TypeError——两条路都会把"读不到数据"伪装成成功。
    for cmd in cmds:
        if not isinstance(cmd, (list, tuple)):
            raise RuntimeError("Redis 命令必须是参数列表")
        for a in cmd:
            if isinstance(a, (list, tuple, dict)):
                raise RuntimeError("Redis 命令参数不能是列表/字典")
    try:
        reps = await redis_client.pipeline([list(c) for c in cmds], **_conn_kw())
    except redis_client.RedisError as e:
        # 连得上、被拒绝（NOAUTH/WRONGPASS）：这不是"回退 redis-cli 能解决"的故障，
        # 回退同样会卡在同一个口令上，直接把服务端原文给用户。
        raise RuntimeError(str(e)) from None
    except redis_client.RedisConnectionError:
        return [await _r_cli([str(a) for a in cmd], kind) for cmd, kind in zip(cmds, kinds)]
    for rep in reps:
        if isinstance(rep, redis_client.RedisError):
            raise RuntimeError(str(rep))
    return [_shape(rep, kind) for rep, kind in zip(reps, kinds)]


async def _r(cmd: list, kind: str = "value"):
    """单条命令版的 _rp（错误照抛），给写入与扫描这类一步就够的调用用。"""
    return (await _rp([list(cmd)], [kind]))[0]


# key 详情的一条流水线：前 3 条是页面头部信息，中间 5 条算长度（判断 truncated），
# 后 5 条取值。取值命令全部有界——GETRANGE 只读 64KB、*_RANGE/HSCAN/SSCAN 只读 200 条，
# 所以对着百万元素的 key 也不会像 GET/SMEMBERS 那样把整个值搬回面板。
# 类型不匹配的探测命令返回 WRONGTYPE，正好用同一次往返把 key 的真实类型判出来。
def _detail_commands(k: str) -> tuple[list, list]:
    cmds = [
        ["TYPE", k], ["TTL", k], ["MEMORY", "USAGE", k],
        ["STRLEN", k], ["HLEN", k], ["LLEN", k], ["SCARD", k], ["ZCARD", k],
        ["GETRANGE", k, "0", "65535"], ["HSCAN", k, "0", "COUNT", "200"],
        ["LRANGE", k, "0", "199"], ["SSCAN", k, "0", "COUNT", "200"],
        ["ZRANGE", k, "0", "199", "WITHSCORES"],
    ]
    kinds = ["text", "int", "int",
             "int", "int", "int", "int", "int",
             "exact", "scanlist", "lines", "scanlist", "lines"]
    return cmds, kinds


async def redis_scan(cursor: str = "0", pattern: str = "*", count: int = 200) -> dict:
    c = cursor if str(cursor).isdigit() else "0"
    n = max(10, min(int(count) if str(count).isdigit() else 200, 1000))
    cur, keys = await _r(["SCAN", str(c), "MATCH", (pattern or "*")[:200], "COUNT", str(n)],
                         kind="scan")
    return {"cursor": cur if cur.isdigit() else "0", "keys": [k for k in keys if k][:n]}


def _pairs(flat: list) -> list:
    return [{"field": flat[i], "value": flat[i + 1] if i + 1 < len(flat) else ""} for i in range(0, len(flat) - len(flat) % 2, 2)]


async def _redis_key_cli(k: str) -> dict:
    """redis-cli 子进程路线的 key 详情：连不上 TCP（只开 unix socket、强制 TLS、
    bind 到别的地址）时用，行为与引入客户端之前逐字一致——包括按类型只发必要命令、
    string 走 _rcli_exact 保住值末尾的换行。"""
    ktype = (await _rcli(["TYPE", k])).strip()
    ttl = (await _rcli(["TTL", k])).strip()
    mem = (await _rcli(["MEMORY", "USAGE", k])).strip()
    truncated = False
    if ktype == "string":
        slen = int(await _rcli(["STRLEN", k]) or 0)
        truncated = slen > 65536
        data: object = await _rcli(["GETRANGE", k, "0", "65535"]) if truncated else await _rcli_exact(["GET", k])
    elif ktype == "hash":
        n = int(await _rcli(["HLEN", k]) or 0)
        truncated = n > 200
        if truncated:
            data = _pairs(_lines(await _rcli(["HSCAN", k, "0", "COUNT", "200"]))[1:])
        else:
            data = _pairs(_lines(await _rcli(["HGETALL", k])))
    elif ktype == "list":
        n = int(await _rcli(["LLEN", k]) or 0)
        truncated = n > 200
        data = _lines(await _rcli(["LRANGE", k, "0", "199"]))
    elif ktype == "set":
        n = int(await _rcli(["SCARD", k]) or 0)
        truncated = n > 200
        data = _lines(await _rcli(["SSCAN", k, "0", "COUNT", "200"]))[1:] if truncated else _lines(await _rcli(["SMEMBERS", k]))
    elif ktype == "zset":
        n = int(await _rcli(["ZCARD", k]) or 0)
        truncated = n > 200
        data = _pairs(_lines(await _rcli(["ZRANGE", k, "0", "199", "WITHSCORES"])))
    else:
        data = None
    return {
        "key": k, "type": ktype,
        "ttl": int(ttl) if ttl.lstrip("-").isdigit() else None,
        "mem": int(mem) if mem.isdigit() else None,
        "value": data, "truncated": truncated,
    }


async def redis_key(k: str) -> dict:
    k = _check_key(k)
    cmds, kinds = _detail_commands(k)
    try:
        raw = await redis_client.pipeline(cmds, **_conn_kw())
    except redis_client.RedisConnectionError:
        return await _redis_key_cli(k)
    except redis_client.RedisError as e:
        raise RuntimeError(str(e)) from None
    reps = [_shape(r, kind) for r, kind in zip(raw, kinds)]
    if isinstance(reps[0], redis_client.RedisError):
        raise RuntimeError(str(reps[0]))
    ktype = reps[0]

    def pick(i: int):
        v = reps[i]
        # 类型不匹配的探测命令返回 WRONGTYPE：这不是故障，是"这个 key 不是那种类型"，
        # 让一条探测把整页打挂的话，用户就再也看不到 key 的内容了。
        return None if isinstance(v, redis_client.RedisError) else v

    ttl = pick(1)
    mem = pick(2)
    truncated = False
    if ktype == "string":
        slen = int(pick(3) or 0)
        truncated = slen > 65536
        # GETRANGE 的 0..65535 在长度 ≤64KB 时就是全值，且客户端按协议长度取字节、
        # 不经任何 strip，因此不再需要旧实现里 GET（保原样）/GETRANGE（会被 strip）两套取法。
        data: object = _text(pick(8))
    elif ktype == "hash":
        n = int(pick(4) or 0)
        truncated = n > 200
        data = _pairs(pick(9) or [])
    elif ktype == "list":
        n = int(pick(5) or 0)
        truncated = n > 200
        data = pick(10) or []
    elif ktype == "set":
        n = int(pick(6) or 0)
        truncated = n > 200
        data = pick(11) or []
    elif ktype == "zset":
        n = int(pick(7) or 0)
        truncated = n > 200
        data = _pairs(pick(12) or [])
    else:
        data = None
    return {
        "key": k, "type": ktype,
        "ttl": ttl if isinstance(ttl, int) else None,
        "mem": mem if isinstance(mem, int) else None,
        "value": data, "truncated": truncated,
    }


async def redis_del(keys: list) -> dict:
    ks = [k for k in (str(x) for x in keys[:200] if x) if len(k) <= 512]
    reps = await _rp([["UNLINK", k] for k in ks], ["int"] * len(ks))
    return {"deleted": sum(1 for r in reps if isinstance(r, int) and r > 0)}


# ---------- Redis 在线写入 ----------

async def redis_set(k: str, value: str) -> dict:
    """覆盖 string 值，并保留原来的过期时间。

    SET 会把 key 变成永不过期，照直写等于顺手改掉了 TTL；先读 PTTL 再补
    PEXPIRE（不用 SET KEEPTTL，兼容 Redis 5）。
    """
    k = _check_key(k)
    if len(value) > MAX_CELL_CHARS:
        raise RuntimeError(f"值超过 {MAX_CELL_CHARS // 1048576}MB 上限")
    # SET 会无条件覆盖 key：不先确认类型，用户在 string 编辑器里点一次保存就能把
    # 一个 hash/list 整个换成字符串。TYPE+PTTL 同一条往返读回。
    ktype, pttl = await _rp([["TYPE", k], ["PTTL", k]], ["text", "int"])
    if ktype not in ("string", "none"):
        raise RuntimeError(f"该 key 是 {ktype}，不能用字符串编辑器覆盖")
    keep = pttl if isinstance(pttl, int) and pttl > 0 else 0
    # SET 与补 TTL 的 PEXPIRE 也在同一条往返里：中间不能插入别的请求，
    # 否则两条命令之间 key 被别人改写就会补错对象。
    # kinds 必须跟着 cmds 一起变：写死 ["text","int"] 时，没有 TTL 的 key（最常见的一类）
    # 只有一条 SET，_rp 立刻因数量不一致抛错——实测线上编辑任何永不过期的 string key
    # 都固定失败，等于字符串编辑功能整体不可用。
    cmds = [["SET", k, value]] + ([["PEXPIRE", k, str(keep)]] if keep else [])
    await _rp(cmds, ["text"] + (["int"] if keep else []))
    return {"ok": True, "ttl_kept": bool(keep)}


async def redis_member(k: str, typ: str, op: str, value: str = "", field: str = "", score: str = "") -> dict:
    """集合类 key 的条目增删：hash 按 field，其余按 value 本身。"""
    k = _check_key(k)
    typ = str(typ or "").lower()
    op = str(op or "").lower()
    if op not in ("add", "del"):
        raise RuntimeError("未知操作")
    if typ not in ("hash", "list", "set", "zset"):
        raise RuntimeError(f"{typ or '该'}类型不支持在线增删条目")
    if typ == "hash":
        if not field:
            raise RuntimeError("缺少 field")
        args = ["HSET", k, field, value] if op == "add" else ["HDEL", k, field]
    elif typ == "list":
        args = ["RPUSH", k, value] if op == "add" else ["LREM", k, "0", value]
    elif typ == "set":
        args = ["SADD", k, value] if op == "add" else ["SREM", k, value]
    else:
        s = str(score or "").strip()
        if op == "add" and not re.match(r"^-?\d+(\.\d+)?$", s):
            raise RuntimeError("score 必须是数字")
        args = ["ZADD", k, s, value] if op == "add" else ["ZREM", k, value]
    n = await _r(args, kind="int")
    if op == "del" and n == 0:
        raise RuntimeError("没有找到要删除的条目（值需完全一致）")
    return {"ok": True, "n": n}


async def redis_ttl(k: str, ttl) -> dict:
    k = _check_key(k)
    try:
        t = int(str(ttl).strip())
    except ValueError:
        raise RuntimeError("TTL 必须是整数秒，-1 表示不过期") from None
    if t < -1 or t > 31536000000:
        raise RuntimeError("TTL 范围：-1（永久）到 1000 年")
    # EXPIRE 传负数会直接删 key，永久只能走 PERSIST
    applied = await _r(["PERSIST", k] if t == -1 else ["EXPIRE", k, str(t)], kind="int")
    return {"ok": True, "applied": applied == 1, "ttl": t}

