# Telegram knowledge pipeline

This repository contains the Telegram Markdown cleaner (`api/webhook.py`) and an offline-first corpus pipeline for extracting topic cards from complete exports.

## Existing bots

- `MarkItDown_Converter_bot` converts files to Markdown. It does not perform semantic research.
- `tg-knowledge` removes Telegram export noise with deterministic rules. It is useful for a readable copy, but some lines are deliberately removed. Keep the original export as the source for exhaustive analysis.
- `research/corpus_pipeline.py` ingests original Markdown/TXT exports without cleaning or deduplicating them, submits one batch of semantic analysis requests, verifies exact evidence quotes, and generates a dashboard.

The long-running analysis is a command-line batch job, not a Vercel webhook. A webhook should return quickly; the Batch API runs asynchronously, so the Telegram bot remains useful for individual file conversion and cleanup.

## Run locally

Python 3.10+; the offline stages use only the standard library. Put Telegram Desktop Markdown exports or converted text files in `exports/`.

```bash
python research/corpus_pipeline.py ingest --input ./exports --output ./work/corpus.json
python research/corpus_pipeline.py prepare --input ./work/corpus.json --run-dir ./work/run
```

`ingest` walks every `.md`, `.markdown`, and `.txt` file. It splits by line boundaries near 4,000 characters and verifies that concatenating all chunks recreates each source byte-for-byte (UTF-8). It records the file SHA-256, character offsets, and line ranges. It does not trim, normalize, deduplicate, or discard source text.

`prepare` verifies each record hash, preserves a JSONL source snapshot, and creates a Gemini JSONL file. Related fragments stored as `variants` are expanded into separate records. Byte-identical text is sent to the model once, while every original occurrence remains in the snapshot and gets its own result row and source location. The output records map each model result to the exact source id and offsets.

## Semantic batch processing

The included Gemini Batch API preset uses `gemini-3.1-flash-lite`. To submit:

```bash
export GEMINI_API_KEY="your-key"
python research/corpus_pipeline.py submit --run-dir ./work/run --confirm-cloud-upload
```

Then check the job and download its result when complete:

```bash
python research/corpus_pipeline.py poll --run-dir ./work/run
python research/corpus_pipeline.py validate --run-dir ./work/run
python research/corpus_pipeline.py dashboard --run-dir ./work/run
python research/corpus_pipeline.py export-obsidian --run-dir ./work/run --output ./work/Obsidian
```

The model processes every chunk, returns topic tags plus methods, ideas, pain points, tools, cases, and other findings. Each finding must include an exact source quote. The validator checks the quote as a literal substring and checks record ids, counts, and hashes. A missing, malformed, or unsupported response goes into `NEEDS_REVIEW`; it is never silently counted as reviewed. `MODEL_RESULT_GROUNDED` means the JSON and exact-quote checks passed. It does **not** mean a person reviewed the record or that an old recommendation is currently true.

`dashboard.html` and `validation-summary.json` distinguish model coverage, quote-check status, retry/review backlog, and the number of records personally reviewed. `export-obsidian` creates an index, topic cards split into manageable Markdown files, a retry queue, an HTML dashboard, and exact reconstructed source exports. They never label machine analysis as manual review.

## Privacy and speed

`ingest` and `prepare` run offline. `submit` refuses to upload anything unless `--confirm-cloud-upload` is supplied. Google currently lists free Batch pricing for Gemini 3.1 Flash-Lite, but says free-tier prompts and responses may be used to improve products. Its Batch API targets completion within 24 hours; many jobs finish sooner, but there is no five-second guarantee for semantic inference. Read the current [pricing](https://ai.google.dev/gemini-api/docs/pricing) and [Batch API terms](https://ai.google.dev/gemini-api/docs/batch-api) before uploading private chats.

On the 27 source exports available during development (13,384,171 characters), the offline `ingest` stage produced 3,464 lossless chunks in 2.1 seconds; `prepare` produced 3,451 requests and a 35.7 MB batch file in 1.2 seconds. This benchmark covers parsing, exact-preservation checks, and packaging only; it does not include semantic model processing.

For private work, do not run `submit`. The offline source-preservation and request-preparation stages are still useful, but this first version does not include a local-model adapter.

## Local checks

```bash
python -m unittest discover -s research -v
```
