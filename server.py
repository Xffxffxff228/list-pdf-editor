from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

import pymupdf as fitz


ROOT = Path(__file__).resolve().parent
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_TEXT_CHARS = 2000


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
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError("Новый текст слишком длинный")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("Не удалось прочитать PDF") from exc
    with doc:
        if doc.needs_pass:
            raise ValueError("PDF защищён паролем")
        if page_number < 0 or page_number >= doc.page_count:
            raise ValueError("Такой страницы нет")

        if not isinstance(box, dict):
            raise ValueError("Не удалось определить положение текста")
        try:
            coords = [float(box[k]) for k in ("x", "y", "w", "h")]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Не удалось определить положение текста") from exc
        if not all(math.isfinite(v) for v in coords):
            raise ValueError("Некорректная область текста")
        x, y, w, h = coords
        if (x < 0 or y < 0 or w <= 0 or h <= 0 or
                x + w > 1.05 or y + h > 1.05):
            raise ValueError("Некорректная область текста")

        page = doc[page_number]
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
            raise ValueError("В этой области нет распознаваемого текста. Для сканов нужен OCR")
        span = max(spans, key=lambda entry: entry[0])[1]
        color_int = int(span.get("color", 0))
        color = (((color_int >> 16) & 255) / 255,
                 ((color_int >> 8) & 255) / 255,
                 (color_int & 255) / 255)
        original_size = float(span.get("size", rect.height * 0.8))
        file = font_file()
        if not file and any(ord(ch) > 255 for ch in value):
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
            raise ValueError("Текст не помещается в исходное место. Попробуйте короче")

        return doc.tobytes(garbage=3, deflate=True)


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mjs": "text/javascript",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/replace":
            self.send_error(404)
            return
        expected_host = f"127.0.0.1:{self.server.server_port}"
        host = self.headers.get("Host", "").lower()
        if host not in {expected_host, f"localhost:{self.server.server_port}"}:
            self.send_json(403, {"error": "Запрос отклонён"})
            return
        try:
            origin = self.headers.get("Origin")
            if origin:
                parsed_origin = urlsplit(origin)
                if (parsed_origin.scheme != "http" or
                        parsed_origin.netloc.lower() != host or parsed_origin.path or
                        parsed_origin.query or parsed_origin.fragment):
                    self.send_json(403, {"error": "Запрос отклонён"})
                    return
            if self.headers.get_content_type() != "application/pdf":
                self.send_json(415, {"error": "Ожидается PDF-файл"})
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_json(400, {"error": "Некорректный запрос"})
                return
            length_header = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]+", length_header):
                self.send_json(400, {"error": "Некорректный размер файла"})
                return
            length = int(length_header)
            if length <= 0:
                self.send_json(400, {"error": "Файл пуст"})
                return
            if length > MAX_PDF_BYTES:
                self.send_json(413, {"error": "Файл больше 100 МБ"})
                return
            data = self.rfile.read(length)
            if len(data) != length:
                self.send_json(400, {"error": "Не удалось получить PDF целиком"})
                return
            page_header = self.headers.get("X-Pdf-Page", "")
            if not re.fullmatch(r"[0-9]+", page_header):
                raise ValueError("Некорректный номер страницы")
            box = json.loads(self.headers.get("X-Pdf-Box", ""))
            text_header = self.headers.get("X-Replace-Text", "")
            if re.search(r"%(?![0-9a-fA-F]{2})", text_header):
                raise ValueError("Некорректный текст замены")
            value = unquote_to_bytes(text_header).decode("utf-8")
            if len(value) > MAX_TEXT_CHARS:
                raise ValueError("Новый текст слишком длинный")
            result = replace_pdf_text(
                data,
                int(page_header),
                box,
                value,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(result)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(result)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
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
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, fmt, *args):
        # Never log document contents or replacement text.
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Требуется Python 3.10 или новее")

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

