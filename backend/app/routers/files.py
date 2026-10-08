import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import config
from .. import database as dbm
from ..deps import need_admin
from ..services import files_service

router = APIRouter()


@router.get("/api/files")
async def files_list(request: Request):
    # 文件接口能直接读到应用目录下的 .env（数据库口令、第三方 key 都在里面），
    # 而 app_get 精心做的那些脱敏会被这里整份绕过。中间件只按 HTTP 方法拦写操作，
    # 挡不住 GET，所以每个端点都要显式要求 admin——包括只读账号用来渲染页面的接口。
    if r := need_admin(request):
        return r
    try:
        return files_service.list_dir(request.query_params.get("path") or "")
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/files/content")
async def files_read(request: Request):
    if r := need_admin(request):
        return r
    try:
        return files_service.read_text(request.query_params.get("path") or "")
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.put("/api/files/content")
async def files_write(request: Request):
    if r := need_admin(request):
        return r
    try:
        declared = request.headers.get("content-length")
        if declared and int(declared) > config.MAX_UPLOAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": f"文件超过 {config.MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"},
            )
        raw = await request.body()
        if len(raw) > config.MAX_UPLOAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": f"文件超过 {config.MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"},
            )
        path = request.query_params.get("path") or ""
        # 原样按字节写入：解码成字符串再落盘会把二进制文件（图片/zip/db）毁成 U+FFFD，
        # 而接口仍返回 200，用户以为上传成功。
        r = files_service.write_text(path, raw)
        # 改文件是覆盖面最大的操作，出事时只有一份审计能回答"谁在什么时候动过什么"。
        dbm.audit(request.state.cp_sub, "file:write", path)
        return r
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/files/download")
async def files_download(request: Request):
    if r := need_admin(request):
        return r
    try:
        abs_ = files_service.safe_path(request.query_params.get("path") or "")
        if not os.path.isfile(abs_):
            return JSONResponse(status_code=400, content={"error": "不是文件"})
        return FileResponse(
            abs_,
            media_type="application/octet-stream",
            filename=os.path.basename(abs_) or "file",
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/files/action")
async def files_action(request: Request):
    if r := need_admin(request):
        return r
    try:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        r = files_service.fs_action(body.get("action", ""), body.get("path", ""), body.get("path2"))
        dbm.audit(request.state.cp_sub, f"file:{body.get('action')}", body.get("path"))
        return r
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})
