import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import aio_db
from ..deps import require_admin
from ..errors import err_json
from ..services import pg_service
from ..util import redact_creds

# 整块 admin only：这一组接口能读出任意 PG 表的任意行、任意 Redis 键值（会话 token、
# 缓存里的凭据都在里面）。viewer 的"只读"不该包含这些，而逐条加检查已经证明会漏——
# 之前 7 个 GET 一条都没写。挂在 router 上，新增端点默认继承门禁。
router = APIRouter(dependencies=[Depends(require_admin)])


# SQL 里的口令是「裸字面量」，redact_creds 那套 key=value 规则抓不到：
#   ALTER ROLE app PASSWORD 'hunter2';   CREATE USER u IDENTIFIED BY 'hunter2';
# 控制台的语句会整条进审计表（管理员都能翻），抹不平就等于把凭据抄成第二份明文副本。
_SQL_PW_RE = re.compile(r"(?i)\b(password|identified\s+by)\s+(?:'[^']*'|\"[^\"]*\"|[^\s,;)]+)")


def _sql_for_audit(sql: str) -> str:
    return redact_creds(_SQL_PW_RE.sub(lambda m: f"{m.group(1)} ***", str(sql or "")))[:70]


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = None
    return b if isinstance(b, dict) else {}


@router.get("/api/db/pg/databases")
async def pg_databases():
    try:
        return await pg_service.list_dbs()
    except Exception as e:  # noqa: BLE001
        return err_json(e, 502)


@router.get("/api/db/pg/roles")
async def pg_roles():
    try:
        return await pg_service.list_roles()
    except Exception as e:  # noqa: BLE001
        return err_json(e, 502)


@router.post("/api/db/pg/manage")
async def pg_manage(req: Request):
    body = await _body(req)
    action = body.get("action")
    name = body.get("name") or ""
    try:
        if action == "createDb":
            await pg_service.create_db(name, body.get("owner"))
        elif action == "dropDb":
            await pg_service.drop_db(name)
        elif action == "createRole":
            await pg_service.create_role(name, body.get("password") or "")
        elif action == "setPassword":
            await pg_service.set_role_password(name, body.get("password") or "")
        elif action == "dropRole":
            await pg_service.drop_role(name)
        elif action == "grant":
            await pg_service.grant(name, body.get("db") or "")
        else:
            raise RuntimeError("未知操作")
        await aio_db.audit(req.state.cp_sub, f"db:{action}", name)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/db/pg/query")
async def pg_query(req: Request):
    """SQL 控制台。db 决定语句跑在哪个库，缺省 postgres。

    没有这个参数时控制台只能摸到 psql 的默认库：用户在「PG 数据浏览」里选中业务库、
    切到控制台敲同样的表名，得到的是 relation does not exist —— 面板没说它在别的库，
    看着像浏览器的表列表错了。返回体回带 db，界面据此写明执行位置。
    """
    body = await _body(req)
    sql = body.get("sql") or ""
    db = str(body.get("db") or "postgres").strip()
    if not pg_service.is_ident(db):
        return JSONResponse(status_code=400, content={"error": "库名不合法"})
    try:
        out = await pg_service.query_sql(sql, db)
        await aio_db.audit(req.state.cp_sub, "db:sql", f"{db}: {_sql_for_audit(sql)}")
        return {"result": out, "db": db}
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.get("/api/db/redis")
async def redis():
    try:
        return await pg_service.redis_info()
    except Exception as e:  # noqa: BLE001
        return err_json(e, 502)


@router.post("/api/db/redis/password")
async def redis_password(req: Request):
    body = await _body(req)
    pw = (body.get("password") or "").strip()
    try:
        r = pg_service.set_redis_password(pw)
    except Exception as e:  # noqa: BLE001
        return err_json(e)
    # 只记事件不记内容：口令进审计表等于把凭据抄进一份谁都能翻的日志
    await aio_db.audit(req.state.cp_sub, "db:redis-password", "已清空" if not pw else "已更新")
    return r


