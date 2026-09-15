"""任务页面与动作；GET 只读，POST 经主应用同源/CSRF 校验后才运行。"""

from fastapi import BackgroundTasks, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .agent.contracts import RESULT_LABELS, TASK_ERRORS, TASK_LABELS
from .agent.workflow import review_token
from .schemas import InputError
from .services.documents import Documents
from .services.drafts import Drafts
from .services.tasks import Tasks


def install_task_routes(app, database, settings, templates, render, form, integer):
    tasks, drafts, documents = Tasks(database, settings), Drafts(database), Documents(database)
    templates.env.globals.update(
        task_labels=TASK_LABELS, result_labels=RESULT_LABELS, task_errors=TASK_ERRORS
    )

    @app.post("/jobs/{job_id}/tasks")
    async def create_task(request: Request, job_id: str, background_tasks: BackgroundTasks):
        data = await form(request)
        result = tasks.create(job_id, data.get("engine"), data.get("operation_id"))
        background_tasks.add_task(tasks.run, result["id"])
        return RedirectResponse(f"/tasks/{result['id']}", status_code=303)

    @app.get("/tasks/{task_id}")
    def task_page(request: Request, task_id: str):
        task = tasks.detail(task_id)
        snapshot = task["view"].get("snapshot") or task["snapshot"]
        wait = task["view"].get("wait")
        answer = documents.detail(wait["document_id"]) if wait and wait["kind"] == "fact" else None
        return render(
            request,
            "task.html",
            snapshot["corpus"],
            task=task,
            snapshot=snapshot,
            answer=answer,
            review_token=review_token(answer) if answer else "",
            requirements={r["id"]: r for r in snapshot["requirements"]},
            mode=task["mode"],
        )

    @app.get("/tasks/{task_id}/status")
    def task_status(task_id: str):
        return JSONResponse(tasks.status(task_id))

    @app.post("/tasks/{task_id}/respond")
    async def task_respond(request: Request, task_id: str, background_tasks: BackgroundTasks):
        data = await form(request)
        tasks.submit(
            task_id,
            integer(data),
            data.get("action"),
            str(data.get("text", "")),
            data.get("category"),
            0,
            data.get("operation_id"),
            str(data.get("review_token", "")),
        )
        if data.get("action") != "later":
            background_tasks.add_task(tasks.run, task_id)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @app.post("/tasks/{task_id}/draft")
    async def draft_update(request: Request, task_id: str, background_tasks: BackgroundTasks):
        data = await form(request)
        action = data.get("action")
        if action not in ("edit", "confirm"):
            raise InputError("草稿操作无效。")
        op = data.get("operation_id")
        if not isinstance(op, str) or not 8 <= len(op) <= 80:
            raise InputError("操作标识无效，请刷新。")
        # 当前编辑和确认指令一起提交；第二步出错时两者一并回滚。
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = drafts.edit(
                task_id,
                integer(data, "draft_revision"),
                str(data.get("content", "")),
                op + ":edit",
                connection=connection,
            )
            if action == "confirm":
                tasks.submit(
                    task_id,
                    integer(data),
                    "confirm_draft",
                    "",
                    "",
                    result["revision"],
                    op + ":confirm",
                    connection=connection,
                )
        if action == "confirm":
            background_tasks.add_task(tasks.run, task_id)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @app.get("/materials/{material_id}")
    def material_page(request: Request, material_id: str):
        material = drafts.detail(material_id)
        return render(
            request,
            "material.html",
            material["metadata"]["snapshot"]["corpus"],
            material=material,
            mode=material["metadata"]["mode"],
        )

    @app.get("/materials/{material_id}/export/{extension}")
    def export_material(material_id: str, extension: str):
        if extension not in ("md", "txt"):
            raise InputError("只支持 Markdown 或 TXT 导出。")
        material = drafts.detail(material_id)
        return Response(
            content=material["content"],
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="job-material-v{material["version"]}.{extension}"'
            },
        )
