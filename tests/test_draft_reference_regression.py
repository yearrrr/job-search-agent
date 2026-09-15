import json

import pytest

from job_search_agent.agent.contracts import validate_draft
from job_search_agent.agent.model import ModelError

FACT_ID = "eab83455-1290-419d-9656-e89794d70777"


def plan(rationale):
    return json.dumps(
        {
            "sections": [
                {"kind": kind, "fact_ids": [FACT_ID], "rationale": rationale}
                for kind in ("简历段落", "项目介绍", "自我介绍")
            ]
        }
    )


@pytest.mark.parametrize("reference", [FACT_ID, FACT_ID[:8]])
def test_known_fact_reference_is_not_fabricated_number(reference):
    evidence = {FACT_ID: {"text": "使用 Python 编写测试。"}}
    assert validate_draft(plan(f"优先展示事实（{reference}），对应 r1。"), evidence, {"r1"})


@pytest.mark.parametrize(
    "rationale",
    [
        "依据未知事实（eab99999）",
        "依据事实（eab83455）提高效率 999%。",
        "依据事实（eab83455-1290-419d-9656-e89794d70778）",
        "新增 12345678 元收入。",
    ],
)
def test_unknown_reference_or_metric_still_rejected(rationale):
    with pytest.raises(ModelError):
        validate_draft(plan(rationale), {FACT_ID: {"text": "使用 Python。"}}, {"r1"})


def test_ambiguous_fact_prefix_cannot_hide_a_number():
    evidence = {
        FACT_ID: {"text": "使用 Python。"},
        "eab83455-1290-419d-9656-e89794d70778": {"text": "使用 SQLite。"},
    }
    with pytest.raises(ModelError):
        validate_draft(plan("依据事实（eab83455）。"), evidence)
