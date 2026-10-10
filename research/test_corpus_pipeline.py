import hashlib
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import corpus_pipeline as pipeline


class CorpusPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _source(self, texts):
        rows = []
        for i, text in enumerate(texts, 1):
            rows.append({
                "id": f"F{i:05d}",
                "source_id": "S01",
                "topics": ["ideas"],
                "occurrences": [{"line_start": i, "line_end": i}],
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })
        path = self.root / "corpus.json"
        path.write_text(json.dumps({"summary": {"source_files": 1}, "records": rows}, ensure_ascii=False), encoding="utf-8")
        return path

    def _prepare(self, source, group_chars=6000, group_records=10):
        run = self.root / "run"
        pipeline.prepare(Namespace(input=str(source), run_dir=str(run), model=pipeline.DEFAULT_MODEL,
                                   group_chars=group_chars, group_records=group_records))
        return run

    def test_prepare_preserves_every_unicode_record_and_maps_once(self):
        source = self._source(["Идея 🚀: найти спрос.", "Повторный фрагмент", "Служебная строка"])
        run = self._prepare(source, group_chars=150, group_records=2)
        snapshot = [json.loads(line) for line in (run / "source-snapshot.jsonl").read_text(encoding="utf-8").splitlines()]
        mapping = json.loads((run / "request-map.json").read_text(encoding="utf-8"))
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([x["text"] for x in snapshot], ["Идея 🚀: найти спрос.", "Повторный фрагмент", "Служебная строка"])
        all_ids = [rid for ids in mapping.values() for rid in ids]
        self.assertEqual(sorted(all_ids), ["F00001", "F00002", "F00003"])
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertEqual(manifest["record_count"], 3)

    def test_validation_flags_nonverbatim_quote_and_keeps_missing_rows(self):
        source = self._source(["Найти нишу через интервью.", "Проверить спрос.", "Повторный короткий пост"])
        run = self._prepare(source, group_chars=260, group_records=10)
        mapping = json.loads((run / "request-map.json").read_text(encoding="utf-8"))
        keys = list(mapping)
        ids = [mapping[key][0] for key in keys]
        model_rows = [
            {"id": ids[0], "topic_ids": ["ideas"], "kind": "METHOD", "summary": "Искать нишу интервью.",
             "findings": [{"type": "method", "text": "Проводить интервью", "evidence_quote": "через интервью", "promotional": False, "needs_external_verification": False}],
             "tools": [], "confidence": 0.9, "uncertainty": ""},
            {"id": ids[1], "topic_ids": ["validation"], "kind": "RECOMMENDATION", "summary": "Проверить спрос.",
             "findings": [{"type": "recommendation", "text": "Проверить спрос", "evidence_quote": "эта цитата выдумана", "promotional": False, "needs_external_verification": True}],
             "tools": [], "confidence": 0.8, "uncertainty": ""},
        ]
        results = []
        for key, model_row in zip(keys[:2], model_rows):
            results.append({"key": key, "response": {"candidates": [{"content": {"parts": [{"text": json.dumps({"records": [model_row]}, ensure_ascii=False)}]}}]}})
        path = run / "fake-results.jsonl"
        path.write_text("invalid json line\n" + "\n".join(json.dumps(result, ensure_ascii=False) for result in results) + "\n", encoding="utf-8")
        pipeline.validate(Namespace(run_dir=str(run), results=str(path)))
        rows = [json.loads(line) for line in (run / "validated-results.jsonl").read_text(encoding="utf-8").splitlines()]
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[ids[0]]["status"], "MODEL_RESULT_GROUNDED")
        self.assertIn("evidence_quote_not_verbatim", by_id[ids[1]]["validation_errors"])
        self.assertEqual(by_id[ids[1]]["status"], "NEEDS_REVIEW")
        self.assertEqual(by_id[ids[2]]["status"], "NEEDS_REVIEW")
        self.assertEqual(len(rows), 3)
        self.assertEqual(json.loads((run / "validation-summary.json").read_text(encoding="utf-8"))["malformed_result_lines"], 1)
        pipeline.dashboard(Namespace(run_dir=str(run)))
        export_dir = self.root / "obsidian"
        pipeline.export_obsidian(Namespace(run_dir=str(run), output=str(export_dir), shard_records=2))
        self.assertTrue((export_dir / "00 - Индекс.md").exists())
        self.assertTrue((export_dir / "dashboard.html").exists())
        self.assertTrue((export_dir / "Raw records" / "records-0001.md").exists())
        self.assertTrue((export_dir / "Topics" / "ideas" / "ideas-0001.md").exists())

    def test_cloud_upload_requires_explicit_flag(self):
        source = self._source(["text"])
        run = self._prepare(source)
        with self.assertRaisesRegex(ValueError, "Refusing cloud upload"):
            pipeline.submit(Namespace(run_dir=str(run), model=None, confirm_cloud_upload=False))

    def test_ingest_keeps_crlf_bom_emoji_and_every_source_character(self):
        raw = "\ufeffЗаголовок\r\n\r\nПост с эмодзи 🚀 и без потерь\r\n" + "x" * 600
        source = self.root / "telegram.md"
        source.write_bytes(raw.encode("utf-8"))
        output = self.root / "ingested.json"
        pipeline.ingest(Namespace(input=str(source), output=str(output), chunk_chars=256))
        corpus = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("".join(row["text"] for row in corpus["records"]), raw)
        self.assertEqual(corpus["sources"][0]["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        for previous, current in zip(corpus["records"], corpus["records"][1:]):
            self.assertEqual(previous["char_end"], current["char_start"])

    def test_prepare_expands_stored_variants_instead_of_silently_skipping_them(self):
        source = self.root / "with-variant.json"
        base_text = "Основа совета"
        variant_text = "Вариант совета с уточнением"
        row = {
            "id": "F00001", "text": base_text,
            "sha256": hashlib.sha256(base_text.encode()).hexdigest(),
            "variants": [{"id": "F00002", "text": variant_text,
                          "occurrences": [{"source_id": "S02"}]}],
        }
        source.write_text(json.dumps({"records": [row]}, ensure_ascii=False), encoding="utf-8")
        run = self._prepare(source)
        snapshot = [json.loads(line) for line in (run / "source-snapshot.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["id"] for r in snapshot], ["F00001", "F00002"])
        self.assertEqual(snapshot[1]["text"], variant_text)

    def test_exact_duplicate_text_is_analyzed_once_but_retained_for_each_occurrence(self):
        source = self._source(["Одинаковый совет: поговорить с клиентами.", "Одинаковый совет: поговорить с клиентами."])
        run = self._prepare(source)
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        mapping = json.loads((run / "request-map.json").read_text(encoding="utf-8"))
        duplicates = json.loads((run / "duplicate-map.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["unique_content_records"], 1)
        self.assertEqual(manifest["exact_duplicate_records_reused"], 1)
        self.assertEqual(len(mapping), 1)
        canonical = next(iter(duplicates))
        answer = {"records": [{"id": canonical, "topic_ids": ["pain"], "kind": "RECOMMENDATION",
                               "summary": "Поговорить с клиентами.", "findings": [{
                                   "type": "recommendation", "text": "Поговорить с клиентами",
                                   "evidence_quote": "поговорить с клиентами", "promotional": False,
                                   "needs_external_verification": True}], "tools": [], "confidence": 0.9,
                               "uncertainty": ""}]}
        key = next(iter(mapping))
        result = {"key": key, "response": {"candidates": [{"content": {"parts": [{"text": json.dumps(answer, ensure_ascii=False)}]}}]}}
        result_path = run / "results.jsonl"
        result_path.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
        pipeline.validate(Namespace(run_dir=str(run), results=str(result_path)))
        rows = [json.loads(line) for line in (run / "validated-results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "MODEL_RESULT_GROUNDED" for row in rows))
        self.assertEqual(sum(bool(row.get("duplicate_content_of")) for row in rows), 1)

    def test_obsidian_export_reconstructs_original_source_bytes(self):
        raw = "\ufeffПривет\r\nЭто тестовая выгрузка 🚀\r\n" + "y" * 600
        source = self.root / "telegram.md"
        source.write_bytes(raw.encode("utf-8"))
        corpus_path = self.root / "corpus.json"
        pipeline.ingest(Namespace(input=str(source), output=str(corpus_path), chunk_chars=256))
        run = self.root / "run"
        pipeline.prepare(Namespace(input=str(corpus_path), run_dir=str(run), model=pipeline.DEFAULT_MODEL,
                                   group_chars=6000, group_records=10))
        mapping = json.loads((run / "request-map.json").read_text(encoding="utf-8"))
        results = []
        for key, ids in mapping.items():
            answer = {"records": [{"id": rid, "topic_ids": [], "kind": "NOISE", "summary": "",
                                    "findings": [], "tools": [], "confidence": 0.5, "uncertainty": ""}
                                   for rid in ids]}
            results.append({"key": key, "response": {"candidates": [{"content": {"parts": [{"text": json.dumps(answer, ensure_ascii=False)}]}}]}})
        result_path = run / "fake-results.jsonl"
        result_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in results) + "\n", encoding="utf-8")
        pipeline.validate(Namespace(run_dir=str(run), results=str(result_path)))
        pipeline.dashboard(Namespace(run_dir=str(run)))
        export_dir = self.root / "obsidian"
        pipeline.export_obsidian(Namespace(run_dir=str(run), output=str(export_dir), shard_records=50))
        copies = list((export_dir / "Sources").glob("*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
