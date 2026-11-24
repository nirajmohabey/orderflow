# Bounded Airflow SMTP dependency correction

A fresh November 23, 2026 advisory query found `apache-airflow-providers-smtp==3.2.0` in the recorded selected Airflow environment. OSV `PYSEC-2026-4396` marks versions from 2.0.0 up to, but excluding, 3.2.2 as affected. The [upstream Apache fix](https://github.com/apache/airflow/pull/65346) passes a validating SSL context when upgrading an SMTP connection with STARTTLS. Without that validation, the SMTP credential exchange could be exposed to an intermediary with an untrusted certificate. See the [Apache advisory reference](https://lists.apache.org/thread/6v2ds757000msmjmovnnqryqzks83ps0) and retained [advisory data](advisory.json).

This finding concerns the optional separate local/Linux Airflow environment, not the Vercel FastAPI/Queue runtime, Neon authentication service or an observed compromise. The application, dashboard and dbt locked graphs returned zero known-advisory matches in the same query; npm reported zero. No SMTP service was contacted and no email was sent.

## Narrow change and compatibility

Only one pin changes in the [hardened constraints](../../../../orchestration/constraints-hardened-3.12.txt): SMTP provider 3.2.0 -> 3.2.2. The original official all-extras constraints remain unchanged as design/install history. The earlier dnspython 2.9.2 fix is retained. No application dependency, database, cloud service or billing setting changes.

[Official 3.2.2 package metadata](https://pypi.org/pypi/apache-airflow-providers-smtp/3.2.2/json) requires Python >=3.11, Airflow >=2.11.0, common-compat >=1.12.0 and aiosmtplib >=5.1.0. The existing selected pins satisfy these bounds: Python 3.12.14, Airflow 3.4.0, common-compat 1.23.0 and aiosmtplib 5.1.3. [Saved metadata](package-metadata.json) retains both versions and distribution hashes.

The Linux/Python3.12.14 resolver completed a 134-package selected graph with the fixed pin, without changing an installed environment. A fresh OSV exact-version query found zero advisory matches in that resolved graph. That is dependency compatibility/advisory evidence, not proof of successful installation or execution.

- [Pin-change hashes](pin-change.json): before/after hardened constraints and preserved official source hash.
- [Resolved Linux graph](candidate-linux-resolved.txt): exact 134 packages selected by uv 0.11.25.
- [Resolved-graph advisory query](resolved-graph-audit.json): every requested package/version and raw result.
- [Original full audit](../dependencies/audit-summary.json): the failed selected historical installed graph is retained rather than relabeled fixed.
- [Recorded installed snapshot scope](../dependencies/snapshot-scope.json): original interpreter/platform/timestamps remain explicit.

The matching [GitHub Actions run](https://github.com/nirajmohabey/orderflow/actions) must separately install the exact hardened constraints on Linux, pass `pip check`, run the bounded DNS parser regression, execute the real finite Airflow DAG/dbt workflow and collect its installed graph before the change is described as executed. Local resolution does not stand in for those gates. Source-bound old Linux results remain historical.

Known-advisory matching is time-bound. It does not establish exploitability of every optional feature, audit container/host OS packages, or prove absence of other vulnerabilities.
