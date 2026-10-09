"""线程池版数据库访问：热路径专用，避免 SQLite 的全局锁卡住事件循环。

database.py 用一把进程级 RLock 串行化所有读写（连接是 check_same_thread=False），
在 async 处理函数里直接调用就等于每个请求都在事件循环上做同步磁盘 I/O。
读写密集的路由用这里的包装，冷路径继续用 dbm.* 即可。
"""

from starlette.concurrency import run_in_threadpool

from . import database as dbm


async def query(sql: str, params=()) -> list:
    return await run_in_threadpool(dbm.query, sql, params)


async def query_one(sql: str, params=()) -> dict | None:
    return await run_in_threadpool(dbm.query_one, sql, params)


async def execute(sql: str, params=()):
    return await run_in_threadpool(dbm.execute, sql, params)


async def audit(username, action: str, detail=None) -> None:
    return await run_in_threadpool(dbm.audit, username, action, detail)
