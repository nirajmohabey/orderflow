# Benchmark status and a future verification fix

> Patch version note: Current v0.1.1 native evidence is in `artifacts/recheck-2026-11-23/`. The earlier Linux/Airflow/container/OS and measured v1 workload results below are historical v0.1.0 evidence. Use `scripts/review_patch.py` for the current native review. The legacy release/partial-render tools require their matching original source; no current full performance matrix or Linux rerun is claimed.

The selected release keeps `scripts/benchmark.py` v1 byte-for-byte, along with its actual reports. All six final10k trials passed. Five of six100k trials passed the full gate. The remaining one-worker repetition processed100,000 identities with zero ingestion HTTP errors and passed all16 database/observer assertions, but its three final authenticated API assertions failed. Its timed workload lasted1,852.989971seconds, longer than the30-minute fixture-token lifetime. The original report contains false assertions, not contemporaneously recorded401 statuses; expiry is the source/timing diagnosis, separately reproduced by the focused proof below.

That trial remains **failed**, its original report/raw evidence remains available, and the complete100k matrix is **not verified**. Individual completed timings are partial audit evidence, not a passing six-run benchmark or a same-host speedup claim. The selected release manifest must keep benchmark status `not_verified`; no median from an incomplete passing group is promoted into the portfolio's optional benchmark field.

A matching private Linux retry was blocked by the provider's free Hobby usage quota. No paid upgrade was authorized. The application104-test Windows/Linux proofs retain their own strict current-source gates and do not depend on a fabricated benchmark completion.

## Future v2 harness

`analysis/benchmark_v2.py` is a versioned future harness outside the selected104-test source scope. It retains preflight sign-in and the measured input/worker/observer/timer path. After the timer finishes and worker processes stop, it obtains fresh fixture credentials immediately before the final API assertions. It records refresh/overview/cross-tenant HTTP statuses and proof that refresh began after the measurement finish. Tokens and signing secrets are never emitted.

Its provenance hashes **its own v2 file**, rather than falsely citing v1. A100k v2 run requires a complete six-trial10k gate using that exact v2 harness; historical v1 gates cannot satisfy it. It does not amend prior reports, implement mixed-version compatibility, weaken release verification, or alter authentication/token lifetimes. The preserved original sources and the future-v2 diff under `artifacts/benchmarks/protocol/` make the changes reviewable.

When an appropriate isolated environment and resource budget are available, run a new, separately labeled full series from the project directory:

```powershell
.\.venv\Scripts\python.exe analysis\benchmark_v2.py --scale 10000 --workers 1 4 --repetitions 3 --label future-v2
.\.venv\Scripts\python.exe analysis\benchmark_v2.py --scale 100000 --workers 1 4 --repetitions 3 --label future-v2
```

These commands are future instructions, not evidence of an executed v2 performance matrix. Use the corresponding `.venv/bin/python` on Linux. Do not merge v2 timings into the prior v1 series.

## Focused authentication reproduction

```powershell
.\.venv\Scripts\python.exe analysis\check_postflight.py
```

This separate native proof uses actual PostgreSQL and a loopback HTTP server in a fresh owned verification database. A signed synthetic credential with its original30-minute lifetime backdated past expiry must return401; fresh fixture sign-ins must allow200 reads while retaining cross-tenant404. The proof neither waits30minutes nor changes the system clock or application authentication. Its result is `artifacts/benchmarks/protocol/postflight-expiry-proof.json`, with its own script/harness hashes and selected-source before/after check. It is separate analysis evidence, not a105th test, a restored deleted benchmark database, or grounds for relabeling the failed trial.
