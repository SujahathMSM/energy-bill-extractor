# Evaluation

How well does the extractor read bills, and how often does it catch its own mistakes? This
page covers what is measured, the recorded runs on real models, how to re-run them, and the
latest harness run.

> **This branch's latest run is simulated.** The session that built this harness had no API
> key, so the "Latest run" section at the bottom comes from a stand-in backend that injects
> known errors. It proves the harness works; it is **not** model accuracy. The **real-model
> numbers** are the recorded runs below (17 October 2026). Set `ANTHROPIC_API_KEY` and run
> `make eval` to replace the latest run with real numbers.

## What is measured

**Dataset.** Real bills are personal data, so the set is synthetic:
`python -m evals.generate_dataset` (run automatically by `make eval`) writes 24 UK electricity
bills from `bill_extractor.synth`:

- **3 layouts.** Single rate with meter readings. Economy 7 day/night with no cumulative
  readings (the model must return null, not invent readings). Account summary first, with a
  previous balance (the large "Amount due" is not this period's total).
- **5 fictional suppliers**, varied date formats, unit spellings (`p/kWh`, `pence per kWh`)
  and decimal places. Every page is watermarked as a synthetic sample.
- **Each bill as several inputs:** digital PDF, 150 dpi scan (PNG), and phone-style photos
  (JPEG; tilted, blurred, unevenly lit) at four damage levels: clean, mild, moderate, severe.
- **6 bills carry one planted inconsistency**, printed on the bill itself: a wrong total, a
  bad MPAN digit, swapped readings, a wrong day count, consumption that disagrees with the
  bands, or no unit rate. Ground truth records what is printed, fault included, so a faithful
  read still scores 100% on fields. What must happen is that the bill gets flagged.

**Scoring** (`bill_extractor/scoring.py`). Each of 19 fields is scored as correct, wrong,
missed (null for a printed value) or invented (a value that is not printed):

| Field type | Rule |
|---|---|
| IDs: account, MPAN, meter serial | Exact match after removing spaces and punctuation |
| Dates | Exact match |
| Numbers | Within 1p or 0.5%, whichever is larger |
| Unit rates | Same number of bands; each band's price per kWh (after unit conversion) and kWh match |
| Other charges | Amounts match one for one (labels vary too much to score) |
| Text: supplier, tariff | Case-insensitive, punctuation ignored |

**Metrics** (`python -m evals.run_eval`):

- **Field accuracy**, overall, per field and per input type. **Exact-match rate**: documents
  with every field correct.
- **Schema-valid rate**: replies that validated, possibly after the repair attempt. Also the
  rate on the first attempt.
- **Consistency checks**: pass, fail and skipped counts for each check, and the share of
  documents that pass every check.
- **Review flag**: on planted-fault bills, the catch rate and whether the *expected* check
  fired. On clean bills, the catch rate on the model's own errors and the false-alarm rate,
  broken down by check.
- **Latency** p50 and p95, mean attempts, tokens, and **estimated cost per bill**
  (`bill_extractor/pricing.py`: Sonnet 5.5 $2 / $10, Haiku 5.5 $0.10 / $0.50 per M tokens).

## Recorded runs on real models

These runs used the `bill_extractor.evaluate` CLI from before this branch. The scoring is the
same, but the review flag had no `input_quality` check yet, so severe photos were not flagged
for blur.

### Headline

| Model | Docs | Field accuracy | All fields correct | Planted faults caught | Latency p50 / p95 | Cost per bill |
|---|---|---|---|---|---|---|
| Sonnet 5.5, clean inputs | 30 | **100%** | 100% | not tested | 2.5 s / 3.2 s | ~$0.011 |
| Sonnet 5.5, 4 photo levels | 96 | 97.5% | 78.1% | not tested | 2.6 s / 7.0 s | ~$0.013 |
| Haiku 5.5, 4 photo levels | 96 | 96.0% | 68.8% | not tested | 4.0 s / 7.9 s | ~$0.0008 |
| Haiku 5.5, PDF / scan / photo, 12 of 24 bills faulty | 72 | 99.6% | 91.7% | **36 / 36 (100%)** | 3.4 s / 4.6 s | ~$0.0008 |
| Qwen2.5-VL 3B, local, clean photos | 10 | 88.4% | 0% | not tested | 26.4 s / 43.4 s | $0 |

### Accuracy against photo damage (same 96 documents)

| Photo level | Sonnet field acc. | Haiku field acc. | Sonnet all correct | Haiku all correct |
|---|---|---|---|---|
| Clean | 99.8% | 99.6% | 95.8% | 91.7% |
| Mild | 99.6% | 98.9% | 91.7% | 79.2% |
| Moderate | 99.6% | 99.6% | 91.7% | 91.7% |
| Severe | 91.0% | 86.0% | 33.3% | 12.5% |

### Failure patterns

1. **Long digit strings fail first.** The weakest fields were MPAN (Sonnet 86.5%, Haiku
   81.2%) and meter serial (88.5% / 82.3%). Blur and glare turn a 13-digit number into a
   plausible-looking wrong one. The MPAN check digit catches most of these, but about 1 in 10
   wrong digits still gives a valid check digit, and the meter serial has no checksum at all.
   Money fields stayed at 98–100%.
2. **Accuracy is flat until severe damage, then drops sharply.** Two in three severe photos
   had at least one wrong or missed field, while moderate photos were as good as clean ones.
   This is why `input_quality` now flags photos below a sharpness threshold calibrated
   between those two levels (see `DECISIONS.md`): the cases most likely to be wrong are the
   ones sent for review.
3. **Blank fields slip past the checks.** Haiku's 6 misses on the planted-fault run were
   `period_days` and `reading_type`. A blank day count falls back to the date span, so no
   check fails. Checks catch inconsistencies, not omissions of optional fields.
4. **The review flag catches what adds up wrong, not what reads wrong.** On the models' own
   errors, both caught 66.7% with 0% false alarms. The 7 misses were a wrong supplier name, a
   wrong meter serial, or an MPAN misread that still had a valid check digit. On planted
   faults, the checks caught 100% and the expected check fired every time.
5. **The small local model invents data.** Qwen 3B scored 0% on `other_charges` (it added
   charges that were not on the bill) and 40% on `reading_type`. At 1568 px, 19 of 20
   replies were empty; 1024 px fixed that. It took 26 s per bill on an RTX 3050 laptop.

**Model choice.** Sonnet is the default because it is more accurate where it matters (severe
photos, digit strings). Haiku is about 16× cheaper and caught every planted fault, so for
high volume `BILLX_ANTHROPIC_MODEL=claude-haiku-5-5` is a reasonable trade-off.

## How to run

```bash
make eval                                                     # Claude if ANTHROPIC_API_KEY is set, else simulated
python -m evals.run_eval --backend anthropic --model claude-haiku-5-5
python -m evals.run_eval --backend anthropic --variants photo_clean,photo_mild,photo_moderate,photo_severe
python -m evals.run_eval --backend oracle                     # harness self-test: must be 100%
python -m evals.run_eval --limit 5                            # quick smoke run
```

Results go to `evals/results/latest.json` (every document's outcomes) and
`evals/results/latest.md`, and the section below is updated in place. With the default
variants (`pdf,png,photo,photo_severe`), a full run is 96 documents and costs about $1.20 on
Sonnet 5.5 or about $0.08 on Haiku 5.5.

**Proving the harness without a key.** `--backend oracle` returns ground truth: anything
below 100% field accuracy, or any planted fault not flagged, is a harness bug (this is also a
unit test). `--backend simulated` returns ground truth with seeded errors of the kinds seen in
the real runs: digit misreads, blank day counts, pence read as pounds, a wrong total, an
invented reading, and invalid JSON or schema errors. These show that scoring, the repair path
and the review flag react as they should.

## Limits of this evaluation

- Synthetic bills are cleaner and more uniform than real ones: 3 layouts, 5 suppliers. A
  perfect score on them shows the pipeline works, not that real bills will score that well.
  The next step is a small, consented and redacted set of real bills.
- 24 bills is small. A single document moves a per-variant rate by about 4 points.
- The blur threshold was calibrated on the synthetic photos. Real photos differ in how much
  text they hold, so the threshold needs checking on real data.

## Latest run

<!-- latest-run:start -->
**SIMULATED (no API key: injected errors, not model accuracy)** - 2026-11-05T05:56:27Z

24 synthetic bills (6 with a planted fault) x 4 inputs (pdf, png, photo, photo_severe) = 96 documents. Run time 2.9 s.

> These numbers come from a stand-in backend, not a model. They show the harness works; they say nothing about extraction accuracy. Set `ANTHROPIC_API_KEY` and run `make eval` for real numbers.

- Documents: 96 (1 failed to extract)
- Field accuracy: 99.1%
- Documents with every field correct: 83.2%
- Outcomes: {'correct': 1789, 'wrong': 14, 'missed': 2, 'hallucinated': 0}
- Review flag caught 10 of 13 documents with an error (76.9%); false alarms on correct documents: 19.0% (input_quality: 11)
- Planted faults: checks flagged 24 of 24 faulty documents (100.0%); expected check failed on 100.0%
  - bad_mpan (meter_point_checksum): 4/4 flagged (100.0%), expected check 100.0%
  - missing_unit_rates (onboarding_fields): 4/4 flagged (100.0%), expected check 100.0%
  - swapped_readings (readings_match_consumption): 4/4 flagged (100.0%), expected check 100.0%
  - wrong_consumption (bands_match_consumption): 4/4 flagged (100.0%), expected check 100.0%
  - wrong_period_days (billing_period): 4/4 flagged (100.0%), expected check 100.0%
  - wrong_total (total_reconciles): 4/4 flagged (100.0%), expected check 100.0%
- Extraction errors (most common first):
  - 1x ExtractionError: Model output failed validation after 2 attempts: - (reply): The model reply was not valid JSON: Expecting ',' delimiter Reply with a single JSON object and nothing else.
- Schema-valid replies: 99.0% (first attempt: 92.7%)
- Documents passing every consistency check: 52.6%
- Latency p50 / p95: 144 / 251 ms; mean attempts 1.06
- Tokens: 392024 in / 21113 out
- Estimated cost: $0.01048 per bill, $0.9952 in total (simulated tokens priced as claude-sonnet-5-5)

| Input | Docs | Field accuracy | All fields correct |
|---|---|---|---|
| pdf | 24 | 99.6% | 91.7% |
| photo | 24 | 98.7% | 75.0% |
| photo_severe | 24 | 98.4% | 69.6% |
| png | 24 | 99.8% | 95.8% |

| Field | Accuracy |
|---|---|
| supplier_name | 100.0% |
| account_number | 100.0% |
| meter_point_id | 96.8% |
| meter_serial | 95.8% |
| tariff_name | 100.0% |
| currency | 100.0% |
| billing_period_start | 100.0% |
| billing_period_end | 100.0% |
| period_days | 97.9% |
| previous_reading | 100.0% |
| current_reading | 100.0% |
| reading_type | 98.9% |
| consumption_kwh | 100.0% |
| standing_charge | 100.0% |
| subtotal_excl_tax | 100.0% |
| tax_amount | 100.0% |
| total_amount | 96.8% |
| unit_rates | 96.8% |
| other_charges | 100.0% |

| Check | Passed | Failed | Skipped | Pass rate (when run) |
|---|---|---|---|---|
| bands_match_consumption | 87 | 4 | 4 | 95.6% |
| billing_period | 91 | 4 | 0 | 95.8% |
| cost_reconciles | 88 | 3 | 4 | 96.7% |
| currency_consistent | 95 | 0 | 0 | 100.0% |
| input_quality | 48 | 23 | 24 | 67.6% |
| meter_point_checksum | 88 | 7 | 0 | 92.6% |
| onboarding_fields | 91 | 4 | 0 | 95.8% |
| rates_plausible | 88 | 3 | 4 | 96.7% |
| readings_match_consumption | 59 | 4 | 32 | 93.7% |
| total_reconciles | 88 | 7 | 0 | 92.6% |
| units_understood | 95 | 0 | 0 | 100.0% |

Most common failures:

- `meter_serial` wrong: 4 document(s)
- `meter_point_id` wrong: 3 document(s)
- `unit_rates` wrong: 3 document(s)
- `total_amount` wrong: 3 document(s)
- `period_days` missed: 2 document(s)
- `reading_type` wrong: 1 document(s)
- Extraction failed (1x): ExtractionError: Model output failed validation after 2 attempts: - (reply): The model reply was not valid JSON: Expecting ',' delimiter Reply with a single JSO

<!-- latest-run:end -->
