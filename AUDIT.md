# Audit

Audit of `main` at `7295117`, before the finish-and-polish work. Method: read every module,
test, config file and the demo page; installed with `pip install -e ".[dev]"`; ran the tests
and ruff; started the API on the mock backend and called each endpoint with the bundled sample
bills and with malformed inputs.

## What works

- **Pipeline.** Upload → `preprocess.prepare` (PDF render via pypdfium2, EXIF rotation, resize)
  → vision backend → `clean_raw` → Pydantic validation (one retry with the errors fed back)
  → `validation.analyse` (unit normalisation, derived figures, 10 checks) → `needs_review`.
- **Backends.** Claude (prompt / structured-output / forced-tool modes with automatic
  fallback), any OpenAI-compatible server, Ollama, and an offline mock. The SDKs already retry
  408/409/429/5xx and connection errors with exponential backoff (`max_retries=2`).
- **Checks.** Cost reconciliation, total reconciliation (incl. other charge lines), readings vs
  consumption, bands vs consumption, plausible rates, MPAN check digit, currency consistency,
  billing period, unit parsing, onboarding fields.
- **Switching savings.** `POST /v1/savings` plus UI block, with clear 422s.
- **Evaluation.** `bill_extractor.synth` (3 layouts × PDF/scan/photo, planted faults, 4 photo
  damage levels) and `bill_extractor.evaluate` + `scoring` (correct / wrong / missed /
  invented per field, review-flag catch rate, latency, tokens). Real runs on Sonnet, Haiku
  and Qwen are recorded in the README.
- **Tests and lint.** 145 passed, 1 skipped (37 s, no network). `ruff check` and
  `ruff format --check` clean.
- **Runtime.** API serves `/health`, `/v1/bills/extract`, `/v1/savings`, `/docs` and the demo
  page; all three bundled samples return 200. Wrong type → 415, corrupt PDF → 415, empty → 400.
- **Docker.** `python:3.12-slim`, non-root user, healthcheck; CI builds and smoke-tests it.

## Broken (bugs)

| # | Problem | Evidence | Impact |
|---|---|---|---|
| B1 | **Invalid JSON from the model is not retried.** `parse_json_object` raises `BackendError`, which skips the retry loop and becomes a 502. Only *schema* errors get the repair attempt. | `extractor.py` only catches `ValidationError`. | A truncated or chatty reply fails the request instead of getting one repair attempt. |
| B2 | **Demo shows the wrong bill.** The mock backend returns "Fernwick Energy" for every file, including the "Brackenridge Power" and "Halcyon Volt" sample buttons. | Clicking a sample on the demo page. | The first thing a visitor sees contradicts the bill in the preview. |
| B3 | **Decompression bombs → 500.** Pillow warns up to 179 MP and then raises `DecompressionBombError`, which is not caught. A 12 000 × 12 000 PNG is decoded in full (~430 MB). | Probe in this audit. | Memory spike; unhandled 500. |
| B4 | **A PDF with zero pages would be sent to the model with no images.** | `prepare()` returns `pages=[]` without error. | Wasted model call and a confusing result. |
| B5 | **Drag-and-drop rejects files with an empty MIME type**, which some OS/browser combinations report. The server sniffs magic bytes anyway. | `pick()` tests `file.type` only. | "This file type isn't supported" for a valid JPEG. |
| B6 | **Timeouts and rate limits are reported as a generic 502.** | `anthropic.APITimeoutError` / `RateLimitError` → `BackendError` → 502. | The user can't tell "try again" from "broken". |

## Unfinished

- **CORS** is not configured (preflight returns 405), so the API can't be called from another
  origin, even deliberately.
- **HEIC** (the iPhone default) is not accepted.
- **No field-level review state.** The response says *which checks* failed but not *which
  fields* to look at, so the UI can't highlight them.
- **No input-quality warnings.** Blurry, tiny or truncated-PDF inputs are processed silently.
- **Strictness gaps in the schema.** Negative rates/kWh, `NaN`/`Infinity`, a currency such as
  `"£"` or `"pounds"`, and untrimmed strings all validate.
- **Request validation errors** (missing `file` field) return FastAPI's raw list, not a
  readable message.
- **The eval harness reports** no schema-valid rate, no per-check pass rate and no cost per
  bill, and lives only as a module (`python -m bill_extractor.evaluate`). There is no `evals/`
  entry point, no `latest.json`, and no `EVALS.md`.
- **The UI** has no download-JSON button, no field highlighting, and its results section
  shows only the clean-bill run (100%), not the harder degraded-photo and planted-fault runs.

## Gaps vs a portfolio-ready project

- **README** is thorough but long. A hiring manager would need more than 2 minutes to find the
  pitch, a screenshot and the headline numbers. There is no screenshot.
- **Makefile** has no `dev`, `docker` or one-command `eval`.
- **CI** uses `actions/checkout@v4` / `setup-python@v5` (Node 20; Node 24 majors exist) and
  runs on push to `main` only, so feature branches are tested only once a PR exists.
- **`.env.example`** does not document every setting (`BILLX_HOST`, `BILLX_MAX_IMAGE_SIDE`,
  `BILLX_MAX_OUTPUT_TOKENS`, `BILLX_OPENAI_MODEL` examples).
- **No deploy config** or deploy guide.
- **References to "the Oklahoma sample"** in the README and `savings.py` point at a bill that
  is not in the repo (a real bill used during development, rightly not committed).
- **README says Haiku "is the pick"** while the default model is Sonnet, without explaining
  why.

## Plan

Fix B1–B6 and the unfinished items within the existing design. Then: tests for every new path,
an `evals/` entry point with the missing metrics, DX/CI updates, UI polish, README rewrite and
deploy docs. Judgement calls go in `DECISIONS.md`.
