"""模型只返回建议；程序校验要求覆盖、引用范围和草稿事实。"""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .model import ModelError

RESULT_LABELS = {"supported": "有证据支持", "mismatch": "明确不符", "unknown": "资料不足"}
TASK_LABELS = {
    "queued": "待运行",
    "processing": "处理中",
    "waiting_input": "等待补充",
    "waiting_confirmation": "等待确认",
    "completed": "已完成",
    "failed": "运行停止",
}
TASK_ERRORS = {
    "tool_correction_limit": "已反馈 3 次工具参数纠正提示，模型仍提交无效请求，已停止继续调用；记录与预算保留。",
    "repeated_search": "模型连续重复检索同一资料，已停止无效调用；已有记录保留。",
    "uncertain_attempt": "上次请求的结果未能落盘；为避免重复调用，本任务已停止。请保留记录，必要时新建任务。",
    "stale_evidence": "已引用的事实或岗位字段发生变化，请基于最新资料重新分析。",
    "storage": "任务保存未完成。请检查本地存储，再点击继续未完成任务。",
    "internal": "任务遇到未预期错误，已有记录已保留，请检查测试或联系开发者。",
    "checkpoint_missing": "任务的检查点缺失，已停止重新执行。请恢复整个资料目录的备份，或基于当前资料新建任务。",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Finding(Strict):
    requirement_id: str = Field(min_length=1, max_length=20)
    status: Literal["supported", "mismatch", "unknown"]
    fact_ids: list[str] = Field(max_length=8)
    reason: str = Field(min_length=1, max_length=700)
    question: str = Field(max_length=500)


class Analysis(Strict):
    items: list[Finding] = Field(min_length=1, max_length=24)


class Section(Strict):
    kind: Literal["简历段落", "项目介绍", "自我介绍"]
    fact_ids: list[str] = Field(max_length=8)
    rationale: str = Field(min_length=1, max_length=500)


class DraftPlan(Strict):
    sections: list[Section] = Field(min_length=3, max_length=3)


def decode(content, schema):
    try:
        # 不修补截断 JSON，也不执行输出中的代码。
        if not isinstance(content, str) or len(content) > 24000:
            raise ValueError
        return schema.model_validate(json.loads(content)).model_dump()
    except (ValueError, TypeError, ValidationError, RecursionError):
        raise ModelError("invalid_output") from None


def validate_analysis(content, requirements, evidence):
    result = decode(content, Analysis)
    expected = {r["id"] for r in requirements}
    seen = [i["requirement_id"] for i in result["items"]]
    if len(seen) != len(set(seen)) or set(seen) != expected:
        raise ModelError("invalid_output")
    for item in result["items"]:
        refs = item["fact_ids"]
        if len(refs) != len(set(refs)) or not set(refs) <= evidence.keys():
            raise ModelError("invalid_output")
        if item["status"] == "unknown":
            if refs or not item["question"].strip():
                raise ModelError("invalid_output")
        elif not refs or item["question"]:
            raise ModelError("invalid_output")
    return result["items"]


def validate_draft(content, evidence, requirement_ids=()):
    result = decode(content, DraftPlan)
    if {s["kind"] for s in result["sections"]} != {"简历段落", "项目介绍", "自我介绍"}:
        raise ModelError("invalid_output")
    # 事实 UUID 及唯一的八位字母数字缩写也是来源标识；纯数字不能冒充标识绕过校验。
    uuid_pattern = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    facts = [key for key in evidence if re.fullmatch(uuid_pattern, key)]
    aliases = set(facts)
    for key in facts:
        prefix = key[:8]
        if re.search("[a-f]", prefix) and sum(k.startswith(prefix) for k in facts) == 1:
            aliases.add(prefix)
    for section in result["sections"]:
        refs = section["fact_ids"]
        if len(set(refs)) != len(refs) or not set(refs) <= evidence.keys():
            raise ModelError("invalid_output")
        # 建议说明也不允许引入没有出处的数字。
        # r1 等已知要求编号是引用，不是凭空新增的业绩数字；未知编号仍拒绝。
        rationale = re.sub(
            rf"(?<![A-Za-z0-9_-])(?:{uuid_pattern}|[0-9a-f]{{8}})(?![A-Za-z0-9_-])",
            lambda match: "" if match[0] in aliases else match[0],
            section["rationale"],
        )
        rationale = re.sub(
            r"(?<![A-Za-z0-9_])r\d+(?![A-Za-z0-9_])",
            lambda match: "" if match[0] in requirement_ids else match[0],
            rationale,
        )
        numbers = set(re.findall(r"\d+(?:\.\d+)?", rationale))
        source_numbers = set(
            re.findall(r"\d+(?:\.\d+)?", "\n".join(evidence[r]["text"] for r in refs))
        )
        if not numbers <= source_numbers:
            raise ModelError("invalid_output")
    return result["sections"]
