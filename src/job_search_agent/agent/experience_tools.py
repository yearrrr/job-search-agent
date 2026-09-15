"""绑定档案快照的三项只读工具：摘要、完整经历和原始证据分层返回。"""

from typing import Literal

from pydantic import Field, ValidationError

from ..schemas import CATEGORY_LABELS
from ..services.common import digest
from ..services.profile import Profile
from ..services.tasks import TaskStopped, ensure_fresh
from .contracts import Strict
from .model import ModelError
from .retrieval import compact_fact, terms

Category = Literal[
    "education", "project", "skill", "experience", "award", "publication", "basic", "other"
]


class ExperienceSearch(Strict):
    query: str = Field(max_length=200)
    categories: list[Category] = Field(min_length=1, max_length=8)
    limit: int = Field(ge=1, le=12)


class Details(Strict):
    experience_ids: list[str] = Field(min_length=1, max_length=8)


class Evidence(Strict):
    fact_ids: list[str] = Field(min_length=1, max_length=8)


CONTRACTS = {
    "search_experiences": ExperienceSearch,
    "get_experience_details": Details,
    "get_evidence": Evidence,
}
DESCRIPTIONS = {
    "search_experiences": "在当前资料区已确认经历中按关键词和类别查询摘要；空 query 浏览分类。最多 12 条；省略时换查询范围。未知不等于不会。",
    "get_experience_details": "按经历 ID 读取完整事实和修订版本。只能使用搜索返回的 ID，每次最多 8 条。",
    "get_evidence": "取得已读取事实对应的原件位置和原文。每次最多 8 条；只有查过证据的事实可写入匹配依据。",
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": DESCRIPTIONS[name],
            "parameters": schema.model_json_schema(),
        },
    }
    for name, schema in CONTRACTS.items()
]


def validate_call(name, arguments):
    try:
        result = CONTRACTS[name].model_validate(arguments).model_dump()
        if name == "search_experiences":
            result["query"] = " ".join(result["query"].split()).casefold()
            result["categories"] = sorted(set(result["categories"]))
        else:
            key = "experience_ids" if name == "get_experience_details" else "fact_ids"
            result[key] = sorted(set(result[key]))
        return result
    except (KeyError, ValidationError, TypeError):
        raise ModelError("tool_rejected") from None


def facts_version(facts):
    return digest(sorted([(f["id"], f["revision"], digest(f)) for f in facts]))


def rejection_feedback(snapshot, name, arguments, discovered, detailed, correction):
    """只说明本任务已见过的合法选项，不替模型猜测 ID，也不泄露其他资料。"""
    error = {
        "code": "tool_rejected",
        "correction": correction,
        "message": "本次请求被拒绝，未返回任何经历或证据。请修正参数后重新调用，编号必须原样复制，不能自行生成。",
        "allowed_tools": list(CONTRACTS),
    }
    try:
        args = validate_call(name, arguments)
    except ModelError:
        error["reason"] = "工具名称或参数结构无效。"
        if name in CONTRACTS:
            error["expected_arguments"] = CONTRACTS[name].model_json_schema()
        return {"error": error}
    facts = {f["id"]: f for f in snapshot["facts"]}
    eligible = (discovered if name == "get_experience_details" else detailed) & facts.keys()
    key = "experience_ids" if name == "get_experience_details" else "fact_ids"
    ids = args.get(key, [])
    error["reason"] = "请求包含未搜索到的编号，或尚未读取详情就请求证据；整批未执行。"
    error["invalid_ids"] = [i[:100] for i in ids if i not in eligible]
    ordered = sorted(eligible, key=lambda i: (i not in ids, i))
    error["available_experiences"] = [
        {"id": i, "title": facts[i]["text"].splitlines()[0][:120], "category": facts[i]["category"]}
        for i in ordered[:32]
    ]
    error["available_count"] = len(eligible)
    if name == "get_evidence":
        error["read_details_first"] = [i for i in ids if i in discovered and i not in detailed]
    error["next_step"] = (
        "从可用列表或此前工具结果原样复制编号；没有所需经历时先 search_experiences，未读详情时先 get_experience_details。"
    )
    return {"error": error}


def fresh(database, snapshot):
    with database.connect() as c:
        ensure_fresh(c, snapshot)
    current = [compact_fact(f) for f in Profile(database).confirmed(snapshot["corpus"])]
    if facts_version(current) != facts_version(snapshot["facts"]):
        raise TaskStopped("stale_evidence")


def execute(database, snapshot, name, arguments, discovered, detailed):
    fresh(database, snapshot)
    args = validate_call(name, arguments)
    facts = {f["id"]: f for f in snapshot["facts"]}
    if name == "search_experiences":
        query_terms, found = terms(args["query"]), []
        for f in facts.values():
            if f["category"] not in args["categories"]:
                continue
            score = len(query_terms & terms(f["text"])) if query_terms else 1
            if score:
                found.append((score, f))
        found.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return {
            "experiences": [
                {
                    "id": f["id"],
                    "revision": f["revision"],
                    "category": f["category"],
                    "summary": f["text"][:350],
                }
                for _, f in found[: args["limit"]]
            ],
            "matched": len(found),
            "omitted": max(0, len(found) - args["limit"]),
        }
    ids = args["experience_ids" if name == "get_experience_details" else "fact_ids"]
    if not set(ids) <= facts.keys() or not set(ids) <= (
        discovered if name == "get_experience_details" else detailed
    ):
        raise ModelError("tool_rejected")
    if name == "get_experience_details":
        return {
            "facts": [
                {k: f[k] for k in ("id", "revision", "category", "text", "document_id", "name")}
                for i in ids
                for f in [facts[i]]
            ]
        }
    return {
        "evidence": [
            {"fact_id": i, "document_id": facts[i]["document_id"], "sources": facts[i]["sources"]}
            for i in ids
        ]
    }


ALL_CATEGORIES = list(CATEGORY_LABELS)
