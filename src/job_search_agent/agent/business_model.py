"""离线模型替身调用同一真实检索工具；仅用于验证工程流程，不代表模型效果。"""

import json
import re

from .model import ModelReply, ToolCall

ANCHORS = [
    "python",
    "fastapi",
    "sql",
    "git",
    "rag",
    "langgraph",
    "docker",
    "kubernetes",
    "embedding",
    "企业实习",
    "测试",
    "工具调用",
    "恢复",
]


class BusinessDemoModel:
    def complete(self, messages, *, tools=None, max_tokens=None):
        payload = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        if payload["phase"] == "draft":
            ids = list(payload["evidence"])
            return ModelReply(
                content=json.dumps(
                    {
                        "sections": [
                            {
                                "kind": kind,
                                "fact_ids": ids[:5],
                                "rationale": "优先展示已确认且可回查的经历；表述仍需人工核对。",
                            }
                            for kind in ("简历段落", "项目介绍", "自我介绍")
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        tool_messages = [m for m in messages if m["role"] == "tool"]
        if not tool_messages:
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        id=f"search-{i}",
                        name="search_confirmed_facts",
                        arguments={"query": "", "category": category, "limit": 8},
                    )
                    for i, category in enumerate(
                        ("education", "project", "skill", "experience", "other")
                    )
                ]
            )
        facts = {f["id"]: f for m in tool_messages for f in json.loads(m["content"])["facts"]}
        items = []
        for req in payload["requirements"]:
            text = req["text"].casefold()
            refs, status = [], "unknown"
            education = [f for f in facts.values() if f["category"] == "education"]
            if (
                "硕士" in text
                and education
                and not any("硕士" in f["text"] or "博士" in f["text"] for f in education)
            ):
                refs = [f["id"] for f in education if "本科" in f["text"]][:1]
                if refs:
                    status = "mismatch"
            elif "本科" in text and education:
                refs = [
                    f["id"]
                    for f in education
                    if "本科" in f["text"]
                    and all(y in f["text"] for y in re.findall(r"20\d\d", text))
                ][:1]
                if refs:
                    status = "supported"
            else:
                anchors = [a for a in ANCHORS if a in text]
                available = {}
                for anchor in anchors:
                    matches = []
                    for f in facts.values():
                        clauses = re.split(r"[；;。]", f["text"].casefold())
                        if any(
                            anchor in c
                            and not any(
                                n in c for n in ("未提供", "没有", "未测", "未做", "不熟悉", "不会")
                            )
                            for c in clauses
                        ):
                            matches.append(f["id"])
                    available[anchor] = matches
                if anchors and all(available.values()):
                    refs = list(dict.fromkeys(matches[0] for matches in available.values()))[:8]
                    status = "supported"
            reason = {
                "supported": "离线规则找到相关已确认事实；复杂要求是否完全满足仍需人工核对。",
                "mismatch": "岗位要求硕士及以上，已确认学历为本科，存在明确学历差异。",
                "unknown": "当前检索到的已确认事实不足以判断，不能据此认定没有该能力。",
            }[status]
            items.append(
                {
                    "requirement_id": req["id"],
                    "status": status,
                    "fact_ids": refs,
                    "reason": reason,
                    "question": f"关于“{req['text']}”，是否有可核实的经历或具体说明？"
                    if status == "unknown"
                    else "",
                }
            )
        return ModelReply(content=json.dumps({"items": items}, ensure_ascii=False))