# ---------- 网页数据浏览 ----------

@router.get("/api/db/pg/{db}/tables")
async def pg_tables(db: str):
    try:
        return await pg_service.list_tables(db)
    except Exception as e:  # noqa: BLE001
        return err_json(e, 502)


@router.get("/api/db/pg/{db}/table")
async def pg_table(db: str, req: Request):
    q = req.query_params
    try:
        page = int(q.get("page") or 1)
        size = int(q.get("size") or 50)
    except ValueError:
        page, size = 1, 50
    try:
        return await pg_service.table_data(db, q.get("schema") or "public", q.get("name") or "", page, size)
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.get("/api/db/redis/keys")
async def redis_keys(req: Request):
    q = req.query_params
    try:
        return await pg_service.redis_scan(q.get("cursor") or "0", q.get("pattern") or "*", int(q.get("count") or 200))
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.get("/api/db/redis/key")
async def redis_key_detail(req: Request):
    try:
        return await pg_service.redis_key(req.query_params.get("k") or "")
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/db/redis/del")
async def redis_key_del(req: Request):
    body = await _body(req)
    keys = body.get("keys")
    if not isinstance(keys, list) or not keys:
        return JSONResponse(status_code=400, content={"error": "缺少 keys 参数"})
    try:
        r = await pg_service.redis_del([str(k) for k in keys])
    except Exception as e:  # noqa: BLE001
        return err_json(e)
    # 不记录 key 内容：业务 key 名本身可能带敏感信息，只记删了几个
    await aio_db.audit(req.state.cp_sub, "db:redis-del", f"{r['deleted']} 个 key")
    return r


# ---------- 在线写入（表行 + Redis 值） ----------

@router.post("/api/db/pg/{db}/row")
async def pg_row_write(db: str, req: Request):
    """按主键（无主键时按 ctid）更新/删除单行，或插入单行。

    审计只记 库.表 + 动作 + 影响行数：字段值可能是密码、token 或业务数据，
    抄进审计表等于又开了一份人人可读的副本。
    """
    body = await _body(req)
    action = str(body.get("action") or "")
    schema = str(body.get("schema") or "public")
    table = str(body.get("table") or "")
    try:
        r = await pg_service.write_row(db, schema, table, action, body.get("key"), body.get("values"))
        await aio_db.audit(req.state.cp_sub, f"db:row-{action}", f"{db}.{schema}.{table} ×{r['affected']}")
        return r
    except Exception as e:  # noqa: BLE001
        # 驱动报错的原文经常把带凭据的连接信息或整条语句回显出来，而审计表跟着 panel.db 推异地
        await aio_db.audit(req.state.cp_sub, f"db:row-{action}",
                           f"{db}.{schema}.{table}(失败: {redact_creds(str(e))[:120]})")
        return err_json(e)


@router.post("/api/db/redis/set")
async def redis_key_set(req: Request):
    body = await _body(req)
    try:
        r = await pg_service.redis_set(str(body.get("key") or ""), str(body.get("value") or ""))
        await aio_db.audit(req.state.cp_sub, "db:redis-set", f"string key（TTL {'保留' if r['ttl_kept'] else '未变'}）")
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/db/redis/member")
async def redis_key_member(req: Request):
    body = await _body(req)
    typ = str(body.get("type") or "")
    op = str(body.get("op") or "")
    try:
        r = await pg_service.redis_member(
            str(body.get("key") or ""), typ, op,
            str(body.get("value") or ""), str(body.get("field") or ""), str(body.get("score") or ""),
        )
        await aio_db.audit(req.state.cp_sub, f"db:redis-{typ}-{op}", f"{str(body.get('key') or '')[:80]}")
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/db/redis/ttl")
async def redis_key_ttl(req: Request):
    body = await _body(req)
    try:
        r = await pg_service.redis_ttl(str(body.get("key") or ""), body.get("ttl"))
        await aio_db.audit(req.state.cp_sub, "db:redis-ttl", f"{str(body.get('key') or '')[:80]} → {r['ttl']}s")
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)
