import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import aio_db, config, errors
from ..deps import need_admin
from ..errors import err_json
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
        return await run_in_threadpool(files_service.list_dir, request.query_params.get("path") or "")
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.get("/api/files/content")
async def files_read(request: Request):
    if r := need_admin(request):
        return r
    try:
        return await run_in_threadpool(files_service.read_text, request.query_params.get("path") or "")
    except Exception as e:  # noqa: BLE001
        return err_json(e)


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
        r = await run_in_threadpool(files_service.write_text, path, raw)
        # 改文件是覆盖面最大的操作，出事时只有一份审计能回答"谁在什么时候动过什么"。
        await aio_db.audit(request.state.cp_sub, "file:write", path)
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


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
        return err_json(e)


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
        r = await run_in_threadpool(
            files_service.fs_action, body.get("action", ""), body.get("path", ""), body.get("path2")
        )
        await aio_db.audit(request.state.cp_sub, f"file:{body.get('action')}", body.get("path"))
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/files/upload")
async def files_upload(request: Request):
    """multipart 多文件上传到当前目录。

    旧做法是前端把每个文件读成文本再逐个 PUT，二进制内容被静默替换成 U+FFFD；
    这里直接接管上传字节，并按整个请求体做总量上限。
    """
    if r := need_admin(request):
        return r
    try:
        form = await request.form()
        files = [f for f in form.getlist("files") if hasattr(f, "read")]
        if not files:
            return JSONResponse(status_code=400, content={"error": "没有收到文件"})
        target = str(form.get("path") or "")
        overwrite = str(form.get("overwrite") or "") in {"1", "true", "yes"}
        saved, failed, total = [], [], 0
        for f in files:
            data = await f.read()
            total += len(data)
            if total > config.MAX_UPLOAD_BYTES:
                failed.append(f"{f.filename}（整体超过上限）")
                continue
            try:
                r = await run_in_threadpool(files_service.save_upload, target, f.filename, data, overwrite)
                saved.append(r)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{f.filename}（{errors.public_message(e)}）")
        await aio_db.audit(request.state.cp_sub, "file:upload", f"{target} ×{len(saved)}")
        return {"saved": saved, "failed": failed, "bytes": total}
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/files/extract")
async def files_extract(request: Request):
    if r := need_admin(request):
        return r
    try:
        body = await request.json()
        r = await run_in_threadpool(
            files_service.extract_archive, str(body.get("path") or ""), body.get("into") or None
        )
        await aio_db.audit(request.state.cp_sub, "file:extract", str(body.get("path") or ""))
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/files/archive")
async def files_archive(request: Request):
    if r := need_admin(request):
        return r
    try:
        body = await request.json()
        paths = body.get("paths") or []
        if not isinstance(paths, list):
            return JSONResponse(status_code=400, content={"error": "paths 要是数组"})
        r = await run_in_threadpool(files_service.make_archive, [str(x) for x in paths], str(body.get("name") or "archive.tar.gz"))
        await aio_db.audit(request.state.cp_sub, "file:archive", f"{len(paths)} 项")
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)
