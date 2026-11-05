# Decisions

Judgement calls made while finishing the project. Each says what was decided, why, and what it
costs. The architecture, stack and model provider were not changed.

## Process

1. **Branch.** The work is on `claude/quirky-galileo-vb14y0`, not `claude/finish-polish`. The
   automation that ran this session requires that branch name. It was identical to `main`
   when work started, so it is a fresh branch from the latest default branch.
2. **No rewrite of the existing evaluator.** `bill_extractor.synth`, `evaluate` and `scoring`
   already did per-field scoring, planted faults and photo-damage levels. `evals/` is a thin
   entry point over them (`generate_dataset.py`, `run_eval.py`). The missing metrics were
   added to `scoring.py`, so the old CLI gets them too.
3. **No API key was available**, so the real-model runs were not repeated. EVALS.md keeps the
   recorded runs from 17 October 2026, labelled as such, and the auto-written "Latest run"
   section is a simulated self-test that says so in bold. The recorded runs predate the
   `input_quality` check. Re-running would change the review-flag numbers (more severe
   photos flagged) but not field accuracy.
4. **Docker was verified locally with a modified copy of the Dockerfile.** This sandbox's
   HTTPS proxy uses its own CA, so `pip install` inside the build failed certificate checks.
   A throwaway copy that trusted that CA built and passed the same smoke test CI runs. The
   committed Dockerfile is unchanged by this and builds in CI, which has normal network
   access.

## Robustness

5. **The JSON repair shares the attempt budget with schema repair.** `BILLX_MAX_ATTEMPTS=2`
   means one reply plus one repair, whether the first reply was invalid JSON or failed the
   schema. One budget keeps the worst-case cost and latency per bill predictable.
6. **Transient errors are retried by the provider SDKs, not a custom loop.** The Anthropic
   and OpenAI SDKs already retry connection errors, 408, 409, 429 and 5xx with exponential
   backoff and honour `retry-after`. A second retry layer would multiply waits. The count is
   now configurable (`BILLX_PROVIDER_RETRIES`). Persistent overload or rate limits map to 503
   with `Retry-After: 30`, and timeouts to 504, so a client can tell "try again" from
   "broken".
7. **Schema strictness: impossible values are rejected; questionable ones go to the checks.**
   Negative rates, kWh, readings and standing charges, `NaN` and non-ISO currencies fail
   validation and trigger the repair attempt. Negative totals, subtotals and other charges
   are allowed, because credit bills exist. "Period ends before it starts" stays a *check*,
   not a schema rule: rejecting it would throw away a faithful read of a misprinted bill, and
   the point is to flag it.
8. **Currency aliases.** `£`, `pounds`, `euro`, `$` and similar are normalised to ISO codes,
   because models sometimes write them despite the prompt. `Rs` maps to LKR, matching the
   existing unit parser. It is ambiguous with INR; a bill that also prints units such as
   `INR/kWh` will fail `currency_consistent` and be flagged.
9. **`input_quality` sets `needs_review`.** Model confidence is not calibrated, but input
   quality is measurable and predicts errors. In the recorded runs, severe photos had 33%
   all-correct against 92% for moderate. The check fails photos under 1000 px on the long
   side, or with an edge-sharpness score below `BILLX_BLUR_THRESHOLD`.
   - **Calibration.** The score is the mean `FIND_EDGES` response after downscaling to
     1200 px. Over 8 synthetic bills: clean 6.5–7.9, mild 5.5–6.6, moderate 2.0–3.1,
     severe 1.25–1.9. The threshold of 2.0 separates moderate from severe.
   - **Cost.** Correctly read severe photos are flagged too. That shows as false alarms in
     the eval, so the report breaks false alarms down by check.
   - **Risk.** The score depends on how much text is on the page, so a sparse real bill
     could score low. The threshold is configurable (0 disables it) and needs checking on
     real photos.
10. **PDFs skip the quality check.** They are rendered at the target resolution, so there is
    nothing to measure. PNG inputs (scans and screenshots) get the size check but no blur
    score, because PNG is lossless and usually not a camera photo.
11. **Pixel cap of 40 MP.** This is checked from the image header before decoding, which stops
    decompression bombs before they use memory. 40 MP is above any phone camera's output.
12. **HEIC is an optional extra** (`pillow-heif`). The Docker image and dev install include it;
    a minimal install answers 415 with a hint. iPhones save HEIC by default, but the
    dependency is native code that not everyone wants.
13. **CORS is off by default.** The UI is served by the API itself (same origin). Origins are
    allowed only through `BILLX_CORS_ORIGINS`.
14. **Client request IDs are sanitised.** Only `[A-Za-z0-9._-]{1,64}` is echoed and logged;
    anything else is replaced. This stops log injection through the header.
15. **Early upload rejection.** A middleware rejects a body whose `Content-Length` is over the
    limit before the multipart form is parsed. The in-handler check stays for requests with
    no length.

## Product and UI

16. **The mock backend replays ground truth for the bundled samples.** Before, every sample
    button showed "Fernwick Energy", which contradicted the bill in the preview. Matching is
    by the SHA-256 of the rendered first page: exact, so it never returns another bill's
    values. Any other file still gets the fixed example bill, and the page says so.
17. **A fourth sample bill with a planted error** (swapped meter readings). The review flow
    and field highlighting are the project's main idea, and a visitor without an API key
    should see them in one click.
18. **Field highlighting is driven by the API**, through `Check.fields` and
    `ExtractionResult.flagged_fields`, not by UI logic. Any API client gets the same signal.
19. **The PDF preview uses the browser's built-in viewer** (`<object>`). Headless browsers
    show a fallback note instead. Rendering previews on the server would mean a second
    upload or a new endpoint, which isn't worth it for a demo.

## Evaluation

20. **Photos in the eval set are JPEG, not PNG.** The brief suggested "photo-like PNGs". Phone
    photos arrive as JPEG, and JPEG compression artefacts are part of the damage being
    measured. Scans stay PNG.
21. **The default eval inputs are `pdf,png,photo,photo_severe`** (96 documents). This covers
    every input type plus the worst damage level, at about $1.20 per run on Sonnet. The full
    damage curve is one `--variants` flag away.
22. **The simulated backend** injects the error types seen in real runs, at rates that rise
    with damage, and is seeded so runs repeat. It exists to exercise the harness (scoring,
    repair path, review flag), not to estimate accuracy. Every output that comes from it is
    labelled.
23. **Cost** comes from a small price table (`bill_extractor/pricing.py`, checked November
    2026). Unknown models report no cost rather than a wrong one. Simulated runs price their
    synthetic token counts at the default model's rates, and say so.

## Deploy and CI

24. **Render** was chosen: it has a free tier, Docker support and a declarative
    `render.yaml`. The blueprint defaults to demo mode (`BILLX_BACKEND=mock`), `autoDeploy:
    false`, and Haiku when switched to Claude, because a public URL with a real key and no
    auth can run up a bill. Not deployed, as instructed.
25. **CI runs on every push**, not only on `main`, so feature branches are tested before a PR
    exists. A concurrency group cancels superseded runs. The actions are on their current
    majors (`checkout@v7`, `setup-python@v7`, Node 24).
26. **The default model stays Sonnet 5.5.** The old README said Haiku "is the pick", while the
    config defaulted to Sonnet. Sonnet is kept as the default for accuracy on poor photos;
    the README and `.env.example` now present Haiku as the cost option. Changing a default
    that changes accuracy is the owner's call.
