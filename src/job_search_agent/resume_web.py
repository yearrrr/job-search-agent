"""简历工作台；生成和确认是 POST，阅读和下载不会触发模型调用。"""

import json

from fastapi import BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response

from .schemas import InputError
from .services.jobs import Jobs
from .services.resume_export import docx_bytes, pdf_bytes
from .services.resumes_v2 import ORDER, Resumes, check_fresh, snapshot_token
from .services.tasks import TaskStopped


def install_resume_routes(app, database, settings, render, form):
    resumes = Resumes(database, settings, app.state.v2_runs)
    app.state.resumes = resumes

    def json_field(data, name):
        try:
            return json.loads(str(data.get(name, "")))
        except (ValueError, RecursionError):
            raise InputError("表单内容格式无效，请保留输入并重新打开页面。") from None

    @app.get("/v2/jobs/{job_id}/resume")
    def workspace(request: Request, job_id: str, recommendation: str = "", source: str = ""):
        snapshot = resumes.snapshot(job_id)
        selected = {f["id"] for f in snapshot["facts"] if f["category"] not in ("project", "other")}
        rec, instruction, original_order = None, "", {}
        if recommendation or source:
            run = resumes.runs.detail(recommendation or source)
            if run["target_id"] != job_id or run["kind"] != (
                "recommendation" if recommendation else "resume"
            ):
                raise InputError("这份选择记录不属于当前岗位。")
            if recommendation:
                if run["status"] != "completed":
                    raise InputError("项目推荐尚未完成。")
                try:
                    check_fresh(database, run["snapshot"])
                except (TaskStopped, InputError):
                    raise InputError("推荐所用资料已变化，请重新推荐或手动选择。") from None
                rec = run["result"]
                selected.update(p["fact_id"] for p in rec["projects"])
                original_order = {p["fact_id"]: i for i, p in enumerate(rec["projects"])}
            else:
                selected = set(run["snapshot"]["selected_ids"])
                original_order = {i: n for n, i in enumerate(run["snapshot"]["selected_ids"])}
                instruction = run["snapshot"]["instruction"]
        facts = sorted(
            snapshot["facts"],
            key=lambda f: (ORDER.index(f["category"]), original_order.get(f["id"], 1000)),
        )
        histories, versions = resumes.list_for_job(job_id)
        return render(
            request,
            "resume_workspace.html",
            snapshot["corpus"],
            job=Jobs(database).detail(job_id),
            snapshot=snapshot,
            token=snapshot_token(snapshot),
            facts=facts,
            category_order=ORDER,
            selected=selected,
            recommendation=rec,
            instruction=instruction,
            histories=histories,
            versions=versions,
            key_configured=settings.key_configured,
        )

    @app.post("/v2/jobs/{job_id}/resume/start")
    async def start(request: Request, job_id: str, background_tasks: BackgroundTasks):
        data = await form(request)
        result = resumes.start(
            job_id,
            str(data.get("kind", "")),
            json_field(data, "selection"),
            str(data.get("snapshot_token", "")),
            str(data.get("engine", "")),
            str(data.get("instruction", "")),
            data.get("operation_id"),
        )
        background_tasks.add_task(resumes.runs.run, result["id"])
        return JSONResponse({"url": f"/v2/runs/{result['id']}"})

    @app.post("/v2/resumes/{run_id}/edit")
    async def edit(request: Request, run_id: str):
        data = await form(request)
        try:
            revision = int(str(data.get("revision", "")))
        except ValueError:
            raise InputError("草稿版本无效。") from None
        result = resumes.save(
            run_id, json_field(data, "content"), revision, data.get("operation_id")
        )
        return JSONResponse({**result, "message": "草稿已保存，可以继续编辑或确认此版本。"})

    @app.post("/v2/resumes/{run_id}/confirm")
    async def confirm(request: Request, run_id: str):
        data = await form(request)
        if data.get("reviewed") != "yes":
            raise InputError("请先核对正文，确认内容真实准确。")
        try:
            revision = int(str(data.get("revision", "")))
        except ValueError:
            raise InputError("草稿版本无效。") from None
        result = resumes.confirm(run_id, revision, data.get("operation_id"))
        return JSONResponse(
            {
                **result,
                "url": f"/v2/resume-versions/{result['id']}",
                "message": "已确认保存，可以下载 Word 和 PDF。",
            }
        )

    @app.get("/v2/resume-versions/{version_id}")
    def version_page(request: Request, version_id: str):
        version = resumes.version(version_id)
        stale = False
        try:
            check_fresh(database, version["snapshot"])
        except (InputError, TaskStopped):
            stale = True
        return render(
            request,
            "resume_version.html",
            version["snapshot"]["corpus"],
            version=version,
            stale=stale,
            facts={f["id"]: f for f in version["snapshot"]["facts"]},
        )

    @app.get("/v2/resume-versions/{version_id}/download/{format}")
    def download(version_id: str, format: str):
        if format not in ("docx", "pdf"):
            raise InputError("仅支持 Word 和 PDF。")
        version = resumes.version(version_id)
        content = docx_bytes(version) if format == "docx" else pdf_bytes(version)
        media = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if format == "docx"
            else "application/pdf"
        )
        return Response(
            content,
            media_type=media,
            headers={
                "Content-Disposition": f'attachment; filename="resume-v{version["version"]}.{format}"'
            },
        )
