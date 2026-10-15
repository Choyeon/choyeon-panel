"""只读探针的取数骨架：TTL 缓存、锁内复查、以及按到达时间合并的强制刷新。

七个对账探针（updates / ports / ssh-audit / storage / memory / pending / load）各自抄过一份
「带 TTL 的缓存 + 一把锁」，抄出来的三份不一样，而差别都落在用户身上：

- updates / exposure / login 在**拿锁之前**不查缓存。`apt list` 在包多的机器上能跑到
  APT_TIMEOUT（120 秒），这几分钟里连普通的不带 refresh 的读取都要排队等锁——表现为
  「点了重新探测，整个页签转圈」。
- 拿锁之后是否复查缓存也不一致（storage / memory / pending / load 复查，其余不复查）。
  不复查的后果是同一件事被做两遍：两个人同时点「重新探测」，锁把它们串成两轮完整回源，
  第二个人等到的其实是他自己那轮，而前一轮刚跑完、结果一模一样。
- 时间基准两套（time.time 与 time.monotonic）。校时一跳，TTL 就跟着失真：wall clock 往回
  调会让缓存「永远不会过期」，往前跳会让每一发请求都重新回源。这里统一走 monotonic。

合并的依据是**到达时间**，不是数据新旧：请求进来先记下当时的 generation，拿到锁以后发现
号码已经变了，就说明「自你到达之后已经有人回过源了」——那份结果对你来说就是新鲜的，
直接用，不再重复回源。所以连点和多人同时刷新只跑一轮探测，而单次点击永远会真的回源
（它到达时号码还没变），不需要给「刷新」加任何最少间隔，也就不会出现「按了刷新却拿到旧数据」。

缓存的拷贝是浅拷贝，和收口前一致：加 `cached` 这个标记不会污染缓存本体，
但 payload 里的嵌套结构是共享的，调用方不要就地改。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


def new_cache() -> dict:
    """缓存本体的形状只有一个出处：at 用 monotonic 秒，gen 是回源轮次。"""
    return {"at": 0.0, "data": None, "gen": 0}


def reset(cache: dict) -> None:
    """把缓存清成「还没有过任何一轮探测」。gen 保留：合并看的是「有没有新 payload」。"""
    cache["at"] = 0.0
    cache["data"] = None


def fresh(cache: dict, ttl: float, now: float) -> bool:
    return bool(cache.get("data")) and now - float(cache.get("at") or 0.0) < ttl


def served(data: dict, *, cached: bool, merged: bool = False) -> dict:
    out = dict(data)
    out["cached"] = cached
    if merged:
        out["refresh_merged"] = True
    return out


async def read(cache: dict, lock: asyncio.Lock, collect: Callable[[], Awaitable[Any]], *,
               refresh: bool, ttl: float) -> dict:
    """按上面三条定义读一次探针数据：需要回源时才回源，回源成功后写入缓存。"""
    arrival = int(cache.get("gen") or 0)
    data = cache.get("data")
    if not refresh and fresh(cache, ttl, time.monotonic()):
        return served(data, cached=True)
    async with lock:
        data = cache.get("data")
        if data and int(cache.get("gen") or 0) != arrival:
            # 有人在你排队的时候跑完了这一轮：它对你就是新鲜答案，不必再跑一遍。
            return served(data, cached=True, merged=True)
        if not refresh and fresh(cache, ttl, time.monotonic()):
            return served(data, cached=True)
        data = await collect()
        cache["at"] = time.monotonic()
        cache["data"] = data
        cache["gen"] = arrival + 1
        return served(data, cached=False)
