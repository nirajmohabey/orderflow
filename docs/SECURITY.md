# Security review and reproducible evidence

OrderFlow's security claim is bounded: authenticated tenant-scoped ingestion, immutable identities, atomic local PostgreSQL effects and explicit reporting failures were exercised with synthetic data. This is not an independent penetration test, a claim of exhaustive vulnerability absence or proof of a hosted production identity-provider integration.

## Dependency review and a fixed finding

The exact-version OSV audit and independent official npm audit are saved under `artifacts/security/`. The post-patch gate reports no matching known advisories for the 32 registry PyPI versions in the application lock, 81 dashboard npm versions, 60 locked dbt versions and the 134 distributions in the executed Linux Airflow environment. The application interpreter contains 33 distributions including the local OrderFlow package; that unpublished local project is not presented as a PyPI advisory query. Query timestamps, lock/environment hashes, every package query and full advisory responses are retained.

The initial audit found **dnspython 2.9.1 in the actual Airflow environment**, matching [the upstream WKS parser memory/CPU advisory](https://github.com/rthalley/dnspython/security/advisories/GHSA-vvf4-9x63-8xvv). It was upgraded to the patched 2.9.2. `scripts/check_dns_bounds.py` verifies the largest valid service number and rejects the first out-of-range value using two harmless in-memory cases, without an exhaustion-sized allocation or network request. The patched installed graph and passing parser result are recorded in `airflow-installed.json` and `dns-bounds.json`; original findings/graph remain in `before-dns-patch/`.

The official 720-entry Airflow all-extras constraints file is preserved byte-for-byte. A reviewed hardened copy changes only dnspython 2.9.1→2.9.2; its derivation and hashes are in `airflow-constraint-hardening.json`. The workflow uses the selected base Airflow/psycopg environment. Unused LiteLLM, SoupSieve and Tornado versions in the broad constraints inventory still match advisories and remain visibly reported. Those packages were absent from the executed selected Airflow graph. The audit does not silently suppress their findings or claim all optional providers were installed/tested.

Package advisory matching is time-specific. It does not establish exploitability, absence of unreported issues, built-image OS safety or the security of GitHub Actions' own dependencies. Distribution/platform differences are explicit rather than collapsed into a claimed universal installation. The [OSV API](https://google.github.io/osv.dev/post-v1-querybatch/) and [official npm registry audit](https://docs.npmjs.com/cli/v11/commands/npm-audit/) receive only public package names/versions, not application data or credentials.

## Static findings retained and reviewed

Bandit 1.9.4 scans runtime code, maintenance/verification tools and the Airflow DAG with every severity retained. The captured report has **zero high, eleven medium and 45 low findings**; `bandit.json` contains all findings, and `static-triage.json` records individual medium call sites, source hashes and bounded assessments. No `nosec` suppression or severity filter was used to manufacture a zero count.

The nine SQL-construction warnings were reviewed against their sources: fixed table-name tuples in the recovery observer; fixed column/keyset fragments with bound values in the service; and model identifiers produced only from a fixed name enum plus a parsed UUID in the pipeline. The URL-open warning is a local audit helper called only with fixed HTTPS OSV endpoints and URL-encoded advisory IDs. The XML warning concerns trusted local pytest-generated JUnit artifacts in the release runner, not a runtime upload. These assessments depend on the captured call sites and validators; they are not approval to interpolate arbitrary SQL identifiers, fetch untrusted arbitrary URLs or parse arbitrary third-party XML.

Low findings concern argument-array maintenance subprocesses, executable lookup/PATH, a seeded synthetic benchmark shuffle and the `token_type="bearer"` protocol field. Hosts, executable configuration and PATH remain trusted. Credentials use `secrets`; the seeded generator is not a credential generator.

The static gate preserves all raw findings and fails for high findings, scanner errors, focused Ruff undefined-name findings, changed/unreviewed medium signatures or changes to their containing files. This is a review aid, not a complete security verifier. Focused Ruff F821/F822/F823 returned no findings. Audit tools live in a separate environment and were not added to the frozen application graph.

## Runtime boundaries exercised

Core and independent acceptance/recovery checks cover source-key tenant/source binding, server-maintained JWT memberships, forced RLS with the actual nonowner runtime role, pooled-context clearing, composite foreign keys, immutable envelopes/revisions/receipts, duplicate identity races, typed-input/JSON byte/depth bounds, late/equal-time revisions and process death on both sides of commit. Retries preserve terminality, bound durable attempts and audit operator decisions. Cross-tenant reads return 404. These tests operate on fresh owned verification databases and do not reset the serving inbox.

The connector review also found that checking response length after transparent HTTP decompression could exceed the intended memory budget. The adapter now requests identity encoding, rejects unsupported content encodings before consuming a stream and reads bounded chunks. Regression cases include a never-read compressed stream, so rejection is established before inflation. This connector is a configured administrative upstream, not a public arbitrary-URL endpoint. HTTP acceptance, response byte bounds, page cursor compare-and-set and interrupted-page replay remain separately tested.

Inventory reads observed/reference CSV independently, validates scope/shape/size and preserves both hashes. Object keys are constrained and resolved under an owned storage root. Publication admits checksum/count-verified artifacts only after actual dbt checks; a failed attempt preserves the last-good report and visible stale state. Local files are not represented as shared cloud storage, and an injected S3 adapter test is not claimed as an executed AWS connection.

RLS mitigates missing filters and crossed relationships under trusted server context. It does not claim to contain a stolen application database credential or compromised server that deliberately chooses another context. Authentication/authorization checks do not rely on model output, client roles, fixture body fields or an arbitrary SQL API. See [the backend contract](BACKEND.md) for the exact counter, timestamp, pagination and deployment boundaries.

## Source exposure and CI

`scripts/security_audit.py --source` checks the Git index and unignored working files against generated local environment/credential-registry values and common credential prefixes without printing values. It includes ignored application/Vercel/container environment files when present and the private local/source-key/CI registries. It flags accidentally publishable runtime/environment directories. The saved current scan has no findings; its exact count/time is in `source-exposure.json`. Binary contents, deleted Git history and external services are excluded. This limited scan is not an exhaustive secret detector; re-run it after final release edits.

The supplied GitHub workflow uses verified full commit pins for official actions, read-only repository permissions and `persist-credentials:false`. It runs a fresh ephemeral PostgreSQL service with generated local role/source credentials, refuses existing CI roles/databases/configuration, and never calls the Windows cluster bootstrap on Linux. No cloud deployment credential is granted. Separate jobs exercise real PostgreSQL, finite Airflow/dbt, a browser against the actual API and the shipped Docker stack. Private `.env`/`.runtime` data is excluded from uploads. Raw medium findings remain reviewable in CI instead of being discarded.

Workflow YAML/pins, helper compilation and refusal guards were checked locally. A positive bootstrap proof also created a separate native PostgreSQL17 cluster and copied fixture checkout: CI roles/databases, API readiness, same-origin sign-in, durable ingestion/projection and Airflow metadata DDL privileges worked; repeating setup refused existing objects, and the owned cluster was stopped. See `artifacts/ci/bootstrap-native-proof.json`. **GitHub-hosted execution is not claimed green:** this native helper proof is not a Linux workflow run, and the account's earlier runner/billing restriction is separate from application correctness. The manually executed Linux, Docker, browser and pipeline evidence retains its own artifact and denominator.

## Reproduce

```text
python scripts/security_audit.py
python scripts/collect_dependencies.py --output artifacts/security/application-installed.json
```

Run the collector with the actual Airflow/dbt interpreters for their respective installed graphs. In a separate tool environment containing Bandit 1.9.4/Ruff 0.17.6, run `python scripts/static_audit.py`. A required installed-graph omission, advisory, unreviewed medium result or source-exposure finding returns nonzero. The DNS regression uses the patched Airflow interpreter. Keep all before/after evidence and investigate failures; a skipped or blocked check is not a pass.
