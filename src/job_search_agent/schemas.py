"""边界校验集中在程序里；空白不是“不满足”。"""

from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Corpus = Literal["demo", "personal"]
Category = Literal[
    "education", "project", "skill", "experience", "award", "publication", "basic", "other"
]
STATUS_LABELS = {
    "saved": "已收藏",
    "preparing": "准备中",
    "applied": "已投递",
    "interviewing": "面试中",
    "closed": "已结束",
}
CATEGORY_LABELS = {
    "education": "学历",
    "project": "项目",
    "skill": "掌握技能",
    "experience": "实习 / 实践经历",
    "award": "获得奖项",
    "publication": "专利 / 论文",
    "basic": "基础个人信息",
    "other": "其他待归类",
}
FIELD_LABELS = {
    "responsibilities": "岗位职责",
    "requirements": "硬性条件",
    "nice_to_have": "加分项",
    "location": "工作地点",
    "salary": "薪资",
    "deadline": "截止日期",
}


class InputError(ValueError):
    """只携带程序预设的安全提示。"""


class Conflict(InputError):
    pass


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)
    company: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=150)
    job_code: str = Field(default="", max_length=100)
    batch: str = Field(default="", max_length=100)
    source_url: str = Field(default="", max_length=2000)
    raw_jd: str = Field(min_length=1, max_length=40000)
    corpus: Corpus = "demo"

    @field_validator("source_url")
    @classmethod
    def valid_url(cls, value):
        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ("https", "http")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("只接受不含账号密码的 HTTP(S) 链接。")
        return value


def bounded(value: str, limit: int, *, empty: bool = False) -> str:
    value = value.strip()
    if (not empty and not value) or len(value) > limit or "\x00" in value:
        raise InputError("内容为空、含无效字符或超过长度限制，请修改后重试。")
    return value


def valid_corpus(value: str) -> str:
    if value not in ("demo", "personal"):
        raise InputError("资料区无效。")
    return value


def valid_date(value: str) -> str | None:
    if not value.strip():
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise InputError("待办日期应为 YYYY-MM-DD，或留空。") from None
