# Energy Bill Extractor

Upload an electricity bill, as a PDF or a phone photo, and get the tariff and usage back as
validated JSON. A vision-language model reads the bill. Plain code then checks that the numbers
add up, and flags any bill a person should look at before the data is used.

## Why

Switching energy supplier means copying details off an old bill: supply number, tariff, unit
rates, standing charge and usage. Reading the bill automatically makes onboarding much faster.
But a model that misreads one digit of a unit rate quietly mis-prices a customer, so the
extraction cannot be trusted on its own. This project uses the model as the reader and code as
the checker.

## How it works

```mermaid
flowchart LR
    A[PDF or photo] --> B[Preprocess<br/>render pages, fix rotation, resize]
    B --> C[Vision model<br/>Claude, OpenAI, Ollama or vLLM]
    C --> D[Schema validation<br/>retry with errors fed back]
    D --> E[Normalise units<br/>p/kWh to GBP/kWh]
    E --> F[Plausibility checks]
    F --> G[JSON and needs_review flag]
```

### Design decisions

- **The model reads, code calculates.** The model copies values as printed (`24.86`, `"p/kWh"`).
  Unit conversion, period length and the annual-use estimate happen in Python. Models are good
  at reading and unreliable at arithmetic.
- **What is printed wins.** Suppliers count billing days differently (both end dates, or
  nights), so a printed day count is used for the standing-charge maths instead of one counted
  from the dates. Extra charge lines such as levies or a roundup donation are read into
  `other_charges` so the total still reconciles.
- **Null rather than a guess.** The prompt and schema make "not printed on the bill" an explicit
  answer, and the evaluation counts invented values separately from missed ones.
- **JSON from Claude that works with any model.** By default the schema goes into the prompt
  and the reply is parsed and validated. Two stricter modes are opt-in, because tests against
  claude-sonnet-5-5 ruled them out as defaults:
  - Structured output (`output_config.format`) failed with "Schema is too complex", after
    about two minutes spent compiling it. Making missing fields optional rather than nullable
    first got it under the documented limit of 16 union-typed parameters, but not under the
    model's internal compile limit.
  - A forced tool call was rejected: this model does not accept `tool_choice` of type `tool`.

  Prompt mode extracted every field correctly with no retries on the test bills. If a chosen
  mode is rejected, the backend falls back to the next one and remembers what worked.
  OpenAI-compatible servers get a JSON-schema response format, with a fallback to plain JSON
  mode.
- **Validate, then retry once.** If the reply does not match the schema, the validation errors
  are sent back to the model for one more attempt.
- **Checks instead of confidence scores.** A model's confidence in a number is not calibrated;
  arithmetic is. Every check reports pass, fail or skipped, with a reason:

  | Check | What it catches |
  |---|---|
  | `cost_reconciles` | Rates x kWh + standing charge differs from the subtotal: a misread rate or kWh, or pence read as pounds |
  | `total_reconciles` | Subtotal + tax + other charge lines (levies, adjustments, roundups, credits) differs from the total |
  | `readings_match_consumption` | Current reading - previous reading differs from kWh used |
  | `bands_match_consumption` | Day + night kWh differs from total kWh |
  | `rates_plausible` | A unit rate outside a sane range for the currency |
  | `meter_point_checksum` | The GB MPAN check digit fails: a digit was misread |
  | `currency_consistent` | The units imply a different currency from the one stated |
  | `billing_period` | The period ends before it starts, is implausibly long, or the printed day count disagrees with the dates |
  | `onboarding_fields` | Something needed to set up the customer is missing |

- **Nothing is stored.** Bills are personal data. Files are processed in memory, and each
  request logs one line with its ID, latency and failed checks, never the bill's contents.
- **Any model provider.** The backend is chosen with one environment variable, and the same
  tests cover each one.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m bill_extractor   # mock backend, no API key needed
