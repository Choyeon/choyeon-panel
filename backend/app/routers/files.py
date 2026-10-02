import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import database as dbm
from ..services import files_service

router = APIRouter()


@router.get("/api/files")
async def files_list(request: Request):
    try:
        return files_service.list_dir(request.query_params.get("path") or "/root/www")
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/files/content")
async def files_read(request: Request):
    try:
        return files_service.read_text(request.query_params.get("path") or "")
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.put("/api/files/content")
async def files_write(request: Request):
    try:
        raw = await request.body()
        return files_service.write_text(request.query_params.get("path") or "", raw.decode("utf8"))
    except Exception as e:
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
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/files/action")
async def files_action(request: Request):
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        r = files_service.fs_action(body.get("action", ""), body.get("path", ""), body.get("path2"))
        dbm.audit(request.state.cp_sub, f"file:{body.get('action')}", body.get("path"))
        return r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
