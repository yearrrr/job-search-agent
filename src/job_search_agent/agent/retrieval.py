"""绑定资料区及已确认事实版本的只读工具；模型不能传数据库、路径或资料区。"""

import json
import re
from typing import Literal

from pydantic import Field, ValidationError

from ..services.profile import Profile
from .contracts import Strict
from .model import ModelError


class Search(Strict):
    query: str = Field(max_length=100)
    category: Literal[
        "all",
        "education",
        "project",
        "skill",
        "experience",
        "award",
        "publication",
        "basic",
        "other",
    ]
    limit: int = Field(ge=1, le=8)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_confirmed_facts",
            "description": "检索本任务资料区的已确认事实。可用空 query 按分类查询；返回事实 ID、版本与原文片段 ID。没有结果表示未知，不能推断没有能力。每次最多 8 条。",
            "parameters": Search.model_json_schema(),
        },
    }
]


def terms(text):
    words = re.findall(r"[a-z][a-z0-9+#.]*|[\u4e00-\u9fff]+", text.casefold())
    return set(w for w in words if not re.fullmatch(r"[\u4e00-\u9fff]+", w)) | {
        w[i : i + 2]
        for w in words
        if re.fullmatch(r"[\u4e00-\u9fff]+", w)
        for i in range(max(1, len(w) - 1))
    }


def compact_fact(f):
    return {
        "id": f["id"],
        "revision": f["revision"],
        "category": f["category"],
        "text": f["text"],
        "document_id": f["document_id"],
        "name": f["name"],
        "sources": [
            {"id": s["id"], "page": s["page"], "line": s["line"], "text": s["text"]}
            for s in f["sources"]
        ],
    }


def execute_search(database, snapshot, name, arguments):
    try:
        if name != "search_confirmed_facts":
            raise ValueError
        args = Search.model_validate(arguments)
        if args.query and not args.query.strip():
            raise ValueError
    except (ValueError, ValidationError):
        raise ModelError("tool_rejected") from None
    # 真实查询当前数据库；旧版本、撤回确认、笔记、其他资料区和材料版本均不能返回。
    allowed = {f["id"]: f for f in snapshot["facts"]}
    candidates = []
    query_terms = terms(args.query)
    for row in Profile(database).confirmed(snapshot["corpus"]):
        old = allowed.get(row["id"])
        if not old or old["revision"] != row["revision"] or old["text"] != row["text"]:
            continue
        if args.category != "all" and row["category"] != args.category:
            continue
        score = len(query_terms & terms(row["text"])) if query_terms else 1
        if score:
            candidates.append((score, compact_fact(row)))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    found, size = [], 0
    for _, fact in candidates:
        item_size = len(json.dumps(fact, ensure_ascii=False))
        if size + item_size > 8000 or len(found) >= args.limit:
            continue
        size += item_size
        found.append(fact)
    return {"facts": found, "matched": len(candidates), "omitted": len(candidates) - len(found)}
