#!/usr/bin/env python3
"""Lossless, resumable semantic triage for exported Telegram corpora.

The default `prepare` command is offline. `submit` uploads the generated JSONL to
Gemini Batch API only when the caller explicitly passes --confirm-cloud-upload.
Raw source rows are never edited; every model result is joined back by a stable
request key and checked against exact evidence quotes from the original text.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gemini-3.1-flash-lite"
API_ROOT = "https://generativelanguage.googleapis.com"
TOPICS = [
    "traffic", "ideas", "pain", "research", "audience", "validation", "product",
    "positioning", "content", "funnel", "sales", "partners", "retention", "metrics",
    "economics", "ai", "engineering", "ux", "management", "funding", "operations",
    "personal", "news", "other",
]
KINDS = [
    "METHOD", "RECOMMENDATION", "IDEA", "PAIN", "RESEARCH_SIGNAL", "TOOL", "CASE",
    "QUESTION", "OPINION", "CONTEXT", "NOISE",
]
TOPIC_TITLES = {
    "traffic": "Трафик — платный и органический", "ideas": "Идеи — поиск и формирование",
    "pain": "Боли и задачи клиентов", "research": "Исследование рынка и конкурентов",
    "audience": "Аудитория — сегменты и ICP", "validation": "Спрос — проверка гипотез",
    "product": "Продукт — MVP и приоритеты", "positioning": "Позиционирование и оффер",
    "content": "Контент и личный бренд", "funnel": "Воронки — конверсия и активация",
    "sales": "Продажи — аутрич и переговоры", "partners": "Партнёрства и сообщества",
    "retention": "Удержание и обратная связь", "metrics": "Метрики и эксперименты",
    "economics": "Монетизация и экономика", "ai": "ИИ — агенты и автоматизация",
    "engineering": "Разработка — технологии и инструменты", "ux": "Дизайн и пользовательский опыт",
    "management": "Команда — управление и процессы", "funding": "Инвестиции и рост",
    "operations": "Операции — платежи и правила", "personal": "Личная эффективность и обучение",
    "news": "Новости — кейсы и наблюдения", "other": "Прочее",
}

SYSTEM_PROMPT = """Ты проводишь сплошную смысловую разметку архива сообщений. Для КАЖДОЙ записи из массива входа верни ровно один результат с тем же id; не пропускай даже короткие, повторные, рекламные, неясные или служебные записи. Не склеивай записи и не добавляй факты из внешних источников. Различай то, что автор сообщения утверждает, и подтверждённый факт. Выделяй методы, рекомендации, идеи, боли/задачи, сигналы спроса, инструменты/сервисы, кейсы, вопросы, мнения, контекст и шум. Рекламное упоминание не считай независимой рекомендацией. Для каждой смысловой находки добавь evidence quote — точную непрерывную цитату из исходного текста, сохраняя символы и пунктуацию. Если фрагмент невозможно уверенно интерпретировать, укажи низкую уверенность и объясни неопределённость; не выдумывай. Теги можно исправить относительно machine_tags — они лишь подсказка, а не подтверждённая разметка. В поле summary пиши кратко по-русски, сохраняя смысл и условия из источника."""

RESULT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "records": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "topic_ids": {"type": "ARRAY", "items": {"type": "STRING", "enum": TOPICS}},
                    "kind": {"type": "STRING", "enum": KINDS},
                    "summary": {"type": "STRING"},
                    "findings": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "type": {"type": "STRING", "enum": ["method", "recommendation", "idea", "pain", "market_signal", "tool", "case", "warning", "question", "opinion"]},
                                "text": {"type": "STRING"},
                                "evidence_quote": {"type": "STRING"},
                                "promotional": {"type": "BOOLEAN"},
                                "needs_external_verification": {"type": "BOOLEAN"},
                            },
                            "required": ["type", "text", "evidence_quote", "promotional", "needs_external_verification"],
                        },
                    },
                    "tools": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "confidence": {"type": "NUMBER"},
                    "uncertainty": {"type": "STRING"},
                },
                "required": ["id", "topic_ids", "kind", "summary", "findings", "tools", "confidence", "uncertainty"],
            },
        }
    },
    "required": ["records"],
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_records(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else data.get("records")
    if not isinstance(records, list):
        raise ValueError("Input must be a JSON array or an object with a 'records' array")
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []

    def add(item: dict[str, Any], index: int, inherited: dict[str, Any] | None = None) -> None:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError(f"Record {index} has no string 'text' field")
        item = {**(inherited or {}), **item}
        rid = str(item.get("id") or f"R{index:06d}")
        if rid in seen:
            raise ValueError(f"Duplicate record id: {rid}")
        seen.add(rid)
        item["id"] = rid
        text = item["text"]
        actual_hash = sha256_bytes(text.encode("utf-8"))
        if item.get("sha256") and item["sha256"] != actual_hash:
            raise ValueError(f"Existing text SHA-256 mismatch for {rid}; source altered")
        item["sha256"] = actual_hash
        clean.append(item)
        variants = item.get("variants", [])
        if isinstance(variants, list):
            for variant_number, variant in enumerate(variants, 1):
                if not isinstance(variant, dict) or not isinstance(variant.get("text"), str):
                    continue
                variant = dict(variant)
                variant.setdefault("id", f"{rid}-V{variant_number:04d}")
                variant.setdefault("parent_id", rid)
                if "occurrences" not in variant:
                    variant["occurrences"] = item.get("occurrences", [])
                if "source_id" not in variant and variant.get("occurrences"):
                    variant["source_id"] = variant["occurrences"][0].get("source_id")
                variant.setdefault("topics", item.get("topics", []))
                variant.setdefault("primary_topic", item.get("primary_topic"))
                variant.setdefault("date", item.get("date"))
                add(variant, index, {"parent_id": rid})

    for index, item in enumerate(records, 1):
        add(item, index)
    metadata = {"summary": data.get("summary", {}), "sources": data.get("sources", [])} if isinstance(data, dict) else {}
    return clean, metadata


def collect_inputs(path: Path) -> tuple[list[Path], Path]:
    if path.is_file():
        return [path], path.parent
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".markdown", ".txt"})
        if not files:
            raise ValueError(f"No .md, .markdown or .txt exports found under {path}")
        return files, path
    raise ValueError(f"Input path does not exist: {path}")


def split_losslessly(text: str, max_chars: int) -> list[tuple[int, int]]:
    if max_chars < 256:
        raise ValueError("--chunk-chars must be at least 256")
    if not text:
        return [(0, 0)]
    spans = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            newline = text.rfind("\n", start + max_chars // 2, end)
            if newline >= start:
                end = newline + 1
        if end <= start:
            end = min(start + max_chars, len(text))
        spans.append((start, end))
        start = end
    return spans


def ingest(args: argparse.Namespace) -> None:
    input_path = Path(args.input).resolve()
    files, base = collect_inputs(input_path)
    records: list[dict[str, Any]] = []
    source_manifest = []
    for source_number, path in enumerate(files, 1):
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        relative = path.relative_to(base).as_posix()
        source_id = f"S{source_number:04d}"
        source_manifest.append({
            "source_id": source_id,
            "source_path": relative,
            "source_bytes": len(raw),
            "source_chars": len(text),
            "source_sha256": sha256_bytes(raw),
        })
        for part_number, (start, end) in enumerate(split_losslessly(text, args.chunk_chars), 1):
            piece = text[start:end]
            records.append({
                "id": f"{source_id}-C{part_number:05d}",
                "source_id": source_id,
                "source_path": relative,
                "char_start": start,
                "char_end": end,
                "line_start": text.count("\n", 0, start) + 1,
                "line_end": text.count("\n", 0, end) + (1 if end == start or not text[:end].endswith("\n") else 0),
                "topics": [],
                "text": piece,
                "sha256": sha256_bytes(piece.encode("utf-8")),
            })
        reconstructed = "".join(r["text"] for r in records if r["source_id"] == source_id).encode("utf-8")
        if reconstructed != raw:
            raise ValueError(f"Lossless segmentation check failed for {relative}")
    corpus = {
        "summary": {
            "source_files": len(files),
            "unique_source_texts": len({s["source_sha256"] for s in source_manifest}),
            "source_characters": sum(s["source_chars"] for s in source_manifest),
            "unique_fragments": len(records),
            "method": "lossless character-offset chunks; original text retained verbatim; no cleaning or deduplication",
        },
        "sources": source_manifest,
        "records": records,
    }
    out = Path(args.output).resolve()
    if out.exists():
        raise ValueError(f"Refusing to overwrite existing corpus: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, corpus)
    print(json.dumps({"output": str(out), "source_files": len(files), "records": len(records),
                      "source_characters": corpus["summary"]["source_characters"],
                      "status": "lossless segmentation verified; no data uploaded"}, ensure_ascii=False, indent=2))


def make_prompt(rows: list[dict[str, Any]]) -> str:
    payload = []
    for row in rows:
        payload.append({
            "id": row["id"],
            "source_id": row.get("source_id"),
            "source_title": row.get("source_title"),
            "date": row.get("date"),
            "line_start": row.get("line_start"),
            "line_end": row.get("line_end"),
            "machine_tags": row.get("topics", []),
            "text": row["text"],
        })
    return "Разметь все записи ниже. Каждый id должен появиться ровно один раз.\n" + json.dumps(payload, ensure_ascii=False)


def pack_rows(rows: list[dict[str, Any]], max_chars: int, max_records: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for row in rows:
        cost = len(row["text"]) + len(row["id"]) + 128
        if current and (used + cost > max_chars or len(current) >= max_records):
            groups.append(current)
            current, used = [], 0
        current.append(row)
        used += cost
    if current:
        groups.append(current)
    return groups


def make_request(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "key": key,
        "request": {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": make_prompt(rows)}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": RESULT_SCHEMA,
            },
        },
    }


def prepare(args: argparse.Namespace) -> None:
    source = Path(args.input).resolve()
    out = Path(args.run_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError(f"Run directory is not empty: {out}")
    rows, source_summary = load_records(source)
    canonical_rows: list[dict[str, Any]] = []
    duplicate_map: dict[str, list[str]] = {}
    canonical_by_hash: dict[str, dict[str, Any]] = {}
    for row in rows:
        digest = row["sha256"]
        canonical = canonical_by_hash.get(digest)
        if canonical and canonical["text"] == row["text"]:
            duplicate_map[canonical["id"]].append(row["id"])
        else:
            canonical_by_hash[digest] = row
            canonical_rows.append(row)
            duplicate_map[row["id"]] = [row["id"]]
    groups = pack_rows(canonical_rows, args.group_chars, args.group_records)
    id_map: dict[str, list[str]] = {}
    with (out / "source-snapshot.jsonl").open("w", encoding="utf-8", newline="\n") as snap:
        for row in rows:
            snap.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    write_json(out / "duplicate-map.json", duplicate_map)
    with (out / "batch-input.jsonl").open("w", encoding="utf-8", newline="\n") as batch:
        for number, group in enumerate(groups, 1):
            key = f"group-{number:06d}"
            id_map[key] = [r["id"] for r in group]
            batch.write(json.dumps(make_request(key, group), ensure_ascii=False, separators=(",", ":")) + "\n")
    write_json(out / "request-map.json", id_map)
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_path": str(source),
        "input_sha256": sha256_file(source),
        "record_count": len(rows),
        "unique_content_records": len(canonical_rows),
        "exact_duplicate_records_reused": len(rows) - len(canonical_rows),
        "group_count": len(groups),
        "source_characters": sum(len(r["text"]) for r in rows),
        "source_summary": source_summary.get("summary", {}),
        "sources": source_summary.get("sources", []),
        "model": args.model,
        "batch_jsonl_sha256": sha256_file(out / "batch-input.jsonl"),
        "snapshot_sha256": sha256_file(out / "source-snapshot.jsonl"),
        "request_map_sha256": sha256_file(out / "request-map.json"),
        "duplicate_map_sha256": sha256_file(out / "duplicate-map.json"),
        "status": "prepared_offline",
        "human_reviewed": 0,
    }
    write_json(out / "manifest.json", manifest)
    print(json.dumps({"run_dir": str(out), "records": len(rows), "unique_texts_analyzed": len(canonical_rows),
                      "exact_duplicate_records_reused": len(rows) - len(canonical_rows), "requests": len(groups),
                      "source_chars": manifest["source_characters"], "batch_file_bytes": (out / "batch-input.jsonl").stat().st_size,
                      "status": "prepared; no data uploaded"}, ensure_ascii=False, indent=2))


def request_json(url: str, api_key: str, method: str = "GET", body: bytes | None = None,
                 headers: dict[str, str] | None = None, timeout: int = 120) -> tuple[bytes, Any]:
    request_headers = {"x-goog-api-key": api_key}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content = response.read()
            try:
                return content, json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return content, None
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace")
        raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail}") from exc


def upload_jsonl(path: Path, api_key: str) -> str:
    data = path.read_bytes()
    metadata = json.dumps({"file": {"display_name": path.stem}}, separators=(",", ":")).encode()
    req = urllib.request.Request(
        f"{API_ROOT}/upload/v1beta/files", data=metadata, method="POST",
        headers={
            "x-goog-api-key": api_key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": "application/jsonl",
            "Content-Type": "application/jsonl",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            upload_url = response.headers.get("x-goog-upload-url")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Gemini upload initialization failed (HTTP {exc.code})") from exc
    if not upload_url:
        raise RuntimeError("Gemini upload did not return a resumable URL")
    req = urllib.request.Request(upload_url, data=data, method="POST", headers={
        "Content-Length": str(len(data)),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
        "Content-Type": "application/jsonl",
    })
    with urllib.request.urlopen(req, timeout=600) as response:
        payload = json.loads(response.read().decode("utf-8"))
    name = payload.get("file", {}).get("name") or payload.get("name")
    if not name:
        raise RuntimeError("Gemini upload response did not contain a file name")
    return name


def submit(args: argparse.Namespace) -> None:
    if not args.confirm_cloud_upload:
        raise ValueError("Refusing cloud upload. Re-run with --confirm-cloud-upload after reviewing the free-tier data-use terms.")
    run = Path(args.run_dir).resolve()
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("batch_name"):
        raise ValueError(f"This run was already submitted as {manifest['batch_name']}; duplicate submits can create duplicate jobs")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise ValueError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment")
    if sha256_file(run / "batch-input.jsonl") != manifest["batch_jsonl_sha256"]:
        raise ValueError("Batch input changed after prepare; refusing upload")
    uploaded = upload_jsonl(run / "batch-input.jsonl", key)
    model = args.model or manifest.get("model", DEFAULT_MODEL)
    body = json.dumps({"batch": {"display_name": f"tg-knowledge-{run.name}",
                                  "input_config": {"file_name": uploaded}}}).encode()
    _, response = request_json(f"{API_ROOT}/v1beta/models/{urllib.parse.quote(model, safe='')}:batchGenerateContent",
                               key, "POST", body, {"Content-Type": "application/json"})
    batch_name = (response or {}).get("name")
    if not batch_name:
        raise RuntimeError("Batch creation response did not include a job name")
    manifest.update({"status": "submitted", "model": model, "uploaded_file": uploaded,
                     "batch_name": batch_name, "submitted_at_utc": datetime.now(timezone.utc).isoformat()})
    write_json(manifest_path, manifest)
    print(json.dumps({"batch_name": batch_name, "model": model, "status": "submitted"}, ensure_ascii=False, indent=2))


def poll(args: argparse.Namespace) -> None:
    run = Path(args.run_dir).resolve()
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch_name = manifest.get("batch_name")
    if not batch_name:
        raise ValueError("No batch_name in manifest; submit this run first")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise ValueError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment")
    url = f"{API_ROOT}/v1beta/{urllib.parse.quote(batch_name, safe='/')}"
    _, job = request_json(url, key)
    state = job.get("metadata", {}).get("state") or job.get("state") or "UNKNOWN"
    manifest["last_remote_state"] = state
    manifest["last_polled_at_utc"] = datetime.now(timezone.utc).isoformat()
    if state == "JOB_STATE_SUCCEEDED":
        result_file = ((job.get("response") or {}).get("responsesFile")
                       or (job.get("dest") or {}).get("fileName")
                       or (job.get("response") or {}).get("dest", {}).get("fileName"))
        if result_file:
            result_url = f"{API_ROOT}/download/v1beta/{urllib.parse.quote(result_file, safe='/')}:download?alt=media"
            result_bytes, _ = request_json(result_url, key, timeout=600)
            (run / "batch-results.jsonl").write_bytes(result_bytes)
            manifest["status"] = "results_downloaded"
            manifest["results_file"] = result_file
        else:
            write_json(run / "batch-inline-response.json", job.get("response", {}))
            manifest["status"] = "succeeded_no_file"
    elif state in {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}:
        manifest["status"] = state.lower()
        manifest["remote_error"] = job.get("error") or job.get("metadata", {}).get("error")
    else:
        manifest["status"] = "running"
    write_json(manifest_path, manifest)
    print(json.dumps({"batch_name": batch_name, "state": state,
                      "results_downloaded": (run / "batch-results.jsonl").exists(),
                      "error": manifest.get("remote_error")}, ensure_ascii=False, indent=2))


def extract_response_text(result: dict[str, Any]) -> str | None:
    response = result.get("response") or result.get("result") or {}
    if isinstance(response, str):
        return response
    if response.get("text"):
        return response["text"]
    candidates = response.get("candidates", [])
    for candidate in candidates:
        content = candidate.get("content", {})
        texts = [part.get("text", "") for part in content.get("parts", []) if isinstance(part, dict)]
        if texts:
            return "\n".join(texts)
    return None


def normalize_model_record(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    topics = item.get("topic_ids", [])
    findings = item.get("findings", [])
    errors: list[str] = []
    if not isinstance(topics, list) or any(t not in TOPICS for t in topics):
        errors.append("invalid_topic_ids")
    if item.get("kind") not in KINDS:
        errors.append("invalid_kind")
    if not isinstance(findings, list):
        findings = []
        errors.append("findings_not_array")
    quotes: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            errors.append("finding_not_object")
            continue
        if finding.get("type") not in {"method", "recommendation", "idea", "pain", "market_signal", "tool", "case", "warning", "question", "opinion"}:
            errors.append("invalid_finding_type")
        if not isinstance(finding.get("text"), str):
            errors.append("invalid_finding_text")
        quote = finding.get("evidence_quote") if isinstance(finding, dict) else None
        if not isinstance(quote, str) or not quote:
            errors.append("empty_evidence_quote")
        elif quote not in source["text"]:
            errors.append("evidence_quote_not_verbatim")
        else:
            quotes.append(quote)
    content_claim = item.get("kind") not in {"NOISE", "CONTEXT"} or bool(topics) or bool(findings)
    if content_claim and not quotes:
        errors.append("content_without_verbatim_evidence")
    confidence = item.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        errors.append("invalid_confidence")
    if not isinstance(item.get("summary"), str) or not isinstance(item.get("tools", []), list):
        errors.append("invalid_summary_or_tools")
    return {
        "id": source["id"],
        "source_sha256": source["sha256"],
        "source_chars": len(source["text"]),
        "source_id": source.get("source_id"),
        "occurrences": source.get("occurrences", []),
        "source_path": source.get("source_path"),
        "char_start": source.get("char_start"),
        "char_end": source.get("char_end"),
        "line_start": source.get("line_start"),
        "line_end": source.get("line_end"),
        "parent_id": source.get("parent_id"),
        "machine_topics": source.get("topics", []),
        "model_result": item,
        "exact_quote_count": len(quotes),
        "validation_errors": sorted(set(errors)),
        "status": "MODEL_RESULT_GROUNDED" if not errors else "NEEDS_REVIEW",
        "human_reviewed": False,
    }


def validate(args: argparse.Namespace) -> None:
    run = Path(args.run_dir).resolve()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    input_path = Path(manifest["input_path"])
    if input_path.exists() and sha256_file(input_path) != manifest["input_sha256"]:
        raise ValueError("Original input changed after prepare; refusing to join results")
    if sha256_file(run / "source-snapshot.jsonl") != manifest["snapshot_sha256"]:
        raise ValueError("Source snapshot SHA-256 mismatch")
    if sha256_file(run / "batch-input.jsonl") != manifest["batch_jsonl_sha256"]:
        raise ValueError("Batch input SHA-256 mismatch")
    sources: dict[str, dict[str, Any]] = {}
    with (run / "source-snapshot.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if sha256_bytes(row["text"].encode("utf-8")) != row["sha256"]:
                raise ValueError(f"Source text changed in snapshot: {row['id']}")
            sources[row["id"]] = row
    if len(sources) != manifest["record_count"]:
        raise ValueError("Snapshot record count does not match manifest")
    request_map = json.loads((run / "request-map.json").read_text(encoding="utf-8"))
    if sha256_file(run / "request-map.json") != manifest["request_map_sha256"]:
        raise ValueError("Request map SHA-256 mismatch")
    duplicate_map = json.loads((run / "duplicate-map.json").read_text(encoding="utf-8"))
    if sha256_file(run / "duplicate-map.json") != manifest["duplicate_map_sha256"]:
        raise ValueError("Duplicate map SHA-256 mismatch")
    all_expected = [rid for ids in request_map.values() for rid in ids]
    flattened = [rid for ids in duplicate_map.values() for rid in ids]
    if (len(all_expected) != len(set(all_expected)) or set(all_expected) != set(duplicate_map)
            or len(flattened) != len(set(flattened)) or set(flattened) != set(sources)
            or any(canonical not in ids for canonical, ids in duplicate_map.items())):
        raise ValueError("Request and duplicate maps do not account for every source record exactly once")
    result_path = Path(args.results) if args.results else run / "batch-results.jsonl"
    parsed: dict[str, dict[str, Any]] = {}
    group_errors: dict[str, str] = {}
    malformed_result_lines = 0
    unmatched_result_lines = 0
    with result_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                malformed_result_lines += 1
                continue
            if not isinstance(payload, dict):
                malformed_result_lines += 1
                continue
            metadata = payload.get("metadata")
            key = payload.get("key") or (metadata.get("key") if isinstance(metadata, dict) else None)
            if not key or key not in request_map:
                unmatched_result_lines += 1
                continue
            if key in parsed or key in group_errors:
                group_errors[key] = "duplicate_result_key"
                parsed.pop(key, None)
                continue
            if payload.get("error"):
                group_errors[key] = "api_error: " + json.dumps(payload["error"], ensure_ascii=False)
                continue
            text = extract_response_text(payload)
            if not text:
                group_errors[key] = "missing_response_text"
                continue
            try:
                data = json.loads(text)
                model_rows = data.get("records") if isinstance(data, dict) else data
                if not isinstance(model_rows, list):
                    raise ValueError("response has no records array")
                parsed[key] = {str(row.get("id")): row for row in model_rows if isinstance(row, dict)}
                if len(parsed[key]) != len(model_rows):
                    group_errors[key] = "missing_or_duplicate_model_id"
                    parsed.pop(key, None)
            except (json.JSONDecodeError, ValueError, AttributeError) as exc:
                group_errors[key] = f"invalid_json: {exc}"
    output_rows: list[dict[str, Any]] = []
    for key, expected_ids in request_map.items():
        group = parsed.get(key, {})
        group_error = group_errors.get(key)
        if not group_error and set(group) != set(expected_ids):
            group_error = "response_id_set_mismatch"
        for canonical_id in expected_ids:
            duplicate_ids = duplicate_map[canonical_id]
            if group_error:
                for rid in duplicate_ids:
                    output_rows.append({"id": rid, "source_sha256": sources[rid]["sha256"],
                                        "source_chars": len(sources[rid]["text"]),
                                        "status": "NEEDS_REVIEW", "human_reviewed": False,
                                        "validation_errors": [group_error]})
            else:
                for rid in duplicate_ids:
                    result_row = normalize_model_record(group[canonical_id], sources[rid])
                    if rid != canonical_id:
                        result_row["duplicate_content_of"] = canonical_id
                    result_row["duplicate_group_size"] = len(duplicate_ids)
                    output_rows.append(result_row)
    with (run / "validated-results.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in output_rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    write_json(run / "validation-summary.json", summarize(manifest, output_rows, len(parsed), len(group_errors), len(request_map),
                                                          malformed_result_lines, unmatched_result_lines))
    print(json.dumps(json.loads((run / "validation-summary.json").read_text(encoding="utf-8")), ensure_ascii=False, indent=2))


def summarize(manifest: dict[str, Any], rows: list[dict[str, Any]], parsed_groups: int,
              failed_groups: int, request_groups: int, malformed_lines: int = 0,
              unmatched_lines: int = 0) -> dict[str, Any]:
    status_counts = collections.Counter(r["status"] for r in rows)
    topics = collections.Counter(t for row in rows for t in (row.get("model_result") or {}).get("topic_ids", []))
    kinds = collections.Counter((row.get("model_result") or {}).get("kind", "UNCLASSIFIED") for row in rows)
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_files": len(manifest.get("sources", [])),
        "records_in_source": manifest["record_count"],
        "unique_texts_analyzed": manifest.get("unique_content_records", manifest["record_count"]),
        "exact_duplicate_records_reused": manifest.get("exact_duplicate_records_reused", 0),
        "records_with_a_result_row": len(rows),
        "records_with_model_response": sum("model_result" in r for r in rows),
        "records_model_grounded_by_exact_quote_check": status_counts.get("MODEL_RESULT_GROUNDED", 0),
        "records_needing_review_or_retry": status_counts.get("NEEDS_REVIEW", 0),
        "human_reviewed_records": sum(bool(r.get("human_reviewed")) for r in rows),
        "request_groups": request_groups,
        "request_groups_with_response": parsed_groups,
        "request_groups_with_errors": failed_groups,
        "malformed_result_lines": malformed_lines,
        "unmatched_result_lines": unmatched_lines,
        "model_response_coverage_percent": round(100 * sum("model_result" in r for r in rows) / max(1, manifest["record_count"]), 3),
        "record_accounting_percent": round(100 * len(rows) / max(1, manifest["record_count"]), 3),
        "source_text_sha256_verified": True,
        "topic_counts": dict(sorted(topics.items())),
        "kind_counts": dict(sorted(kinds.items())),
        "important_note": "MODEL_RESULT_GROUNDED means schema and exact evidence quotes passed automated checks. It is not a human review or external fact verification.",
    }


def make_dashboard(summary: dict[str, Any], results_path: Path, out_path: Path) -> None:
    topic_counts = summary.get("topic_counts", {})
    max_value = max(topic_counts.values(), default=1)
    bars = []
    for topic, count in sorted(topic_counts.items(), key=lambda pair: (-pair[1], pair[0])):
        width = round(520 * count / max_value)
        bars.append(f'<div class="barrow"><span>{html.escape(topic)}</span><div class="track"><i style="width:{width}px"></i></div><b>{count:,}</b></div>')
    stats = [
        ("Records", summary["records_in_source"]),
        ("Unique texts analyzed", summary["unique_texts_analyzed"]),
        ("Exact repeats reused", summary["exact_duplicate_records_reused"]),
        ("Model-grounded", summary["records_model_grounded_by_exact_quote_check"]),
        ("Needs review / retry", summary["records_needing_review_or_retry"]),
        ("Human reviewed", summary["human_reviewed_records"]),
        ("Model response coverage", f'{summary["model_response_coverage_percent"]}%'),
        ("Rows accounted", f'{summary["record_accounting_percent"]}%'),
    ]
    cards = "".join(f'<article><small>{html.escape(str(k))}</small><strong>{html.escape(str(v))}</strong></article>' for k, v in stats)
    note = html.escape(summary.get("important_note", ""))
    body = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Corpus review dashboard</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;color:#20252b;background:#f6f7f9}}h1{{font-size:28px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}article,.panel{{background:white;border:1px solid #e3e7eb;border-radius:12px;padding:16px}}article small{{display:block;color:#606975}}article strong{{display:block;font-size:24px;margin-top:5px}}.panel{{margin-top:18px}}.barrow{{display:grid;grid-template-columns:155px 1fr 75px;gap:10px;align-items:center;margin:8px 0}}.track{{background:#e9edf1;border-radius:9px;height:14px;overflow:hidden}}.track i{{display:block;height:100%;background:#3878d8;border-radius:9px}}.barrow b{{text-align:right}}.note{{border-left:4px solid #e0a229;padding:10px 14px;background:#fff9e8}}@media(max-width:600px){{.barrow{{grid-template-columns:100px 1fr 45px}}}}</style>
<body><h1>Corpus semantic-review dashboard</h1><p>Machine processing status, joined to the preserved source archive.</p><section class="cards">{cards}</section><section class="panel"><h2>Topic coverage</h2>{''.join(bars) or '<p>Waiting for validated results.</p>'}</section><section class="panel"><h2>Kind counts</h2><pre>{html.escape(json.dumps(summary.get('kind_counts', {}),ensure_ascii=False,indent=2))}</pre></section><p class="note">{note}</p><p>Evidence file: {html.escape(results_path.name)}. Human review count is reported separately.</p></body></html>"""
    out_path.write_text(body, encoding="utf-8")


