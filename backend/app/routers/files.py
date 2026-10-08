import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import config
from .. import database as dbm
from ..services import files_service

router = APIRouter()


@router.get("/api/files")
async def files_list(request: Request):
    try:
        # 默认目录取 FILE_ROOTS 第一项：写死 /root/www 时，改过 CP_FILE_ROOTS
        # 的部署一打开文件页就指向一个不在白名单里的目录，直接报错。
        default = config.FILE_ROOTS[0] if config.FILE_ROOTS else "/"
        return files_service.list_dir(request.query_params.get("path") or default)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/files/content")
async def files_read(request: Request):
    try:
        return files_service.read_text(request.query_params.get("path") or "")
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.put("/api/files/content")
async def files_write(request: Request):
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
        # 原样按字节写入：解码成字符串再落盘会把二进制文件（图片/zip/db）毁成 U+FFFD，
        # 而接口仍返回 200，用户以为上传成功。
        return files_service.write_text(request.query_params.get("path") or "", raw)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/files/download")
async def files_download(request: Request):
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
