"""隔离 PDF/图片解码。只生成页面图像，不做语义提取，不联网。"""

import io
import json
import sys
import warnings
from pathlib import Path

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 24_000_000


def save_image(image, directory, page):
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((1800, 2400))
    path = directory / f"{page}.jpg"
    with path.open("xb") as output:
        image.save(output, "JPEG", quality=92)
    return {"page": page, "file": path.name}


def main():
    content = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if not content or len(content) > 5 * 1024 * 1024:
        raise ValueError
    suffix, output = sys.argv[1:3]
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    pages = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        if suffix == ".pdf":
            import pypdfium2 as pdfium

            with pdfium.PdfDocument(content) as document:
                if not 1 <= len(document) <= 12:
                    raise ValueError
                for i in range(len(document)):
                    page = document[i]
                    try:
                        width, height = page.get_size()
                        if not (1 <= width <= 15000 and 1 <= height <= 15000):
                            raise ValueError
                        bitmap = page.render(scale=min(2.5, 1800 / width, 2400 / height))
                        try:
                            pages.append(save_image(bitmap.to_pil(), directory, i + 1))
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
        else:
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError
                pages.append(save_image(image, directory, 1))
    print(json.dumps({"ok": True, "pages": pages}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"ok": False}))
        sys.exit(1)
