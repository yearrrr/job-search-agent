"""第二版资料页面与局部更新接口。"""

import json

from fastapi import BackgroundTasks, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .schemas import InputError, valid_corpus
from .services.documents import MAX_FILE_BYTES
from .services.profile_parser import ProfileParser
from .services.profile_v2 import MAIN_CATEGORIES, ProfileV2


def install_profile_routes(app, database, settings, render, form):
    profile, parser = ProfileV2(database), ProfileParser(database, settings)
    app.state.profile_parser = parser

    def review(request, document_id, partial=False):
        doc = profile.detail(document_id)
        if doc["kind"] == "notes":
            return RedirectResponse(f"/documents/{document_id}", status_code=303)
        return render(
            request,
            "v2_entries.html" if partial else "v2_document.html",
            doc["corpus"],
            doc=doc,
            parse_job=parser.latest(document_id),
            key_configured=settings.key_configured,
            main_categories=MAIN_CATEGORIES,
        )

    @app.get("/v2/documents/{document_id}")
    def document_page(request: Request, document_id: str):
        return review(request, document_id)

    @app.get("/v2/documents/{document_id}/entries")
    def entries_fragment(request: Request, document_id: str):
        return review(request, document_id, partial=True)

    @app.get("/v2/documents/{document_id}/pages/{page}")
    def page_image(document_id: str, page: int):
        return FileResponse(profile.page_path(document_id, page), media_type="image/jpeg")

    @app.post("/v2/documents/upload")
    async def upload(request: Request, background: BackgroundTasks):
        data = await form(request)
        uploaded = data.get("file")
        if not isinstance(uploaded, UploadFile):
            raise InputError("请选择文件。")
        try:
            content = await uploaded.read(MAX_FILE_BYTES + 1)
        finally:
            await uploaded.close()
        result = await run_in_threadpool(
            profile.import_file,
            uploaded.filename or "",
            content,
            data.get("kind"),
            data.get("corpus"),
            data.get("operation_id"),
        )
        if not result["duplicate"] and settings.key_configured:
            task = parser.start(result["id"], "parse-upload-" + data["operation_id"][:65])
            background.add_task(parser.run, task["id"])
        return RedirectResponse(f"/v2/documents/{result['id']}", status_code=303)

    @app.post("/v2/documents/paste")
    async def paste(request: Request, background: BackgroundTasks):
        data = await form(request)
        result = profile.import_file(
            str(data.get("name", "补充材料")) + ".txt",
            str(data.get("text", "")).encode(),
            data.get("kind"),
            data.get("corpus"),
            data.get("operation_id"),
        )
        if not result["duplicate"] and settings.key_configured:
            task = parser.start(result["id"], "parse-paste-" + data["operation_id"][:65])
            background.add_task(parser.run, task["id"])
        return RedirectResponse(f"/v2/documents/{result['id']}", status_code=303)

    @app.post("/v2/documents/{document_id}/parse")
    async def parse(request: Request, document_id: str, background: BackgroundTasks):
        data = await form(request)
        task = parser.start(document_id, data.get("operation_id"))
        background.add_task(parser.run, task["id"])
        return JSONResponse({"task_id": task["id"], "message": "解析任务已提交。"})

    @app.get("/v2/profile-tasks/{job_id}")
    def parse_status(job_id: str):
        return parser.status(job_id)

    @app.post("/v2/documents/{document_id}/review")
    async def save_entries(request: Request, document_id: str):
        data = await form(request, max_part_size=4 * 1024 * 1024)
        try:
            items = json.loads(str(data.get("items", "")))
            result = profile.update_entries(document_id, items, data.get("operation_id"))
        except InputError:
            raise
        except (KeyError, TypeError, ValueError):
            raise InputError("提交的经历格式无效，请核对后重试。") from None
        return JSONResponse(
            {
                **result,
                "counts": profile.detail(document_id)["counts"],
                "message": f"已保存 {len(result['entries'])} 条经历。",
            }
        )

    @app.get("/v2/profile/new")
    def new_entry(request: Request, corpus: str = "personal"):
        valid_corpus(corpus)
        return render(request, "v2_manual.html", corpus)

    @app.post("/v2/profile/new")
    async def add_entry(request: Request):
        data = await form(request)
        values = {
            key: str(data.get(key, ""))
            for key in ("title", "description", "category", "period", "organization", "role")
        }
        values["technologies"] = [
            s.strip()
            for s in str(data.get("technologies", "")).replace("，", ",").split(",")
            if s.strip()
        ]
        result = profile.add_manual(data.get("corpus"), values, data.get("operation_id"))
        return RedirectResponse(f"/v2/documents/{result['id']}", status_code=303)
