import asyncio
import os
import re

from .. import config
from .. import database as dbm

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def is_ident(s: str) -> bool:
    return bool(IDENT_RE.match(s or ""))


def _quote_pass(p: str) -> str:
    if not re.match(r"^[\x20-\x7e]{1,128}$", p or ""):
        raise RuntimeError("密码只允许可见 ASCII 字符且不超过 128 位")
    return "'" + p.replace("'", "''") + "'"


async def psql(sql: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "su", "-s", "/bin/sh", "postgres", "-c", 'psql -X -A -t -F "|"',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _collect():
        proc.stdin.write(sql.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        chunks = []
        size = 0
        while True:
            part = await proc.stdout.read(64 * 1024)
            if not part:
                break
            size += len(part)
            if size > 512 * 1024:
                proc.kill()
                raise RuntimeError("输出过大（>512KB），请加 LIMIT")
            chunks.append(part)
        err = await proc.stderr.read()
        await proc.wait()
        return b"".join(chunks), err

    try:
        out, err = await asyncio.wait_for(_collect(), timeout=30)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError("psql 超时")
    if proc.returncode == 0:
        return out.decode(errors="replace").strip()
    raise RuntimeError(err.decode(errors="replace").strip() or f"psql exit {proc.returncode}")


async def list_dbs() -> list:
    out = await psql(
        "SELECT d.datname, pg_get_userbyid(d.datdba) owner, pg_size_pretty(pg_database_size(d.datname)) size,\n"
        "        (SELECT count(*) FROM pg_stat_activity a WHERE a.datname=d.datname) conns\n"
        "     FROM pg_database d WHERE d.datistemplate=false AND d.datallowconn=true ORDER BY datname;"
    )
    rows = []
    for line in out.split("\n"):
        f = line.split("|")
        rows.append({"name": f[0], "owner": f[1], "size": f[2], "conns": f[3]})
    return rows


async def list_roles() -> list:
    out = await psql(
        "SELECT r.rolname, r.rolsuper, r.rolcanlogin, r.rolcreaterole, r.rolcreatedb\n"
        "     FROM pg_roles r WHERE r.rolname NOT LIKE 'pg\\_%' ORDER BY rolname;"
    )
    rows = []
    for line in out.split("\n"):
        f = line.split("|")
        rows.append({
            "name": f[0],
            "super": f[1] == "t",
            "login": f[2] == "t",
            "createrole": f[3] == "t",
            "createdb": f[4] == "t",
        })
    return rows


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
    from ..util import run

    pass_ = redis_password()
    env = dict(os.environ)
    if pass_:
        env["REDISCLI_AUTH"] = pass_
    r = await run("redis-cli", ["INFO"], env=env)
    if r["code"] != 0 or r["out"].startswith("NOAUTH"):
        raise RuntimeError("Redis 需要密码，请在下方设置" if r["out"].startswith("NOAUTH") else r["out"])
    info: dict[str, str] = {}
    for line in r["out"].split("\n"):
        i = line.find(":")
        if i > 0 and not line.startswith("#"):
            info[line[:i]] = line[i + 1:].strip()
    keys = await run("redis-cli", ["DBSIZE"], env=env)
    return {"info": info, "dbsize": keys["out"], "authed": bool(pass_)}


def set_redis_password(p: str):
    if p:
        dbm.set_setting("redis_password", p)
    return {"ok": True}
