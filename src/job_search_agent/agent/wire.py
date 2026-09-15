"""图像请求不把 Base64 当作自然语言长度，也不把图片数据写进调用日志。"""

import hashlib


def media_summary(value):
    if isinstance(value, list):
        return [media_summary(v) for v in value]
    if isinstance(value, dict):
        if value.get("type") == "image_url" and isinstance(value.get("image_url"), dict):
            url = value["image_url"].get("url")
            if isinstance(url, str) and url.startswith("data:image/"):
                return {
                    **value,
                    "image_url": {
                        **value["image_url"],
                        "url": {
                            "image_sha256": hashlib.sha256(url.encode()).hexdigest(),
                            "encoded_chars": len(url),
                        },
                    },
                }
        return {k: media_summary(v) for k, v in value.items()}
    return value
