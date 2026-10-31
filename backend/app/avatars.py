"""头像文件的落盘清理。

文件名只有 `<用户id>.<扩展名>` 一种形状（id 是自增整数，扩展名由服务端魔数嗅探决定），
所以"这个用户还有哪些头像文件"是一次前缀匹配，不需要读数据库里的 avatar 字段。
重新上传、界面删号、CLI 删号三处都要清旧文件，实现放在这里只留一份。
"""

import os

from . import config, util


def purge(uid: int) -> dict:
    """删掉该用户的全部头像文件，并把「没删干净」如实交出来。

    返回 `{"purged": 删掉的个数, "kept": 没删掉的文件名, "unread": 目录读不动的异常类型或 None}`。

    删号必须连文件一起删：/api/avatars/<id>.png 是免登录的公开路由（<img> 带不了
    Authorization），行删了文件还留着，这张脸就永久挂在一个已经不存在的账号上，
    备份页、列表页都看不到它，也没法再清。

    两个坑都在这条路上，都得让调用方看见，不能折成"0 个 = 干净"（批次97）：
    ① 目录读不动（缺权限、父路径不是目录）和目录确实不存在是两回事——后者才是"没
       什么可删"，前者用 `dir_state` 标成 `unread`，别让调用方拿它当绿灯；
    ② 单个文件 unlink 失败（只读挂载、`chattr +i`、I/O 错）不能 `except: pass` 咽掉，
       否则计数看着删完了，那张脸还在原处挂着。
    """
    d = config.AVATAR_DIR
    ok, names, why = util.dir_state(d)
    if ok is not True:
        # False=确认目录不存在（没有可删，purged 0 且不是异常）；None=读不动，把原因带出去。
        return {"purged": 0, "kept": [], "unread": why if ok is None else None}
    purged = 0
    kept: list[str] = []
    for name in names:
        if name.startswith(f"{uid}."):
            try:
                os.unlink(os.path.join(d, name))
                purged += 1
            except OSError:
                kept.append(name)
    return {"purged": purged, "kept": kept, "unread": None}


def residue_note(res: dict) -> str:
    """把 purge 的结果折成一句"哪里没删干净"，删干净则空串。

    删号在网页路由和 CLI 两处都要说同一句话，措辞放这里只留一份：调用方一旦各自
    组织，一处念"已删除"、一处念"头像可能残留"，同一个失败就有了两种脸色。
    这里只带文件名与异常类型，不带绝对路径。
    """
    if res.get("unread"):
        return f"头像目录读不动（{res['unread']}），无法确认旧头像已清理"
    kept = res.get("kept") or []
    if kept:
        return f"旧头像没删掉：{'、'.join(kept)} 仍能通过 /api/avatars/ 免登录访问"
    return ""
