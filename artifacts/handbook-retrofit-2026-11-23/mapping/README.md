# Synthetic customer-mapping evidence

This packet implements the revised handbook's one noncanonical OrderFlow mapping example. Maple is fictional; the reviewer/operator are automated. No external customer or human sign-off, new hosted merchant integration, paid service or business saving is claimed.

- [Mapping and handoff](../../../docs/CUSTOMER_MAPPING.md)
- [One-shape offline normalizer](../../../scripts/customer_catalog_mapping.py)
- [Raw synthetic input](../../../fixtures/customer_mapping/maple_catalog.json)
- [Independent literal expected output](../../../fixtures/customer_mapping/maple_catalog.expected.jsonl)
- [Acceptance tests](../../../tests/test_customer_mapping.py)
- [Offline JUnit result](offline-tests.xml): actual run, 22 passed / 1 real HTTP/PostgreSQL integration test deliberately deselected; this is not a full-suite gate or integration result.
- [Component manifest](component-manifest.json): exact normalizer/fixture/test hashes stamped after that offline invocation; the separate full-suite proof supplies before/after source binding.

The [completed acceptance extraction](acceptance.json) records 23/23 mapping tests, including the real HTTP/PostgreSQL workflow, within the successful 196-test full suite. Its exact source proof and JUnit paths are linked in that record. A [separate setup diagnostic](integration-diagnostic.xml) retained one ConnectionTimeout before app assertions when the disposable cluster process had stopped; it is not silently counted as an application pass or as part of the successful rerun. Core connectors, schema, dependencies, hosted API and source credentials are unchanged. The integration uses existing `catalog_app`, `ingest_catalog`, cursor handling, source authentication and worker transactions with an owned disposable PostgreSQL database, not mocked database success.

[Full-suite source proof](../../../artifacts/handbook-retrofit-2026-11-23/native/full-suite.source-proof.json) Â· [Full-suite JUnit](../../../artifacts/handbook-retrofit-2026-11-23/native/full-suite.xml)
