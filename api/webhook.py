import json
import os
import re
import tempfile
import urllib.request
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_FILE_SIZE = 20 * 1024 * 1024

SERVICE_LINE = re.compile(
    r"^\s*(?:"
    r"(?:Channel|Group|Chat).*(?:created|photo changed|title changed)|"
    r"(?:Канал|Группа|Чат).*(?:создан|изменил(?:а)? фото|изменил(?:а)? название)|"
    r"(?:Создан(?:а)? (?:канал|группа|чат)|Изменено фото|Изменено название)"
    r").*$",
    re.IGNORECASE,
)
MEDIA_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
REACTION_LINE = re.compile(
    r"^\s*(?:[👍👎❤️❤🔥🥰👏😁🤔🤯😱😢🎉💯🙏🤩😡🤬🤮💩👀🤝👌🕊️💊⚡️⭐️✨😈🙈🆒☃️☠️🗿]+"
    r"(?:\s*\d+)?\s*)+$"
)
TIME_LINE = re.compile(r"^\s*(?:\*\*)?(\d{1,2}:\d{2})(?:\*\*)?\s*$")
NUMBER_DATE = re.compile(
    r"^\s*(?:#{1,6}\s*)?"
    r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})\s*$"
)
TEXT_DATE = re.compile(r"^\s*(\d{1,2}\s+[A-Za-zА-Яа-яЁё]+\s+\d{4})\s*$")
HEADING = re.compile(r"^\s*#\s+(.+?)\s*$")
BAD_FILENAME = re.compile(r"[^\w. -]+", re.UNICODE)

ATTACHMENT_NAME = re.compile(
    r"^.+\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z|txt|csv|epub)$",
    re.IGNORECASE,
)
FILE_SIZE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB)\s*$", re.IGNORECASE)
URL_LINE = re.compile(r"https?://", re.IGNORECASE)


def clean_markdown(text):
    before = len(text.encode("utf-8"))
    raw = [line.rstrip() for line in text.splitlines()]
    removed = 0

    counts = Counter(
        line for line in raw
        if line.strip()
        and len(line) < 120
        and not line.startswith(("http://", "https://", "#", "*", "—", "@"))
        and not NUMBER_DATE.match(line)
        and not TEXT_DATE.match(line)
        and not TIME_LINE.match(line)
    )
    channel_titles = {line for line, count in counts.items() if count >= 3}

    output = []
    previous_blank = True
    title_written = False
    source_written = False
    index = 0

    while index < len(raw):
        line = raw[index]
        next_line = raw[index + 1] if index + 1 < len(raw) else ""

        # Первое техническое имя экспортированного файла.
        if index == 0 and re.search(r"\s+\d+$", line):
            removed += 1
            index += 1
            continue

        # Блок недоступного вложения:
        # «Название.pdf» + «1.3 MB» + один короткий анонс без URL.
        if ATTACHMENT_NAME.match(line) and FILE_SIZE.match(next_line):
            end = index + 2
            block = [line, next_line]

            while end < len(raw) and raw[end].strip():
                block.append(raw[end])
                end += 1

            # Удаляем только короткие блоки без ссылок.
            # Длинный текст после файла сохраняем как обычный пост.
            has_url = any(URL_LINE.search(item) for item in block)
            if not has_url and len(block) <= 3:
                removed += len(block)
                index = end
                continue

            # Если текст длиннее, убираем только имя файла и размер.
            removed += 2
            index += 2
            continue

        if line == "G":
            removed += 1
            index += 1
            continue

        # Повторяющийся footer: «—» и «@канал — описание».
        if line == "—" and next_line.startswith("@") and "—" in next_line:
            if not source_written:
                output.append(f"*Источник: {next_line.split('—', 1)[0].strip()}*")
                previous_blank = False
                source_written = True
            removed += 2
            index += 2
            continue

        if SERVICE_LINE.match(line) or MEDIA_LINE.match(line) or REACTION_LINE.match(line):
            removed += 1
            index += 1
            continue

        if line in channel_titles:
            if not title_written:
                output.append(f"# {line}")
                previous_blank = False
                title_written = True
            else:
                removed += 1
            index += 1
            continue

        number_date = NUMBER_DATE.match(line)
        text_date = TEXT_DATE.match(line)
        time = TIME_LINE.match(line)

        if number_date:
            line = f"## {number_date.group(1)}"
        elif text_date:
            line = f"## {text_date.group(1)}"
        elif time:
            line = f"*{time.group(1)}*"

        blank = not line.strip()
        if blank and previous_blank:
            index += 1
            continue

        output.append(line)
        previous_blank = blank
        index += 1

    # Удаляем даты, в которых после очистки нет постов.
    result_lines = []
    for position, line in enumerate(output):
        if line.startswith("## "):
            following = output[position + 1] if position + 1 < len(output) else ""
            if not following or following.startswith("## "):
                removed += 1
                continue
        result_lines.append(line)

    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    result = "\n".join(result_lines)
    if result:
        result += "\n"

    return result, before, len(result.encode("utf-8")), removed


