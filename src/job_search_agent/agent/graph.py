"""实际 LangGraph 冒烟图：工具循环 → 持久化人工中断 → 幂等保存虚构结果。"""

import json
import re
from contextlib import contextmanager
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context

from ..config import Settings
from ..db import Database
from .model import DemoModel, Model, ModelError, ToolCall
from .tools import DEMO_TOOLS, execute_demo_tool


class SmokeState(TypedDict, total=False):
    task_id: str
    messages: list[dict]
    calls: list[dict]
    content: str
    model_requests: int
    tool_requests: int
    status: str
    error_code: str
    approved: bool


def build_graph(settings: Settings, database: Database, checkpointer, model: Model):
    def generate(state: SmokeState):
        if state["model_requests"] >= settings.max_model_calls:
            return {"status": "failed", "error_code": "budget"}
        count = state["model_requests"] + 1
        try:
            reply = model.complete(state["messages"], tools=DEMO_TOOLS)
        except ModelError as error:
            return {
                "model_requests": count,
                "status": "failed",
                "error_code": error.code,
            }
        calls = [call.model_dump() for call in reply.tool_calls]
        message = {"role": "assistant", "content": reply.content}
        if calls:
            message["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                    },
                }
                for c in calls
            ]
        return {
            "model_requests": count,
            "messages": state["messages"] + [message],
            "calls": calls,
            "content": reply.content or "",
            "status": "processing",
        }

    def run_tools(state: SmokeState):
        messages = list(state["messages"])
        count = state["tool_requests"]
        for raw in state["calls"]:
            if count >= settings.max_tool_calls:
                return {
                    "tool_requests": count,
                    "status": "failed",
                    "error_code": "budget",
                }
            count += 1  # 被拒绝的请求也占用预算。
            try:
                call = ToolCall.model_validate(raw)
                result = execute_demo_tool(call)
            except ModelError as error:
                return {
                    "tool_requests": count,
                    "status": "failed",
                    "error_code": error.code,
                }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        return {"messages": messages, "tool_requests": count}

    def route(state: SmokeState):
        if state.get("status") == "failed":
            return END
        return "tools" if state.get("calls") else "prepare_confirmation"

    def prepare_confirmation(state: SmokeState):
        return {"status": "waiting_confirmation"}

    def confirm(state: SmokeState):
        # 不在 interrupt 前生成或写业务结果；此节点恢复时可能重跑。
        approved = interrupt(
            {"message": "确认保存这条虚构工程演示结果？", "content": state["content"]}
        )
        if type(approved) is not bool:
            return {
                "approved": False,
                "status": "failed",
                "error_code": "invalid_request",
            }
        return {
            "approved": approved,
            "status": "processing" if approved else "cancelled",
        }

    def save(state: SmokeState):
        content = database.save_smoke_once(state["task_id"], state["content"])
        return {"status": "completed", "content": content}

    graph = StateGraph(SmokeState)
    for name, node in [
        ("model", generate),
        ("tools", run_tools),
        ("prepare_confirmation", prepare_confirmation),
        ("confirm", confirm),
        ("save", save),
    ]:
        graph.add_node(name, node)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route)
    graph.add_conditional_edges(
        "tools", lambda state: END if state.get("status") == "failed" else "model"
    )
    graph.add_edge("prepare_confirmation", "confirm")
    graph.add_conditional_edges("confirm", lambda state: "save" if state["approved"] else END)
    graph.add_edge("save", END)
    return graph.compile(checkpointer=checkpointer)


@contextmanager
def smoke_graph(settings: Settings, model: Model | None = None):
    database = Database(settings.data_dir)
    database.initialize()
    # 即使用户全局设置过追踪环境变量，本项目也不向第三方发送图状态。
    with (
        tracing_context(enabled=False),
        SqliteSaver.from_conn_string(
            str(database.data_dir / "checkpoints.sqlite3")
        ) as checkpointer,
    ):
        checkpointer.setup()
        yield build_graph(settings, database, checkpointer, model or DemoModel())


def run_smoke(settings: Settings, task_id: str, *, action: str, approved: bool = True) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", task_id):
        raise ValueError("演示任务 ID 只能包含 1–64 个字母、数字、下划线或连字符。")
    if action not in ("start", "status", "resume") or type(approved) is not bool:
        raise ValueError("演示操作或确认参数无效。")
    config = {"configurable": {"thread_id": task_id}, "recursion_limit": 300}
    with smoke_graph(settings) as graph:
        snapshot = graph.get_state(config)
        if not snapshot.values:
            if action != "start":
                raise ValueError("没有找到该演示任务，请先开始。")
            graph.invoke(
                {
                    "task_id": task_id,
                    "messages": [
                        {
                            "role": "user",
                            "content": "请查询虚构资料中的 Python 经历，生成工程演示说明。",
                        }
                    ],
                    "model_requests": 0,
                    "tool_requests": 0,
                    "status": "processing",
                },
                config,
            )
        elif action == "resume" and snapshot.next:
            if snapshot.values.get("status") == "waiting_confirmation":
                graph.invoke(Command(resume=approved), config)
            else:
                graph.invoke(None, config)
        # 重复 start/resume 不重新创建完成任务，也不重置预算。
        state = graph.get_state(config).values
        return {
            key: state.get(key)
            for key in (
                "task_id",
                "status",
                "content",
                "model_requests",
                "tool_requests",
                "error_code",
            )
        }
