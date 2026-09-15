"""岗位第二版页面：解析、核对、准备分析均由明确 POST 触发。"""

from fastapi import BackgroundTasks, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .agent.experience_tools import fresh
from .schemas import FIELD_LABELS, InputError, JobInput
from .services.jd_v2 import JD, MAX_IMAGE_BYTES, MAX_JD_BYTES
from .services.jobs import Jobs
from .services.runs_v2 import Runs
from .services.tasks import TaskStopped, make_snapshot


def install_job_v2_routes(app, database, settings, render, form):
    runs = Runs(database, settings)
    jd = JD(database, settings, runs)
    app.state.v2_runs = runs

    @app.post("/v2/jobs/import")
    async def import_jd(request: Request, background_tasks: BackgroundTasks):
        data = await form(request, max_files=6)
        files, size = [], 0
        for upload in data.getlist("images"):
            if isinstance(upload, UploadFile) and upload.filename:
                content = await upload.read(MAX_IMAGE_BYTES + 1)
                size += len(content)
                if size > MAX_JD_BYTES:
                    raise InputError("截图总大小不得超过 15 MB。")
                files.append((upload.filename, content))
        result = await run_in_threadpool(
            jd.import_source,
            str(data.get("corpus", "")),
            str(data.get("text", "")),
            files,
            data.get("operation_id"),
        )
        background_tasks.add_task(runs.run, result["id"])
        return RedirectResponse(f"/v2/runs/{result['id']}", status_code=303)

    @app.post("/v2/jobs/manual")
    async def manual_job(request: Request):
        data = await form(request)
        job = JobInput(**{key: str(data.get(key, "")) for key in JobInput.model_fields})
        fields = {
            key: {"text": str(data.get(key, "")), "sources": [], "method": "user_edit"}
            for key in FIELD_LABELS
        }
        if any(len(v["text"]) > 12000 for v in fields.values()):
            raise InputError("岗位字段单项不得超过 12000 字符。")
        result = Jobs(database).create(job, data.get("operation_id"), fields=fields)
        return RedirectResponse(f"/jobs/{result['id']}", status_code=303)

    def context(run_id):
        run = runs.detail(run_id)
        source = jd.source(run["target_id"]) if run["kind"] == "jd" else None
        confirmed = jd.confirmation(run_id) if source else None
        stale = False
        if run["kind"] == "preparation":
            try:
                fresh(database, run["snapshot"])
            except TaskStopped:
                stale = True
        draft = None
        if run["kind"] in ("recommendation", "resume"):
            from .services.resumes_v2 import Resumes, check_fresh

            try:
                check_fresh(database, run["snapshot"])
            except (TaskStopped, InputError):
                stale = True
            if run["kind"] == "resume" and run["status"] == "completed":
                draft = Resumes(database, settings, runs).draft(run_id)
        return {
            "draft": draft,
            "run_label": {
                "jd": "JD 解析",
                "preparation": "岗位准备分析",
                "recommendation": "项目推荐",
                "resume": "简历撰写",
            }[run["kind"]],
            "run": run,
            "source": source,
            "confirmed": confirmed,
            "stale": stale,
            "requirements": {r["id"]: r for r in run["snapshot"].get("requirements", [])},
            "facts": {f["id"]: f for f in run["snapshot"].get("facts", [])},
        }

    @app.get("/v2/runs/{run_id}")
    def run_page(request: Request, run_id: str):
        ctx = context(run_id)
        return render(request, "v2_run.html", ctx["run"]["corpus"], **ctx)

    @app.get("/v2/runs/{run_id}/content")
    def run_content(request: Request, run_id: str):
        ctx = context(run_id)
        return render(request, "v2_run_content.html", ctx["run"]["corpus"], **ctx)

    @app.get("/v2/runs/{run_id}/status")
    def run_status(run_id: str):
        run = runs.detail(run_id)
        return JSONResponse(
            {
                k: run[k]
                for k in (
                    "id",
                    "kind",
                    "status",
                    "message",
                    "error_code",
                    "model_calls",
                    "tool_calls",
                    "known_tokens",
                    "unknown_usage",
                    "cache_hits",
                    "tool_errors",
                )
            }
        )

    @app.post("/v2/runs/{run_id}/continue")
    async def resume_run(request: Request, run_id: str, background_tasks: BackgroundTasks):
        await form(request)
        result = runs.resume(run_id)
        background_tasks.add_task(runs.run, run_id)
        return JSONResponse({**result, "message": "已请求继续；沿用本任务原预算和调用记录。"})

    @app.get("/v2/jd-sources/{source_id}/images/{page}")
    def source_image(source_id: str, page: int):
        source = jd.source(source_id)
        asset = next((f for f in source["files"] if f["page"] == page), None)
        if not asset:
            raise InputError("截图不存在。")
        return FileResponse(database.local_path(asset["image"]), media_type="image/jpeg")

    @app.post("/v2/runs/{run_id}/confirm-jd")
    async def confirm_jd(request: Request, run_id: str):
        data = await form(request, max_part_size=250000)
        result = jd.confirm(run_id, dict(data), data.get("operation_id"))
        return JSONResponse({"message": "岗位已保存。", "url": f"/jobs/{result['id']}"})

    @app.post("/v2/jobs/{job_id}/prepare")
    async def prepare_job(request: Request, job_id: str, background_tasks: BackgroundTasks):
        data = await form(request)
        snapshot = make_snapshot(database, job_id)
        result = runs.create(
            "preparation",
            snapshot["corpus"],
            job_id,
            snapshot,
            str(data.get("engine", "")),
            data.get("operation_id"),
        )
        background_tasks.add_task(runs.run, result["id"])
        url = f"/v2/runs/{result['id']}"
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"url": url})
        return RedirectResponse(url, status_code=303)