def output_filename(original_name, text):
    stem = Path(original_name).stem or "telegram-export"

    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            stem = heading.group(1)
            break

    stem = BAD_FILENAME.sub("-", stem).strip(" .-")
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
        return None


def send_message(chat_id, text):
    telegram_json("sendMessage", {"chat_id": chat_id, "text": text})


def send_document(chat_id, path, filename, caption):
    boundary = uuid.uuid4().hex
    body = bytearray()

    for key, value in (("chat_id", str(chat_id)), ("caption", caption)):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
    )
    body.extend(b"Content-Type: text/markdown\r\n\r\n")
    body.extend(path.read_bytes())
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

    try:
        remote_path = result["result"]["file_path"]
        urllib.request.urlretrieve(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{remote_path}",
            destination,
        )
        return True
    except Exception:
        return False


def process_document(chat_id, document):
    file_name = document.get("file_name", "telegram-export.md")

    if Path(file_name).suffix.lower() != ".md":
        send_message(chat_id, "Пришли Markdown-файл .md из Telegram Desktop.")
        return

    if document.get("file_size", 0) > MAX_FILE_SIZE:
        send_message(chat_id, "Файл больше 20 МБ. Для Vercel доступны файлы до 20 МБ.")
        return

    send_message(chat_id, "⏳ Очищаю Telegram Markdown…")

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.md"

        if not download_file(document["file_id"], source):
            send_message(chat_id, "Не удалось скачать файл из Telegram.")
            return

        try:
            raw = source.read_text(encoding="utf-8-sig")
            cleaned, before, after, removed = clean_markdown(raw)

            result_name = output_filename(file_name, raw)
            output = Path(directory) / result_name
            output.write_text(cleaned, encoding="utf-8")

            percent = round((1 - after / before) * 100) if before else 0
            send_document(
                chat_id,
                output,
                result_name,
                f"Готово: {before} → {after} байт (−{percent}%). "
                f"Удалено служебных строк: {removed}.",
            )
        except UnicodeDecodeError:
            send_message(chat_id, "Не удалось прочитать файл как UTF-8 Markdown.")
        except Exception:
            send_message(chat_id, "Ошибка при очистке. Попробуйте ещё раз.")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

        try:
            message = json.loads(body).get("message")
        except json.JSONDecodeError:
            return

        if not message:
            return

        chat_id = message["chat"]["id"]

        if message.get("text", "").startswith("/start"):
            send_message(
                chat_id,
                "Привет! Пришли .md-выгрузку Telegram Desktop. "
                "Я удалю технический шум, но сохраню посты, ссылки и полезный текст.",
            )
        elif "document" in message:
            process_document(chat_id, message["document"])

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"tg-knowledge"}')
