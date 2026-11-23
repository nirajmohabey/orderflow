# Container OS advisory audit

> Patch version note: Current v0.1.1 native evidence is in `artifacts/recheck-2026-11-23/`. The earlier Linux/Airflow/container/OS and measured v1 workload results below are historical v0.1.0 evidence. Use `scripts/review_patch.py` for the current native review. The legacy release/partial-render tools require their matching original source; no current full performance matrix or Linux rerun is claimed.

On 23 November 2026 the final OrderFlow Compose images were rebuilt and exercised in an owned, private Linux sandbox. Python remains 3.12.14 and PostgreSQL remains 17.12. This is a package advisory scan and functional smoke, not a penetration test or evidence that the application has no vulnerabilities.

| Runtime image | Original OS package findings | Final findings | Final findings with a supplied fixed version |
|---|---:|---:|---:|
| API, worker, init, batch — each | 292 | 217 | 0 |
| PostgreSQL | 498 | 0 | 0 |

The four Python images share the same Debian packages; their counts are repeated package/advisory pairs, not four independent vulnerability populations. Each final image contains 97 distinct advisory identifiers: 50 high, 74 medium, 79 low and 14 unknown package/advisory pairs. “No supplied fixed version” means the captured Debian advisory database offers no fix for that installed release, not that a finding is harmless. The PostgreSQL result means zero OS advisories reported for that exact image by this scanner/database snapshot.

The Python images apply only reviewed Debian 13 package updates: apt, certificate roots, glibc, PCRE2, xz, zlib and OpenSSL. Actual resulting versions include glibc `2.41-12+deb13u5` and OpenSSL `3.5.9-1~deb13u3`. Application, npm and dbt dependencies were not changed by this pass. Package inventories and exact image/base digests are in [runtime-package-evidence.json](../artifacts/security/container-os-audit/runtime-package-evidence.json).

The original Debian PostgreSQL image includes libxml2's old 2.9 ABI and [CVE-2026-6653](https://security-tracker.debian.org/tracker/CVE-2026-6653), for which Debian 13 supplies no fix in the captured tracker. The selected `postgres:17.12-alpine` base uses libxml2 2.13.9. Its eleven initial package/advisory pairs had fixes; targeted `libcrypto3`, `libssl3` and `libxml2` updates removed the reported findings. No package metadata was deleted or suppressed. PostgreSQL's engine version was checked after the rebuild.

Alpine uses musl rather than glibc. This change was validated with a fresh synthetic Compose database; it is not proof that arbitrary existing database volumes or locale-dependent applications can be moved unchanged. The official image documents [Alpine/locale differences and ICU support](https://github.com/docker-library/docs/blob/master/postgres/README.md#image-variants). Existing native and benchmark databases were not changed.

## Residual Debian findings

The complete unfiltered findings remain in the JSON reports. The high-severity families include:

- util-linux mount/nsenter issues, repeated across binary packages. The [Debian tracker](https://security-tracker.debian.org/tracker/CVE-2026-76642) describes privileged mount operations and supplies no Debian 13 fix. The API runs as UID 10001; its JSON/event workflows do not invoke these utilities. This reduces the direct request path but does not establish immunity after arbitrary code execution or host misconfiguration.
- libacl pathname/symlink behavior. [Debian](https://security-tracker.debian.org/tracker/CVE-2026-54369) records that a privileged caller and attacker-controlled path components are required. No application ACL API is exposed; the package remains present and the finding is retained.
- ncurses `infocmp` parsing, systemd-homed signature verification and Perl Archive::Tar parsing. These are not operations invoked by the declared worker/API workflows. This is a source-level boundary assessment, not exploit testing or a blanket reachability exclusion.
- [OpenSSL QUIC stream retention](https://security-tracker.debian.org/tracker/CVE-2026-124709). The application uses ordinary HTTP/TLS through Python libraries, not OpenSSL's QUIC server API. The unfixed advisory is still reported.

Do not use this report to claim zero container vulnerabilities. Re-scan when upstream distributions publish fixes or before a deployment. The Dockerfiles select fixed language/database versions but resolve OS repositories at build time; later builds can have different image IDs and must generate their own evidence.

## Method and functional evidence

Trivy **0.76.2** was downloaded from its [official immutable release](https://github.com/aquasecurity/trivy/releases/tag/v0.76.2). Both the published checksum list and Linux archive were checked against saved SHA-256 values. The scanner ran as `nobody` (UID 65534), with an empty environment and no read or write access to the Docker socket. A separate orchestration process saved each owned image to a TAR; the scanner read that TAR with `--input`, `--offline-scan`, `--scanners vuln` and `--pkg-types os`. It did not execute image entrypoints. No GitHub Actions scanner integration was used.

[Scanner/database provenance](../artifacts/security/container-os-audit/scanner-provenance.json) records the binary hash, anonymous official vulnerability database repository, database hash and 2026-11-23 update timestamp. [Final scan](../artifacts/security/container-os-audit/os-audit.json) contains exact image IDs and package findings. It also retains the old Debian PostgreSQL image as a clearly tagged reference, not as a selected Compose runtime. [Original findings](../artifacts/security/container-os-before.json) and the [unpatched Alpine candidate](../artifacts/security/postgres-alpine-before.json) are preserved.

The [final Docker smoke](../artifacts/checks/docker-final-os-smoke.json) verifies:

- frozen application source plus migrations 001–004, with per-file hashes;
- API readiness over a real loopback socket, ten accepted fixture identities and the literal 8,000-cent current order total;
- worker/API runtime environments contain no administrative or transform database credentials;
- actual dbt execution and checked-report publication;
- real HTTP ingestion: EUR version 4 is quarantined, changed USD content under version 4 is quarantined as `entity_version_conflict`, and corrected version 5 applies;
- PostgreSQL 17.12, and successful cleanup of only the owned Compose test volumes.

This smoke is narrower than the separate 104-test PostgreSQL suite. No public ports were published, no images were pushed, and no native serving or benchmark state was reset. The owned audit sandbox was stopped after collecting the evidence.
