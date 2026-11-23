"""Strict ingress data and deterministic semantic identities."""
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

MAX_INTEGER = 9223372036854775807
MAX_MONEY = 1000000000000000


def valid_text(value):
    if "\x00" in value or not value.strip():
        raise ValueError("Text must be nonblank and contain no NUL")
    value.encode("utf-8", errors="strict")
    return value


class ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Product(ExactModel):
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    unit_price_minor: StrictInt = Field(ge=0, le=MAX_MONEY)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    _text = field_validator("sku", "name")(valid_text)


class Order(ExactModel):
    customer_id: str = Field(min_length=1, max_length=200)
    total_minor: StrictInt = Field(ge=0, le=MAX_MONEY)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: Literal["placed", "paid", "cancelled", "fulfilled"]
    _text = field_validator("customer_id")(valid_text)


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("Timestamps require RFC3339 timezone and at most six fractional digits")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    if not 2000 <= parsed.year <= 2100:
        raise ValueError("Timestamp is outside the supported 2000–2100 range")
    return parsed


def partition_key(entity_type, entity_id):
    literal = entity_type + ":" + entity_id
    if len(literal.encode("utf-8")) <= 128:
        return literal
    return entity_type + ":sha256:" + hashlib.sha256(entity_id.encode("utf-8")).hexdigest()


class Envelope(ExactModel):
    tenant_id: UUID
    source_event_id: str = Field(min_length=1, max_length=200)
    partition_key: str = Field(min_length=1, max_length=128)
    schema_version: Literal["1"]
    entity_type: Literal["product", "order"]
    entity_id: str = Field(min_length=1, max_length=200)
    entity_version: StrictInt = Field(ge=1, le=MAX_INTEGER)
    effective_at: datetime
    occurred_at: datetime
    payload: Product | Order
    _text = field_validator("source_event_id", "partition_key", "entity_id")(valid_text)
    _time = field_validator("effective_at", "occurred_at", mode="before")(parse_time)

    @model_validator(mode="after")
    def matching_payload(self):
        expected = Product if self.entity_type == "product" else Order
        if not isinstance(self.payload, expected):
            raise ValueError("Entity type and payload shape must match")
        if len(self.partition_key.encode("utf-8")) > 128:
            raise ValueError("Partition key exceeds 128 UTF-8 bytes")
        return self


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON keys are forbidden")
            result[key] = value
        return result
    depth = 0
    quoted = escaped = False
    for char in raw.decode("utf-8", errors="strict"):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 16:
                raise ValueError("JSON nesting exceeds 16 levels")
        elif char in "]}":
            depth -= 1
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite numbers are forbidden")))


def parse_envelope(raw):
    return Envelope.model_validate(strict_json(raw))


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_payload(envelope):
    return canonical_json(envelope.payload.model_dump())


def canonical_envelope(envelope):
    value = envelope.model_dump(mode="json")
    for name in ("effective_at", "occurred_at"):
        value[name] = getattr(envelope, name).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return canonical_json(value)


class RetryRequest(ExactModel):
    reason: str = Field(min_length=8, max_length=500)
    _text = field_validator("reason")(valid_text)
