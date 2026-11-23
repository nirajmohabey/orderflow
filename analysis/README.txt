ORDERFLOW BENCHMARK ANALYSIS

This optional, offline renderer is separate from the application runtime.
It reads only the selected completed summaries and their bound raw evidence.
Its default mode executes the release verifier's full benchmark checks and
refuses failed or incomplete series. Explicit partial-audit mode instead checks
all original source/raw hashes and preserves the known failed API postflight.
It does not connect to PostgreSQL or start a workload. No results are replaced.

From the project root, with the pinned Python interpreter available:

  uv run --frozen --with matplotlib==3.11.2 python analysis/render_benchmarks.py --partial-audit

Without --partial-audit the current larger series is correctly rejected.

Matplotlib 3.11.2 was the actual renderer used. This optional analysis package
is not installed into the production images and is outside their selected
application/dependency advisory scope. The recorded package version and
renderer source SHA-256 appear in artifacts/analysis/summary.json.

Outputs are written alongside the project as OrderFlow_Analysis.html and
OrderFlow_Benchmark.png, and copied into artifacts/analysis/. The standalone
HTML includes both workload sizes, every trial table, keyboard-operated
workload controls, source/summary hashes and metric definitions. It uses no
external scripts, fonts or network requests. Printing shows both workloads.

Throughput is distinct terminal receipts divided by HTTP start-to-observed
drain duration. Acceptance latency and first observed committed visibility
are separate populations. Visibility is an observation upper bound including
polling/query overhead, not an exact commit timestamp. Latency dots and table
cells are individual trial p95 values, never a pooled or averaged p95.

Separate microVMs introduce host/cache/I/O differences. These synthetic closed-
loop tests do not establish a causal same-host speedup, hosted service SLO,
Airflow/dbt capacity or customer effect. The full raw selected evidence is in
the release ZIP; ignored raw files are not ordinarily part of a Git clone.
