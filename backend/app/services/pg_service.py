import asyncio
import json
import os
import re

from .. import database as dbm
from ..util import kill_group, run

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def is_ident(s: str) -> bool:
    return bool(IDENT_RE.match(s or ""))


def _quote_pass(p: str) -> str:
    if not re.match(r"^[\x20-\x7e]{1,128}$", p or ""):
        raise RuntimeError("密码只允许可见 ASCII 字符且不超过 128 位")
    return "'" + p.replace("'", "''") + "'"


PSQL_BASE = "psql -X -A -t -v ON_ERROR_STOP=1"


async def psql(sql: str) -> str:
    """执行 SQL，返回 stdout。

    ON_ERROR_STOP=1 是这里的关键：psql 默认对每条语句独立报错但整体退出码仍是 0，
    实测 `SELECT 1; SELECT * FROM nope; SELECT 2` 在不加该参数时
    返回码 0、stdout 只有 "1\\n2"、ERROR 只在 stderr——面板因此把
    "drop_role 目标不存在""SQL 控制台中途报错"一律显示为成功（假成功）。
    加上之后出错语句之后的内容不再执行，退出码 3，错误能真正上抛。
    """
    proc = await asyncio.create_subprocess_exec(
        "su", "-s", "/bin/sh", "postgres", "-c", PSQL_BASE,
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


async def psql_json(sql: str) -> list:
    """sql 必须是 `SELECT json_agg(...)::text`，返回解析后的数组。"""
    out = await psql(sql)
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


async def query_sql(sql: str) -> str:
    if not sql.strip():
        raise RuntimeError("空查询")
    return await psql(sql)


def redis_password():
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


async def redis_info() -> dict:
    pass_ = redis_password()
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
    # DBSIZE 失败时不能把错误正文当"Key 总数"显示：前端是 {{ redis.dbsize ?? '—' }}，
    # 任何非空字符串都会被原样渲染成数字位置。失败就返回 None，让它显示成占位符。
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
    return {"ok": True}
