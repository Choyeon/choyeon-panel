"""头像文件的落盘清理。

文件名只有 `<用户id>.<扩展名>` 一种形状（id 是自增整数，扩展名由服务端魔数嗅探决定），
所以"这个用户还有哪些头像文件"是一次前缀匹配，不需要读数据库里的 avatar 字段。
重新上传、界面删号、CLI 删号三处都要清旧文件，实现放在这里只留一份。
"""

import os

from . import config


def purge(uid: int) -> int:
    """删除该用户的全部头像文件，返回删掉的个数。

    删号必须连文件一起删：/api/avatars/<id>.png 是免登录的公开路由（<img> 带不了
    Authorization），行删了文件还留着，这张脸就永久挂在一个已经不存在的账号上，
    备份页、列表页都看不到它，也没法再清。
    """
    d = config.AVATAR_DIR
    if not os.path.isdir(d):
        return 0
    n = 0
    for name in os.listdir(d):
        if name.startswith(f"{uid}."):
            try:
                os.unlink(os.path.join(d, name))
                n += 1
            except OSError:
                pass
    return n
