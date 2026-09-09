import json
import logging
import os
import re
import tempfile
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_FILE_SIZE = 20 * 1024 * 1024

SERVICE_LINE = re.compile(
    r"^\s*(?:"
    r"(?:Channel|Group|Chat) (?:created|photo changed|title changed)|"
    r"(?:Канал|Группа|Чат) (?:создан|изменил(?:а)? фото|изменил(?:а)? название)|"
    r"(?:Создан(?:а)? (?:канал|группа|чат)|Изменено фото|Изменено название)"
    r")\.?\s*$",
    re.IGNORECASE,
)
MEDIA_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
REACTION_LINE = re.compile(
    r"^\s*(?:[👍👎❤️❤🔥🥰👏😁🤔🤯😱😢🎉💯🙏🤩😡🤬🤮💩👀🤝👌🕊️💊⚡️⭐️✨😈🙈🆒☃️☠️🗿]+"
    r"(?:\s*\d+)?\s*)+$"
)
TIME_LINE = re.compile(r"^\s*(?:\*\*)?(\d{1,2}:\d{2})(?:\*\*)?\s*$")
DATE_LINE = re.compile(
    r"^\s*(?:#{1,6}\s*)?"
    r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})\s*$"
)
HEADING_LINE = re.compile(r"^\s*#\s+(.+?)\s*$")
INVALID_FILENAME = re.compile(r"[^\w. -]+", re.UNICODE)


def clean_markdown(text):
    source_bytes = len(text.encode("utf-8"))
    lines = []
    removed = 0
    previous_blank = True

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        if SERVICE_LINE.match(line) or MEDIA_LINE.match(line) or REACTION_LINE.match(line):
            removed += 1
            continue

        date = DATE_LINE.match(line)
        if date:
            line = f"## {date.group(1)}"

        time = TIME_LINE.match(line)
        if time:
            line = f"*{time.group(1)}*"

        blank = not line.strip()
        if blank and previous_blank:
            continue

        lines.append(line)
        previous_blank = blank

    while lines and not lines[-1].strip():
        lines.pop()

    result = "\n".join(lines)
    if result:
        result += "\n"

    return result, source_bytes, len(result.encode("utf-8")), removed


def output_filename(original_name, text):
    stem = Path(original_name).stem or "telegram-export"

    for line in text.splitlines():
        heading = HEADING_LINE.match(line)
        if heading and not DATE_LINE.match(heading.group(1)):
            stem = heading.group(1)
            break

    stem = INVALID_FILENAME.sub("-", stem).strip(" .-")
    return f"{(stem or 'telegram-export')[:100]}.clean.md"


def telegram_json(method, payload):
    request = urllib.request.Request(
        f"{API_URL}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except Exception:
        logger.exception("Telegram API error: %s", method)
        return None


def send_message(chat_id, text):
    telegram_json("sendMessage", {"chat_id": chat_id, "text": text})


def send_document(chat_id, path, filename, caption):
    boundary = uuid.uuid4().hex
    file_content = path.read_bytes()

    fields = [
        ("chat_id", str(chat_id).encode("utf-8")),
        ("caption", caption.encode("utf-8")),
    ]

    body = bytearray()
    for key, value in fields:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(value)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
    )
    body.extend(b"Content-Type: text/markdown\r\n\r\n")
    body.extend(file_content)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"{API_URL}/sendDocument",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    with urllib.request.urlopen(request, timeout=90):
        pass


def download_file(file_id, destination):
    result = telegram_json("getFile", {"file_id": file_id})
    if not result or not result.get("ok"):
        return False

    remote_path = result["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{remote_path}"

    try:
        urllib.request.urlretrieve(url, destination)
        return True
    except Exception:
        logger.exception("Unable to download Telegram file")
        return False


def process_document(chat_id, document):
    file_name = document.get("file_name", "telegram-export.md")

    if Path(file_name).suffix.lower() != ".md":
        send_message(
            chat_id,
            "Пришли, пожалуйста, Markdown-файл .md — выгрузку из Telegram Desktop.",
        )
        return

    if document.get("file_size", 0) > MAX_FILE_SIZE:
        send_message(chat_id, "Файл больше 20 МБ. Для Vercel сейчас доступны файлы до 20 МБ.")
        return

    send_message(chat_id, "⏳ Очищаю Telegram Markdown…")

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.md"

        if not download_file(document["file_id"], source):
            send_message(chat_id, "Не удалось скачать файл из Telegram.")
            return

        try:
            raw_text = source.read_text(encoding="utf-8-sig")
            cleaned, before, after, removed = clean_markdown(raw_text)
            result_name = output_filename(file_name, raw_text)
            output = Path(directory) / result_name
            output.write_text(cleaned, encoding="utf-8")

            percent = round((1 - after / before) * 100) if before else 0
            send_document(
                chat_id,
                output,
                result_name,
                f"Готово: {before} → {after} байт (−{percent}%). Удалено служебных строк: {removed}.",
            )
        except UnicodeDecodeError:
            send_message(chat_id, "Не удалось прочитать файл как UTF-8 Markdown.")
        except Exception:
            logger.exception("Cleanup failed")
            send_message(chat_id, "Ошибка при очистке. Попробуйте отправить файл ещё раз.")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

        try:
            update = json.loads(body)
        except json.JSONDecodeError:
            return

        message = update.get("message")
        if not message:
            return

        chat_id = message["chat"]["id"]

        if message.get("text", "").startswith("/start"):
            send_message(
                chat_id,
                "Привет! Пришли .md-выгрузку Telegram Desktop. "
                "Я уберу служебные сообщения, фото-превью и реакции, "
                "а затем верну файл с окончанием .clean.md. Исходник не изменяется.",
            )
        elif "document" in message:
            process_document(chat_id, message["document"])

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"tg-knowledge"}')
