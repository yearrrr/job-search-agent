from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .model import ModelError, ToolCall

DEMO_EVIDENCE = {
    "source_id": "fictional-profile:1",
    "kind": "fictional_demo",
    "text": "【虚构资料】示例同学使用 Python 编写过课程练习。",
}


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    query: str = Field(min_length=1, max_length=80)


DEMO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_demo_evidence",
            "description": "只查询明确虚构的工程演示资料。",
            "parameters": SearchArguments.model_json_schema(),
        },
    }
]


def execute_demo_tool(call: ToolCall) -> list[dict]:
    if call.name != "search_demo_evidence":
        raise ModelError("tool_rejected")
    try:
        arguments = SearchArguments.model_validate(call.arguments)
    except ValidationError:
        raise ModelError("tool_rejected") from None
    query = arguments.query.strip().casefold()
    if not query:
        raise ModelError("tool_rejected")
    return [dict(DEMO_EVIDENCE)] if query in DEMO_EVIDENCE["text"].casefold() else []
