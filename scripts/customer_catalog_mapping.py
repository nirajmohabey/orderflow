"""One synthetic merchant export mapping; emits existing canonical JSONL offline.

This is a bounded example, not a configurable connector framework. It neither
contacts a merchant nor loads credentials or writes to a database.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import re
from uuid import UUID

from orderflow.schemas import Envelope, canonical_envelope, partition_key, strict_json

MAX_INPUT_BYTES = 1024 * 1024
MAX_RECORDS = 1000
FIELDS = {"notification_id", "product_code", "revision", "valid_from", "emitted_at",
          "stock_keeping_unit", "display_title", "unit_price", "currency"}


class MappingError(ValueError):
    pass


def map_catalog(raw: bytes, *, tenant_id: str, merchant_code: str) -> list[dict]:
    """Normalize exactly the reviewed Maple export, retaining upstream identities.

    The tenant and expected merchant are operator-reviewed configuration. They
    are not taken from a customer-supplied tenant/role field. Source-key/API
    authorization remains the unchanged connector's responsibility.
    """
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise MappingError("mapping_input_size")
    tenant = str(UUID(tenant_id))
    if not isinstance(merchant_code, str) or not re.fullmatch(r"[a-z0-9-]{1,60}", merchant_code):
        raise MappingError("mapping_merchant_config")
    document = strict_json(raw)
    if not isinstance(document, dict) or set(document) != {"merchant_code", "catalog_updates"}:
        raise MappingError("mapping_document_fields")
    if document["merchant_code"] != merchant_code:
        raise MappingError("mapping_merchant_mismatch")
    records = document["catalog_updates"]
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
        raise MappingError("mapping_record_limit")
    result = []
    for record in records:
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise MappingError("mapping_record_fields")
        price = record["unit_price"]
        # USD is the one reviewed fixture currency. Floats, exponent notation,
        # grouping separators and rounding from extra precision are forbidden.
        if not isinstance(price, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,13})\.[0-9]{2}", price):
            raise MappingError("mapping_price_precision")
        if record["currency"] != "USD":
            raise MappingError("mapping_currency")
        entity = record["product_code"]
        if not isinstance(entity, str):
            raise MappingError("mapping_product_identity")
        envelope = Envelope.model_validate({
            "tenant_id": tenant,
            "source_event_id": record["notification_id"],
            "partition_key": partition_key("product", entity),
            "schema_version": "1",
            "entity_type": "product",
            "entity_id": entity,
            "entity_version": record["revision"],
            "effective_at": record["valid_from"],
            "occurred_at": record["emitted_at"],
            "payload": {"sku": record["stock_keeping_unit"], "name": record["display_title"],
                        "unit_price_minor": int(Decimal(price) * 100), "currency": record["currency"]},
        })
        # Reuse the public canonical contract, including normalized UTC instants.
        result.append(json.loads(canonical_envelope(envelope)))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--merchant", required=True)
    args = parser.parse_args(argv)
    # Bound the read before parsing, even if the input grows after a size check.
    with args.input.open("rb") as source:
        raw = source.read(MAX_INPUT_BYTES + 1)
    try:
        rows = map_catalog(raw, tenant_id=args.tenant, merchant_code=args.merchant)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        parser.exit(2, "Customer mapping rejected; inspect the documented field and configuration contract.\n")
    for row in rows:
        print(json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
