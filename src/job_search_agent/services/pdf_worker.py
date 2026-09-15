"""独立解析进程。只读标准输入；向父进程返回受限 JSON，不转发第三方异常。"""

import io
import json
import logging
import sys

from pypdf import PdfReader


def main():
    logging.disable(logging.CRITICAL)
    content = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    try:
        if len(content) > 5 * 1024 * 1024:
            raise ValueError
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            return {"ok": False, "error": "PDF 已加密，请解密后上传或粘贴文本。"}
        if len(reader.pages) > 30:
            return {"ok": False, "error": "PDF 超过 30 页，请拆分资料。"}
        pages = []
        for page in reader.pages:
            stream = page.get_contents()
            if stream and len(stream.get_data()) > 4 * 1024 * 1024:
                return {"ok": False, "error": "PDF 页面过于复杂，请改用文本。"}
            pages.append(page.extract_text() or "")
            if sum(map(len, pages)) > 120000:
                return {"ok": False, "error": "PDF 文本过长，请拆分资料。"}
        return {"ok": True, "pages": pages}
    except Exception:
        return {"ok": False, "error": "PDF 损坏或无法解析，请重新导出文字型 PDF 或粘贴文本。"}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False))