```

It listens on http://127.0.0.1:8000 by default; set `BILLX_PORT` in `.env` to change it.
Open the page and drop in a bill, or click one of the sample bills. You get the extracted
fields, the charges recalculated line by line, and every check. API docs are at `/docs`.

To use a real model, copy `.env.example` to `.env` and set one of:

| Model | Settings |
|---|---|
| Claude | `BILLX_BACKEND=anthropic`, `ANTHROPIC_API_KEY=...` |
| Local, with Ollama | `ollama pull qwen2.5vl:3b`, then `BILLX_BACKEND=ollama`. Pick a model size that fits your GPU. |
| OpenAI or vLLM | `BILLX_BACKEND=openai`, `BILLX_OPENAI_MODEL=...`, and `OPENAI_API_KEY` or `OPENAI_BASE_URL` |

With Docker: `docker compose up --build`, or `docker compose --profile local up --build` to run
Ollama alongside it.

If port 8000 is already in use (Docker Desktop and WSL often hold it), set `BILLX_PORT=8010`
in `.env`. Both `python -m bill_extractor` and `docker compose` use it; open
http://127.0.0.1:8010.

## API

```bash
curl -F "file=@samples/bill_002_photo.jpg" http://localhost:8000/v1/bills/extract
```

This is the response from the mock backend, which always returns the same fixed bill. A real
model returns the fields from your file.

```json
{
  "extraction": {
    "supplier_name": "Fernwick Energy",
    "meter_point_id": "1600123456785",
    "tariff_name": "Fernwick Fixed Nov 2027",
    "billing_period_start": "2026-08-14",
    "billing_period_end": "2026-09-12",
    "consumption_kwh": 287.0,
    "unit_rates": [{"label": "Standard", "rate": 24.86, "rate_unit": "p/kWh", "kwh": 287.0}],
    "standing_charge": 51.42,
    "standing_charge_unit": "p/day",
    "total_amount": 91.12
  },
  "derived": {
    "currency": "GBP",
    "period_days": 30,
    "unit_rates": [{"label": "Standard", "per_kwh": 0.2486, "kwh": 287.0}],
    "standing_charge_per_day": 0.5142,
    "estimated_annual_kwh": 3492.0
  },
  "checks": [
    {"name": "cost_reconciles", "status": "pass", "detail": "Rates x kWh + standing charge = 86.77, subtotal 86.78."},
    {"name": "meter_point_checksum", "status": "pass", "detail": "MPAN check digit is valid."}
  ],
  "needs_review": false,
  "review_reasons": [],
  "meta": {"backend": "mock", "model": "mock-fixed-bill", "attempts": 1, "latency_ms": 28, "pages_sent": 1}
}
```

The response is trimmed here. Errors use standard codes: 413 (file too large), 415 (not a PDF
or image), 422 (the model's answer never matched the schema) and 502 (provider error).

## Switching savings

`POST /v1/savings` compares an extracted bill with a target tariff on the same
annual usage and estimates what switching would save. This is the onboarding
pitch: read the old bill, then show the customer what they would pay elsewhere.

The demo page shows the same comparison after an extraction: a Switching
savings block with target rate, standing charge and name pre-filled (20p/kWh
and 40p/day for GBP bills), a Compare button, and the annual/monthly saving
with a per-band breakdown. A bill with no unit rates shows that the saving
cannot be estimated instead of inventing one.

```bash
curl -X POST http://localhost:8000/v1/savings -H "Content-Type: application/json" -d '{
  "extraction": {
    "supplier_name": "Fernwick Energy", "currency": "GBP",
    "billing_period_start": "2026-08-14", "billing_period_end": "2026-09-12",
    "consumption_kwh": 287,
    "unit_rates": [{"label": "Standard", "rate": 24.86, "rate_unit": "p/kWh", "kwh": 287}],
    "standing_charge": 51.42, "standing_charge_unit": "p/day"
  },
  "target": {
    "name": "Fuse Standard", "currency": "GBP",
    "unit_rates": [{"rate": 20.0, "rate_unit": "p/kWh"}],
    "standing_charge": 40.0, "standing_charge_unit": "p/day"
  }
}'
```

```json
{
  "currency": "GBP",
  "annual_kwh": 3492.0,
  "current": {"energy": 868.11, "standing": 187.68, "total": 1055.79},
  "target": {"energy": 698.40, "standing": 146.00, "total": 844.40},
  "target_name": "Fuse Standard",
  "saving_annual": 211.39,
  "saving_monthly": 17.62,
  "saving_pct": 20.0,
  "bands": [
    {"label": "Standard", "annual_kwh": 3492.0, "current_cost": 868.11, "target_cost": 698.40}
  ],
  "warnings": [],
  "assumptions": [
    "Costs exclude tax/VAT; both tariffs are treated the same.",
    "Annual use is daily average x 365 and ignores seasons.",
    "Target is a single rate applied to all usage."
  ]
}
```

Rules, all in plain code (`bill_extractor/savings.py`):

- **Annual use** is the bill's daily average x 365, the same estimate the
  extraction reports. Economy 7 usage is split across Day/Night by the bands'
  shares of the bill's kWh.
- **Target** is one unlabelled rate applied to all usage, or one rate per band
  matched by label. A labelled single rate matches only that band.
- **Costs exclude tax/VAT**, so both tariffs are compared on the same basis.
- **No invented current cost.** A bill with no unit rates printed cannot be
  priced, so the endpoint returns 422 instead of guessing — the Oklahoma
  sample bill is such a case. Currency mismatch and a target band with no
  matching rate are also 422.
- If the extraction was flagged for review, the result carries a warning that
  the saving is only as good as the extraction.

## Evaluation

`bill_extractor.synth` generates labelled UK bills in three layouts, each rendered as a digital
PDF, a 150 dpi scan and a phone-style photo (tilted, blurred, unevenly lit):

- **Single rate**, with previous and current meter readings, actual or estimated.
- **Economy 7**, with day and night rates from a smart meter and no cumulative readings. The
  model should return null for the readings, not invent them.
- **Account summary first**, with a previous balance, so the large "Amount due" figure is not
  this period's charges. It tests whether the model picks the right total.

Dates, unit spellings and decimal places vary between bills. Suppliers, names and addresses are
fictional, and every page is watermarked as a synthetic sample.

```bash
make data                                                # 24 bills x 3 formats
python -m bill_extractor.evaluate --backend anthropic --limit 10
```

The report (Markdown and JSON in `reports/`) gives:

- Field accuracy, overall and per field, split into correct, wrong, missed and invented values.
- Share of documents with every field correct, by input type (PDF, scan, photo).
- **Review-flag quality:** of the documents the model got wrong, how many the checks flagged
  (catch rate), and how many fully correct documents were flagged anyway (false-alarm rate).
- Latency (p50 and p95), mean attempts and token use.

### Planted-error bills: measuring the review flag

Clean bills cannot measure the review flag: if the model reads everything correctly,
there is nothing to catch. `synth --faulty N` prints one deliberate inconsistency on
N of the bills, spread evenly through the set:

| Planted fault | Check that must fail |
|---|---|
| `wrong_total` | `total_reconciles` — total £5.00 above subtotal + tax |
| `bad_mpan` | `meter_point_checksum` — one MPAN digit changed |
| `swapped_readings` | `readings_match_consumption` — previous/current swapped |
| `wrong_period_days` | `billing_period` — printed days 5 more than the date span |
| `wrong_consumption` | `bands_match_consumption` — consumption 50 kWh above the bands |
| `missing_unit_rates` | `onboarding_fields` — no unit rate printed at all |

The ground truth records what is printed, fault included, so a faithful extraction
still scores 100% on fields — the bill itself is what must be flagged. The report
then gives a separate planted-fault section: catch rate overall and per fault type,
whether the expected check failed, and a false-alarm rate counted on clean bills only.

```bash
python -m bill_extractor.synth --n 24 --faulty 12 --out data/synthetic
python -m bill_extractor.evaluate --backend anthropic --data data/synthetic
```

An oracle backend that returns ground truth scores 100% on fields and flags every
faulty bill (covered by tests). The next real-model run should use faulty bills too,
so the reported catch rate is the model's, not the oracle's.

### Degraded-photo curve: accuracy against photo damage

Real bills arrive as quick phone photos, not clean scans. `synth --degraded`
writes one extra photo per bill at each damage level, so an evaluation over
those variants shows where accuracy starts to fall:

| Level | Damage |
|---|---|
| `clean` | Nearly straight, sharp, JPEG quality 85–95 |
| `mild` | The original phone photo: tilt ±3°, light blur, JPEG 55–75 |
| `moderate` | Tilt ±6°, stronger blur, dimmer, a glare spot, 5% cropped off one edge, JPEG 40–55 |
| `severe` | Tilt ±10°, heavy blur, dim, larger glare, 10% cropped, JPEG 25–40 |

```bash
python -m bill_extractor.synth --n 24 --degraded --out data/synthetic
python -m bill_extractor.evaluate --backend anthropic --variants photo_clean,photo_mild,photo_moderate,photo_severe
```

The report's per-input table then gives field accuracy and exact-match rate
for each level — the curve. No scoring changes were needed: the degraded
photos are just extra variants.

### Results

claude-sonnet-5-5 in prompt mode, run on 17 October 2026: 10 bills in 3 formats, so 30
documents and 510 fields.

| Input | Documents | Field accuracy | All fields correct |
|---|---|---|---|
| Digital PDF | 10 | 100% | 100% |
| 150 dpi scan | 10 | 100% | 100% |
| Phone-style photo | 10 | 100% | 100% |

- No wrong, missed or invented values. Every reply passed schema validation on the first
  attempt.
- Latency per bill: 2.5 s median, 3.2 s p95.
- About 4,200 input and 270 output tokens per bill.

**What this does and does not show.** The synthetic bills are clean and consistently laid out,
so a perfect score shows the pipeline works end to end, not that it will be perfect on real
bills. That run used clean bills only, so it could not measure how often the review flag
catches mistakes. The planted-error bills above close that gap: the harness now measures
catch rate per fault type, and the oracle test proves it flags every faulty bill when the
extraction is faithful. The next step is a harder set: real bills used with consent, more
layouts, and worse photos.

## Tests

`make test` runs 146 tests without network access or API keys. They cover:

- Preprocessing.
- Every check, including that the generator's ground truth passes all of them.
- Planted-error bills: each fault fails only its expected check, and an oracle backend
  flags every faulty bill while scoring 100% on fields.
- Degraded photos: levels are ordered by measured sharpness, unknown levels are
  rejected, and `generate(degraded=True)` writes one photo per level.
- Switching savings: mock-bill arithmetic, Economy 7 band split and label matching,
  currency mismatch, unpriceable bills, and the `/v1/savings` endpoint.
- The retry loop.
- The evaluation harness: a backend that returns the ground truth must score 100%.
- The exact request bodies sent to Claude and to OpenAI-compatible servers, and each fallback
  path, using the real SDKs with a fake HTTP transport.

CI runs lint and the tests, then builds the Docker image and smoke-tests it.

## Project layout

```
bill_extractor/
  api.py            FastAPI app and demo page
  extractor.py      pipeline: preprocess, model call, validate and retry, checks
  preprocess.py     PDF and image to page images
  prompts.py        system prompt and instructions
  schema.py         Pydantic models (also the JSON schema sent to the model)
  validation.py     unit normalisation, derived figures, plausibility checks
  savings.py        switching-savings estimate: current vs target tariff
  backends/         anthropic, openai-compatible (OpenAI, Ollama, vLLM), mock
  synth.py          synthetic bill generator
  scoring.py        field-level scoring and summary metrics
  evaluate.py       evaluation CLI
  static/index.html demo page
samples/            three synthetic bills in each format
tests/
```

## Limitations and next steps

- Synthetic bills are cleaner than real ones. Measuring real-world accuracy needs a labelled set
  of real bills, used with consent and redacted.
- Only UK electricity is modelled. Gas, dual fuel, multi-register readings and export tariffs are
  not yet supported.
- The annual-use estimate is average daily use x 365, so it is skewed by the season the bill
  covers.
- Possible next steps: source locations for each field so a reviewer can jump to it, a
  cost and latency comparison across models, and turning reviewed corrections into new
  evaluation cases.

## License

MIT
