"""读取唯一完整 JSON 围栏；不拼接多段、不修补截断，也不执行附带说明。"""

import re


def json_content(content):
    if not isinstance(content, str):
        return ""
    text = content.strip()
    fence = chr(96) * 3
    # 部分模型在唯一 JSON 块前附一句进度说明。只接受无其他对象的简短前言，
    # 全部业务字段仍经过严格 schema 和来源校验；多对象或多围栏一律失败。
    if text.count(fence) == 2:
        match = re.fullmatch(
            r"[^{}" + chr(96) + r"]{0,2000}" + fence + r"(?:json)?\s*\n(.*)\n" + fence,
            text,
            flags=re.DOTALL,
        )
        if match:
            return match.group(1).strip()
    if text.startswith(fence + "json\n") and text.endswith("\n" + fence):
        return text[len(fence) + 5 : -len(fence)].strip()
    if text.startswith(fence + "\n") and text.endswith("\n" + fence):
        return text[len(fence) + 1 : -len(fence)].strip()
    return text
