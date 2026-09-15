import hashlib
import hmac
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent.model import ERROR_MESSAGES, ModelError
from .config import Settings, load_settings
from .db import Database
from .schemas import (
    CATEGORY_LABELS,
    FIELD_LABELS,
    STATUS_LABELS,
    Conflict,
    InputError,
    JobInput,
    valid_corpus,
)
from .services.common import uid
from .services.documents import MAX_FILE_BYTES, Documents
from .services.jobs import Jobs
from .services.profile import Profile

PACKAGE_DIR = Path(__file__).parent


class BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0
        limit = (
            15 * 1024 * 1024 if scope.get("path") == "/v2/jobs/import" else MAX_FILE_BYTES
        ) + 128 * 1024

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > limit:
                raise BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLarge:
            await JSONResponse(
                {
                    "message": "请求过大；JD 截图总共限 15 MB，单张限 5 MB。"
                    if scope.get("path") == "/v2/jobs/import"
                    else "请求过大；文件限 5 MB，请拆分或粘贴关键文本。"
                },
                status_code=413,
            )(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    database = Database(settings.data_dir)
    documents, jobs, profile = Documents(database), Jobs(database), Profile(database)

    @asynccontextmanager
    async def lifespan(app):
        try:
            database.initialize()
        except (OSError, sqlite3.Error):
            raise RuntimeError(
                "本地数据库初始化失败，请检查目录权限与数据库版本；原数据未被替换。"
            ) from None
        yield

    app = FastAPI(
        title="个人求职管家", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}
    templates.env.filters["localtime"] = lambda value: (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%Y-%m-%d %H:%M:%S")
        if value
        else ""
    )
    templates.env.globals.update(
        op_id=uid,
        statuses=STATUS_LABELS,
        categories=CATEGORY_LABELS,
        field_labels=FIELD_LABELS,
        model_errors=ERROR_MESSAGES,
    )

    def render(request, name, corpus="demo", status_code=200, **context):
        valid_corpus(corpus)
        token = request.cookies.get("job_csrf") or secrets.token_urlsafe(32)
        response = templates.TemplateResponse(
            request=request,
            name=name,
            status_code=status_code,
            context={"mode": settings.mode, "corpus": corpus, "csrf": token, **context},
        )
        response.set_cookie("job_csrf", token, httponly=True, samesite="strict")
        return response

    async def form(request, max_part_size=150000, max_files=1):
        origin = request.headers.get("origin")
        if (origin and origin != str(request.base_url).rstrip("/")) or request.headers.get(
            "sec-fetch-site"
        ) == "cross-site":
            raise InputError("请求来源不匹配，请从本地工作台重新操作。")
        data = await request.form(max_files=max_files, max_fields=30, max_part_size=max_part_size)
        token, cookie = str(data.get("csrf", "")), request.cookies.get("job_csrf", "")
        if (
            not token
            or not cookie
            or not hmac.compare_digest(token.encode("utf-8"), cookie.encode("utf-8"))
        ):
            raise InputError("页面校验失败，请刷新工作台后重试。")
        return data

    def integer(data, key="revision"):
        try:
            value = int(data[key])
            if value < 0:
                raise ValueError
            return value
        except (KeyError, ValueError, TypeError):
            raise InputError("版本号无效，请刷新页面。") from None

    def redirect(path, result):
        return RedirectResponse(
            f"{path}/{result['id']}" + ("?duplicate=1" if result.get("duplicate") else ""),
            status_code=303,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        # 同源导航表单需要保留 Origin；跨源跳转仍不发送页面来源。
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.exception_handler(InputError)
    async def input_error(request: Request, error: InputError):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"message": str(error)}, status_code=409 if isinstance(error, Conflict) else 400
            )
        return render(
            request,
            "error.html",
            status_code=409 if isinstance(error, Conflict) else 400,
            message=str(error),
        )

    @app.exception_handler(ValidationError)
    async def validation_error(request: Request, error: ValidationError):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"message": "请检查必填内容、字段格式与长度。"}, status_code=400)
        return render(
            request,
            "error.html",
            status_code=400,
            message="请检查必填字段、文本长度与来源链接格式。",
        )

    @app.exception_handler(ModelError)
    async def model_error(request: Request, error: ModelError):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"message": ERROR_MESSAGES[error.code]}, status_code=400)
        return render(request, "error.html", status_code=400, message=ERROR_MESSAGES[error.code])

    @app.exception_handler(OSError)
    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, error: Exception):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"message": "本地存储暂不可用，输入已保留，请稍后重试。"}, status_code=503
            )
        return render(
            request,
            "error.html",
            status_code=503,
            message="本地文件或数据库暂不可用，请检查权限或稍后重试。",
        )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "stage": 4,
            "database": "ok",
            "schema_version": database.health(),
            "mode": settings.mode,
            "release": "v2-batch3",
        }

    @app.get("/")
    def home(request: Request, corpus: str = "demo"):
        listing = jobs.list(corpus)
        with database.connect() as connection:
            counts = dict(
                connection.execute(
                    """SELECT (SELECT count(*) FROM documents WHERE corpus=?) AS documents,
                (SELECT count(*) FROM facts f JOIN documents d ON d.id=f.document_id
                WHERE d.corpus=? AND f.status='pending') AS pending""",
                    (corpus, corpus),
                ).fetchone()
            )
        return render(
            request,
            "index.html",
            corpus,
            jobs=listing,
            counts=counts,
            confirmed_count=len(profile.confirmed(corpus)),
        )

    @app.post("/demo")
    async def demo(request: Request):
        await form(request)
        from .services.demo import seed_demo

        await run_in_threadpool(seed_demo, database)
        return RedirectResponse("/?corpus=demo", status_code=303)

    @app.get("/documents")
    def document_list(request: Request, corpus: str = "demo"):
        valid_corpus(corpus)
        with database.connect() as connection:
            listing = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM documents WHERE corpus=? ORDER BY created_at DESC", (corpus,)
                )
            ]
        return render(
            request,
            "documents.html",
            corpus,
            documents=listing,
            key_configured=settings.key_configured,
        )

    @app.post("/documents/upload")
    async def upload(request: Request):
        data = await form(request)
        uploaded = data.get("file")
        if not isinstance(uploaded, UploadFile):
            raise InputError("请先选择文件；也可在下方粘贴文本。")
        try:
            content = await uploaded.read(MAX_FILE_BYTES + 1)
        finally:
            await uploaded.close()
        result = await run_in_threadpool(
            documents.import_file,
            uploaded.filename or "",
            content,
            data.get("kind"),
            data.get("corpus"),
            data.get("operation_id"),
        )
        return redirect("/documents", result)

    @app.post("/documents/paste")
    async def paste(request: Request):
        data = await form(request)
        result = documents.import_file(
            str(data.get("name", "粘贴资料")) + ".txt",
            str(data.get("text", "")).encode("utf-8"),
            data.get("kind"),
            data.get("corpus"),
            data.get("operation_id"),
        )
        return redirect("/documents", result)

    @app.get("/documents/{document_id}")
    def document_detail(request: Request, document_id: str):
        with database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM v2_documents WHERE document_id=?", (document_id,)
            ).fetchone():
                return RedirectResponse(f"/v2/documents/{document_id}", status_code=303)
        doc = documents.detail(document_id)
        doc["excluded_count"] = sum(f["status"] == "rejected" for f in doc["facts"])
        if request.query_params.get("show_excluded") != "1":
            doc["facts"] = [f for f in doc["facts"] if f["status"] != "rejected"]
        return render(
            request,
            "document.html",
            doc["corpus"],
            doc=doc,
            duplicate=request.query_params.get("duplicate"),
        )

    @app.get("/documents/{document_id}/original")
    def original(document_id: str):
        doc = documents.detail(document_id)
        path = documents.original_path(document_id, doc["suffix"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
            raise InputError("原件校验失败，文件可能在程序外被改动；请保留现状并检查备份。")
        return FileResponse(
            path,
            filename=doc["name"],
            media_type="application/octet-stream",
            content_disposition_type="attachment",
        )

    @app.post("/facts/{fact_id}")
    async def fact_update(request: Request, fact_id: str):
        data = await form(request)
        return redirect(
            "/documents",
            documents.update_fact(
                fact_id,
                str(data.get("text", "")),
                data.get("category"),
                data.get("status"),
                integer(data),
                data.get("operation_id"),
            ),
        )

    @app.post("/documents/{document_id}/facts")
    async def fact_add(request: Request, document_id: str):
        data = await form(request)
        return redirect(
            "/documents",
            documents.add_fact(
                document_id,
                data.get("snippet_id"),
                data.get("category"),
                str(data.get("text", "")),
                data.get("operation_id"),
            ),
        )

    @app.post("/documents/{document_id}/extract")
    async def extract(request: Request, document_id: str):
        data = await form(request)
        if settings.mode != "deepseek":
            raise InputError(
                "当前为离线模式，可直接核对本地候选；真实模型提取需要在本地切换 DeepSeek 模式。"
            )
        from .services.extraction import extract_candidates

        await run_in_threadpool(
            extract_candidates, database, settings, document_id, data.get("operation_id")
        )
        return RedirectResponse(f"/documents/{document_id}", status_code=303)

    @app.get("/profile")
    def profile_page(request: Request, corpus: str = "demo"):
        return render(
            request,
            "profile.html",
            corpus,
            facts=profile.confirmed(corpus),
            preferences=profile.preferences(corpus),
        )

    @app.post("/profile/preferences")
    async def preferences_update(request: Request):
        data = await form(request)
        corpus = data.get("corpus")
        profile.set_preferences(
            corpus,
            str(data.get("roles", "")),
            str(data.get("cities", "")),
            str(data.get("notes", "")),
            integer(data),
            data.get("operation_id"),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"revision": profile.preferences(corpus)["revision"], "message": "偏好已保存。"}
            )
        return RedirectResponse(f"/profile?corpus={corpus}", status_code=303)

    @app.get("/jobs/new")
    def new_job(request: Request, corpus: str = "demo"):
        return render(request, "job_new.html", corpus, key_configured=settings.key_configured)

    @app.post("/jobs")
    async def create_job(request: Request):
        data = await form(request)
        job = JobInput(**{key: str(data.get(key, "")) for key in JobInput.model_fields})
        return redirect("/jobs", jobs.create(job, data.get("operation_id")))

    @app.get("/jobs/{job_id}")
    def job_detail(request: Request, job_id: str):
        from .services.drafts import Drafts
        from .services.runs_v2 import Runs
        from .services.tasks import Tasks

        job = jobs.detail(job_id)
        return render(
            request,
            "job.html",
            job["corpus"],
            job=job,
            tasks=Tasks(database, settings).list_for_job(job_id),
            preparations=Runs(database, settings).list_for_job(job_id),
            materials=Drafts(database).list_for_job(job_id),
            key_configured=settings.key_configured,
            duplicate=request.query_params.get("duplicate"),
        )

    @app.post("/jobs/{job_id}/fields")
    async def job_fields(request: Request, job_id: str):
        data = await form(request)
        result = jobs.update_fields(
            job_id,
            {key: str(data.get(key, "")) for key in FIELD_LABELS},
            integer(data),
            data.get("operation_id"),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"revision": jobs.detail(job_id)["revision"], "message": "岗位字段已保存。"}
            )
        return redirect("/jobs", result)

    @app.post("/jobs/{job_id}/status")
    async def job_status(request: Request, job_id: str):
        data = await form(request)
        before = jobs.detail(job_id)
        result = jobs.change_status(
            job_id, data.get("status"), integer(data), data.get("operation_id")
        )
        if "application/json" in request.headers.get("accept", ""):
            current = jobs.detail(job_id)
            return JSONResponse(
                {
                    "revision": current["revision"],
                    "status": current["status"],
                    "label": STATUS_LABELS[current["status"]],
                    "message": "岗位状态已更新。",
                    "ask_preparation": result.get("changed", before["status"] != "preparing")
                    and current["status"] == "preparing"
                    and result.get("revision", current["revision"]) == current["revision"],
                    "history": [
                        {
                            "time": h["created_at"],
                            "old": STATUS_LABELS.get(h["old_status"], "新建"),
                            "new": STATUS_LABELS[h["new_status"]],
                        }
                        for h in current["history"]
                    ],
                }
            )
        return redirect("/jobs", result)

    @app.post("/jobs/{job_id}/records")
    async def job_record(request: Request, job_id: str):
        data = await form(request)
        return redirect(
            "/jobs",
            jobs.add_note_or_todo(
                job_id,
                str(data.get("text", "")),
                str(data.get("due_date", "")),
                data.get("kind"),
                data.get("operation_id"),
            ),
        )

    @app.post("/todos/{todo_id}")
    async def todo_update(request: Request, todo_id: str):
        data = await form(request)
        if data.get("done") not in ("0", "1"):
            raise InputError("待办状态无效。")
        return redirect(
            "/jobs",
            jobs.set_todo(
                todo_id, data.get("done") == "1", integer(data), data.get("operation_id")
            ),
        )

    from .task_web import install_task_routes

    install_task_routes(app, database, settings, templates, render, form, integer)
    from .profile_web import install_profile_routes

    install_profile_routes(app, database, settings, render, form)
    from .job_v2_web import install_job_v2_routes

    install_job_v2_routes(app, database, settings, render, form)
    from .resume_web import install_resume_routes

    install_resume_routes(app, database, settings, render, form)
    return app
