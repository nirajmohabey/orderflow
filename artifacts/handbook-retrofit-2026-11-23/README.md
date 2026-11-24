# OrderFlow handbook update and review

Starting commit: `b46164173606eb045f11dd05ca8932f49b755ac0`, reviewed November 23, 2026 (America/New_York). JSON timestamps use UTC. The retailer, merchant export and all acceptance inputs are synthetic. Reviewers are automated agents; no human customer accepted a pilot.

## Changes and actual checks

| Change / check | Evidence and result |
|---|---|
| One noncanonical merchant field map and handoff | [Customer mapping](../../docs/CUSTOMER_MAPPING.md); an offline one-shape normalizer, literal oracle and existing connector path |
| Fresh native backend/integration run | [196 tests](native/full-suite.source-proof.json), zero failures/errors/skips, unchanged inputs before/after; includes real dbt and 23 new mapping checks |
| New mapping acceptance | [23/23 outcomes](mapping/acceptance.json); one composite real HTTP/PostgreSQL case exercises interrupted-page resume, durable cursor, replay, late versions, conflict and tenant isolation |
| Native isolation | [Cleanup record](native/isolation.json): only a new loopback cluster; original environment/process records unchanged; cluster stopped |
| Interrupted setup | [Initial interruption](native/interrupted-setup.json) and [one-case setup diagnostic](mapping/integration-diagnostic.xml) retain the stopped-cluster failure; neither is counted as an application pass |
| Built dashboard, sessions and layout | [46/46 checks](review/README.md), including 320/375/768/1440 layouts, 200% text, all tabs, exact integers, authorization/retry/session races, 25 axe states with zero violations; incomplete contrast findings remain disclosed |
| Known dependency advisory | [SMTP fix](review/smtp-fix/README.md): separate optional Linux Airflow constraint 3.2.0 -> 3.2.2 for PYSEC-2026-4396; the resolved 134-package candidate has zero known-advisory matches; metadata resolution is distinct from the subsequent CI install/DAG execution |
| Runtime dependency checks | [Current review](review/summary.json): app/dashboard/dbt selected graphs have no known-advisory matches; the original Airflow finding and old snapshots remain preserved |
| Completed v2 benchmark | [Independent validation](benchmark-status.json) and [durable release](https://github.com/nirajmohabey/orderflow/releases/tag/benchmark-v2-2026-11-23): twelve trials, 228 recorded assertions, 28 raw gzip checks; original v1 remains `not_verified` |
| Documentation consistency | [Correction record](documentation-corrections.json): current hosting/Neon identity/private Queues and completed v2 results replace stale future-work statements |
| Current source and links | [Scoped verification](verification.json): exact execution inputs, unchanged hosted runtime components, local links and privacy/whitespace checks |

The existing [Linux/PostgreSQL/Airflow/dbt/browser and Docker CI](../../.github/workflows/acceptance.yml) runs on the published source, installs the patched Airflow constraints and executes the real finite DAG. Inspect the [actual run](https://github.com/nirajmohabey/orderflow/actions) for its commit-bound outcome; a committed workflow or a resolved dependency graph alone is not that result.

## Production and interpretation boundaries

The deployed architecture remains Vercel Hobby, Neon, Managed Better Auth and private finite Queues. No account, migration, paid service, remote merchant API or hosting replacement was added. The offline mapping does not become an exposed web endpoint. Public signup alone does not grant membership, source keys stay server-side, and model/customer-data claims are not added.

Real owner login and hosted ingestion/Queue/dbt/inventory observations retain their earlier [production provenance](../../docs/PRODUCTION.md); this update does not relabel them as fresh authenticated production journeys. Both live stable aliases are separately checked after publication, with readiness, exact served assets and anonymous authorization boundaries. No user password or verification code is used by this review.

The v2 benchmark measures a shared Linux HTTP/PostgreSQL core with synthetic load, not managed free-plan capacity. Sequential medians do not establish causal scaling speedups. Polling visibility p95 reached 326.59 seconds at 100k; backlog remains material. No SLO, customer adoption or business savings is claimed. Human accessibility review, real merchant acceptance, full-account recovery and host-OS auditing retain their separate limits.
