from __future__ import annotations

import base64
import json
import os
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import fitz


ROOT = Path(__file__).resolve().parent
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_PDF_BYTES * 2


def font_file() -> str | None:
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    return next((str(p) for p in candidates if p.is_file()), None)


def intersect_area(a: fitz.Rect, b: fitz.Rect) -> float:
    r = a & b
    return max(0, r.width) * max(0, r.height)


def replace_pdf_text(data: bytes, page_number: int, box: dict, value: str) -> bytes:
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF больше 100 МБ")
    if not value.strip():
        raise ValueError("Введите новый текст")
    if len(value) > 2000:
        raise ValueError("Новый текст слишком длинный")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("Не удалось прочитать PDF") from exc
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF защищён паролем")
    if page_number < 0 or page_number >= doc.page_count:
        doc.close()
        raise ValueError("Такой страницы нет")

    page = doc[page_number]
    try:
        x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError) as exc:
        doc.close()
        raise ValueError("Не удалось определить положение текста") from exc
    if not all(0 <= v <= 1.05 for v in (x, y, w, h)) or w <= 0 or h <= 0:
        doc.close()
        raise ValueError("Некорректная область текста")

    # The browser reports a box in the page's displayed orientation.
    displayed = page.rect
    rect = fitz.Rect(x * displayed.width, y * displayed.height,
                     (x + w) * displayed.width, (y + h) * displayed.height)
    bounds = page.rect
    if page.rotation:
        rect = rect * page.derotation_matrix
        bounds = page.cropbox
    rect = rect & bounds
    if rect.is_empty or rect.is_infinite:
        doc.close()
        raise ValueError("Не удалось определить область текста")
    rect = fitz.Rect(rect.x0 - 0.8, rect.y0 - 0.8,
                     rect.x1 + 0.8, rect.y1 + 0.8) & bounds

    spans = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                sr = fitz.Rect(span.get("bbox", (0, 0, 0, 0)))
                if span.get("text", "").strip() and intersect_area(sr, rect) > 0:
                    spans.append((intersect_area(sr, rect), span))
    if not spans:
        doc.close()
        raise ValueError("В этой области нет распознаваемого текста. Для сканов нужен OCR")
    span = max(spans, key=lambda entry: entry[0])[1]
    color_int = int(span.get("color", 0))
    color = (((color_int >> 16) & 255) / 255,
             ((color_int >> 8) & 255) / 255,
             (color_int & 255) / 255)
    original_size = float(span.get("size", rect.height * 0.8))
    file = font_file()
    if not file and any(ord(ch) > 255 for ch in value):
        doc.close()
        raise ValueError("Для русского текста нужен системный шрифт Arial или DejaVu Sans")

    # Remove the selected text from the PDF content stream. Preserve images and vector artwork.
    page.add_redact_annot(rect, fill=None)
    page.apply_redactions(images=0, graphics=0, text=0)

    start_size = min(original_size, max(5.5, rect.height * 0.9))
    font_name = "F0" if file else "helv"
    inserted = False
    size = start_size
    while size >= 4.5:
        spare = page.insert_textbox(
            rect,
            value,
            fontsize=size,
            fontname=font_name,
            fontfile=file,
            color=color,
            align=fitz.TEXT_ALIGN_LEFT,
            overlay=True,
        )
        if spare >= 0:
            inserted = True
            break
        size -= 0.5
    if not inserted:
        doc.close()
        raise ValueError("Текст не помещается в исходное место. Попробуйте короче")

    result = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_POST(self):
        if urlparse(self.path).path != "/api/replace":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_json(413, {"error": "Файл больше 100 МБ"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            encoded = payload.get("pdf", "")
            data = base64.b64decode(encoded, validate=True)
            result = replace_pdf_text(
                data,
                int(payload.get("page", -1)),
                payload.get("box", {}),
                str(payload.get("text", "")),
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(result)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except BrokenPipeError:
            return
        except Exception:
            self.send_json(500, {"error": "Ошибка при изменении PDF"})

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Never log document contents or replacement text.
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def main():
    server = None
    for port in range(8765, 8776):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("Не удалось найти свободный локальный порт")

    url = f"http://127.0.0.1:{server.server_port}/"
    print("Лист запущен локально:", url)
    print("Для остановки закройте это окно.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

