"""发送边界只隐藏可识别的联系方式；原件和本地来源保持原样。"""

import json
import re

from .model import ModelError


def guard_secret(value, settings, code="invalid_output"):
    key = settings.api_key.get_secret_value().strip()
    if key and key in json.dumps(value, ensure_ascii=False):
        raise ModelError(code)


def mask_contacts(value):
    if isinstance(value, str):
        value = re.sub(
            r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])", "[邮箱已隐藏]", value
        )
        return re.sub(
            r"(?<![A-Za-z0-9_])(?:\+?86[- ]?)?1[3-9]\d{9}(?![A-Za-z0-9_])", "[手机号已隐藏]", value
        )
    if isinstance(value, list):
        return [mask_contacts(item) for item in value]
    if isinstance(value, dict):
        return {key: mask_contacts(item) for key, item in value.items()}
    return value
