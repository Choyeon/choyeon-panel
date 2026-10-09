"""对外错误契约：把「给运维看的话」和「给日志看的细节」分开。

面板的路由历史上直接把 `str(e)` 回给前端，一条 OSError 就足以把绝对路径、
errno、连接串（含用户名）原样贴到浏览器控制台里——而控制台还会进审计与截图。
这里的做法是：**保留中文提示原文**（前端 toast 与测试都依赖它），只把路径与凭据
擦掉，并把完整异常写进日志。新增代码更推荐直接 raise 本模块的异常，由 main.py 的
handler 统一转成同一种响应体。
"""

import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .log import warn
from .util import redact_creds

# /root/a/b、/etc/nginx/conf.d/x.conf 这类绝对路径（至少两段，避免把 "/ " 误伤）
_ABS_PATH = re.compile(r"(?<![\w.])/(?:[A-Za-z0-9_.+:@-]+/)+[A-Za-z0-9_.+:@-]*")
# psycopg/redis 失败信息里的 user=... password=...
_KV_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b\s*=\s*\S+")
_MAX = 300


class PanelError(Exception):
    """带 HTTP 状态码与对外文案的领域异常。"""

    status = 400
    code = "bad_input"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class BadInput(PanelError):
    status, code = 400, "bad_input"


class NotFound(PanelError):
    status, code = 404, "not_found"


class Forbidden(PanelError):
    status, code = 403, "forbidden"


class ExecFailed(PanelError):
    """命令/远程服务确实失败了：把原因留给运维，同时进日志。"""

    status, code = 502, "exec_failed"


# 这些是程序自身的 bug，原文里通常是变量名/属性名，给运维看没有意义、还会暴露内部结构
_PROGRAMMER_ERRORS = (TypeError, AttributeError, NameError, KeyError, IndexError, ImportError, UnboundLocalError)


def public_message(exc: BaseException, *, fallback: str = "操作失败，请查看面板日志") -> str:
    """对外可见的一句话：预期异常给脱敏原文，程序 bug 只给兜底文案。"""
    if isinstance(exc, PanelError):
        text = exc.message
    elif isinstance(exc, _PROGRAMMER_ERRORS):
        return fallback
    else:
        text = str(exc) or fallback
    text = _KV_SECRET.sub(r"\1=***", redact_creds(text))
    text = _ABS_PATH.sub("<路径>", text).strip()
    return text[:_MAX] or fallback


def err_json(exc: BaseException, status: int | None = None, *, log_tag: str | None = None) -> JSONResponse:
    """旧式 `return JSONResponse(400, {"error": str(e)})` 的替代：状态码不变，内容已脱敏。"""
    if not isinstance(exc, PanelError):
        warn(f"[{log_tag or 'api'}]", type(exc).__name__, str(exc)[:500])
    return JSONResponse(
        status_code=status or getattr(exc, "status", 400),
        content={"error": public_message(exc), "code": getattr(exc, "code", "bad_input")},
    )


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(PanelError)
    async def _panel_error(_req, exc: PanelError):
        return JSONResponse(status_code=exc.status, content={"error": public_message(exc), "code": exc.code})
