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