def dashboard(args: argparse.Namespace) -> None:
    run = Path(args.run_dir).resolve()
    summary_path = run / "validation-summary.json"
    if not summary_path.exists():
        raise ValueError("Run validate first")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    make_dashboard(summary, run / "validated-results.jsonl", run / "dashboard.html")
    print(f"Wrote {run / 'dashboard.html'}")


def export_obsidian(args: argparse.Namespace) -> None:
    run = Path(args.run_dir).resolve()
    out = Path(args.output).resolve()
    summary_path = run / "validation-summary.json"
    if not summary_path.exists():
        raise ValueError("Run validate and dashboard first")
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"Obsidian output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    source_rows: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    with (run / "source-snapshot.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("source_id") and row.get("char_start") is not None:
                source_rows[row["source_id"]].append(row)
    source_links: dict[str, str] = {}
    source_files = {}
    for source in json.loads((run / "manifest.json").read_text(encoding="utf-8")).get("sources", []):
        if source.get("source_id") and source.get("source_path") is not None and source.get("source_sha256"):
            source_files[source["source_id"]] = source
    for source_id, source in source_files.items():
        rows = sorted(source_rows.get(source_id, []), key=lambda r: r["char_start"])
        content = "".join(r["text"] for r in rows)
        encoded = content.encode("utf-8")
        if len(content) != source["source_chars"] or sha256_bytes(encoded) != source["source_sha256"]:
            raise ValueError(f"Source reconstruction failed for {source_id}")
        name = Path(source["source_path"]).name or "export.md"
        safe_name = re.sub(r"[^\w.() -]+", "_", name, flags=re.UNICODE).strip(" .") or "export.md"
        if not Path(safe_name).suffix:
            safe_name += ".md"
        relative = f"Sources/{source_id}-{safe_name}"
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        source_links[source_id] = relative
    # If the input was an already-curated JSON corpus with no raw-source offsets,
    # keep every canonical/variant text in small, readable Markdown shards.
    if not source_files:
        raw_dir = out / "Raw records"
        raw_dir.mkdir(parents=True, exist_ok=True)
        snapshot = [json.loads(line) for line in (run / "source-snapshot.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        for offset in range(0, len(snapshot), args.shard_records):
            shard = snapshot[offset:offset + args.shard_records]
            blocks = ["# Исходные фрагменты — записи %s–%s\n" % (offset + 1, offset + len(shard))]
            for row in shard:
                fence = "`" * max(3, max((len(m.group(0)) for m in re.finditer(r"`+", row["text"])), default=0) + 1)
                blocks.append(f"\n## {row['id']}\n\n{fence}text\n{row['text']}{'' if row['text'].endswith(chr(10)) else chr(10)}{fence}\n")
            (raw_dir / f"records-{offset // args.shard_records + 1:04d}.md").write_text("".join(blocks), encoding="utf-8")
    results = [json.loads(line) for line in (run / "validated-results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    topic_rows: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    review_rows = []
    for row in results:
        result = row.get("model_result") or {}
        labels = result.get("topic_ids", [])
        for topic in labels:
            if topic in TOPIC_TITLES:
                topic_rows[topic].append(row)
        if row["status"] != "MODEL_RESULT_GROUNDED":
            review_rows.append(row)
    for topic, rows in topic_rows.items():
        directory = out / "Topics" / topic
        directory.mkdir(parents=True, exist_ok=True)
        for offset in range(0, len(rows), args.shard_records):
            shard = rows[offset:offset + args.shard_records]
            blocks = [f"# {TOPIC_TITLES[topic]} — часть {offset // args.shard_records + 1}\n\nЗаписей: {len(shard)}. Автоматическая смысловая разметка; см. статус и ссылки на источник.\n"]
            for row in shard:
                result = row.get("model_result") or {}
                rid = row["id"]
                confidence = result.get("confidence", "—")
                blocks.append(f"\n## {rid} · {result.get('kind', 'NEEDS_REVIEW')} · confidence {confidence}\n\n{result.get('summary', 'Нет результата модели.')}\n")
                if row.get("source_id"):
                    source = source_links.get(row["source_id"])
                    if source and row.get("line_start"):
                        blocks.append(f"\nИсточник: [{row['source_id']}, строки {row['line_start']}–{row.get('line_end', row['line_start'])}](../../{source})\n")
                    else:
                        blocks.append(f"\nИсточник: {row['source_id']} · id `{rid}`\n")
                for finding in result.get("findings", []):
                    marker = " · требуется внешняя проверка" if finding.get("needs_external_verification") else ""
                    promo = " · рекламный контекст" if finding.get("promotional") else ""
                    blocks.append(f"\n- **{finding.get('type', 'finding')}**{marker}{promo}: {finding.get('text', '')}\n")
                    quote = finding.get("evidence_quote", "")
                    if quote:
                        fence = "`" * max(3, max((len(m.group(0)) for m in re.finditer(r"`+", quote)), default=0) + 1)
                        blocks.append(f"\n{fence}text\n{quote}{'' if quote.endswith(chr(10)) else chr(10)}{fence}\n")
                if row.get("validation_errors"):
                    blocks.append("\nПроверка: `" + ", ".join(row["validation_errors"]) + "`\n")
            filename = f"{topic}-{offset // args.shard_records + 1:04d}.md"
            (directory / filename).write_text("".join(blocks), encoding="utf-8")
    if review_rows:
        review_dir = out / "Review queue"
        review_dir.mkdir(parents=True, exist_ok=True)
        unique_reviews = {row["id"]: row for row in review_rows}
        review_rows = list(unique_reviews.values())
        for offset in range(0, len(review_rows), args.shard_records):
            shard = review_rows[offset:offset + args.shard_records]
            lines = ["# Очередь повторной проверки\n\nЗаписи с ошибкой ответа, низкой проверяемостью цитат или без темы.\n"]
            for row in shard:
                lines.append(f"\n## {row['id']} · {row['status']}\n\n{', '.join(row.get('validation_errors', [])) or 'Нужна ручная проверка'}\n")
            (review_dir / f"needs-review-{offset // args.shard_records + 1:04d}.md").write_text("".join(lines), encoding="utf-8")
    shutil.copyfile(run / "validated-results.jsonl", out / "validated-results.jsonl")
    shutil.copyfile(summary_path, out / "validation-summary.json")
    dashboard_path = run / "dashboard.html"
    if dashboard_path.exists():
        shutil.copyfile(dashboard_path, out / "dashboard.html")
    topic_links = []
    for topic, rows in sorted(topic_rows.items(), key=lambda item: TOPIC_TITLES[item[0]]):
        parts = (len(rows) + args.shard_records - 1) // args.shard_records
        topic_links.append(f"- [[Topics/{topic}/{topic}-0001|{TOPIC_TITLES[topic]}]] — {len(rows)} записей, частей: {parts}")
    source_entries = [f"- [[{link}|{source_id}]]" for source_id, link in sorted(source_links.items())]
    if not source_entries:
        source_entries = [f"- {source.get('source_id', 'source')}: {source.get('title', 'source metadata')} · SHA-256 `{source.get('sha256', 'нет')}`"
                          for source in json.loads((run / "manifest.json").read_text(encoding="utf-8")).get("sources", [])]
    index_text = (
        "# Индекс полного разбора корпуса\n\n"
        f"- Исходных файлов: {summary.get('source_files', 0)}\n"
        f"- Фрагментов: {summary['records_in_source']}\n"
        f"- Уникальных текстов для модели: {summary['unique_texts_analyzed']}\n"
        f"- Повторы, обработанные один раз: {summary['exact_duplicate_records_reused']}\n"
        f"- Модель вернула ответы: {summary['records_with_model_response']} ({summary['model_response_coverage_percent']}%)\n"
        f"- Прошли автоматическую проверку точных цитат: {summary['records_model_grounded_by_exact_quote_check']}\n"
        f"- Очередь повторной проверки: {summary['records_needing_review_or_retry']}\n"
        f"- Ручная проверка: {summary['human_reviewed_records']}\n\n"
        "## Темы\n\n" + ("\n".join(topic_links) or "Темы появятся после получения модельных результатов.") +
        "\n\n## Исходные выгрузки\n\n" + ("\n".join(source_entries) or "Для этой готовой базы нет исходных файлов с офсетами; точные записи лежат в `Raw records/`.") +
        "\n\n## Дашборд\n\n[Открыть HTML-дашборд](dashboard.html)\n\n"
        "## Аудит\n\n`validated-results.jsonl` хранит построчный статус и цитаты; `validation-summary.json` хранит сводные счётчики. Модельная разметка не считается ручной проверкой.\n"
    )
    (out / "00 - Индекс.md").write_text(index_text, encoding="utf-8")
    print(json.dumps({"obsidian_export": str(out), "source_files_copied_exactly": len(source_links),
                      "topic_documents": sum((len(rows) + args.shard_records - 1) // args.shard_records for rows in topic_rows.values()),
                      "review_records": len({row["id"] for row in review_rows}),
                      "status": "exported; originals preserved"}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="split Telegram Markdown/TXT exports into lossless, offset-addressable records")
    p.add_argument("--input", required=True, help="one Markdown/TXT file or a folder")
    p.add_argument("--output", required=True, help="corpus JSON path")
    p.add_argument("--chunk-chars", type=int, default=4000)
    p.set_defaults(func=ingest)
    p = sub.add_parser("prepare", help="offline integrity check and batch JSONL generation")
    p.add_argument("--input", required=True, help="existing corpus JSON with records[].text")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--group-chars", type=int, default=6000)
    p.add_argument("--group-records", type=int, default=10)
    p.set_defaults(func=prepare)
    p = sub.add_parser("submit", help="upload a prepared run to Gemini Batch API")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--model")
    p.add_argument("--confirm-cloud-upload", action="store_true", help="required: confirms upload to Google's free-tier API")
    p.set_defaults(func=submit)
    p = sub.add_parser("poll", help="check one batch status and download results when ready")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=poll)
    p = sub.add_parser("validate", help="join model results and validate exact source quotes")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--results", help="optional Gemini JSONL response file")
    p.set_defaults(func=validate)
    p = sub.add_parser("dashboard", help="render a local HTML dashboard from validation-summary.json")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=dashboard)
    p = sub.add_parser("export-obsidian", help="write original exports, topic cards, review queue and dashboard")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--shard-records", type=int, default=300)
    p.set_defaults(func=export_obsidian)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
