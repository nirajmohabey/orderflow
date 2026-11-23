"""Immutable, checksum-verified object storage. Local files are not cloud hosting."""
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Protocol
from uuid import uuid4

MAX_OBJECT_BYTES = 8 * 1024 * 1024


class ObjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredObject:
    key: str
    sha256: str
    bytes: int

    def as_dict(self):
        return asdict(self)


def validate_key(key: str) -> str:
    if (not isinstance(key, str) or len(key) > 1000 or not key
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in key.split("/"))):
        raise ObjectError("Invalid object key")
    return key


class ObjectStore(Protocol):
    kind: str
    def put_immutable(self, key: str, data: bytes) -> StoredObject: ...
    def get(self, key: str, *, expected_sha256: str | None = None) -> bytes: ...


class LocalObjectStore:
    kind = "local"

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ObjectError("Object key escapes its storage root")
        return path

    def get(self, key, *, expected_sha256=None):
        path = self._path(key)
        try:
            if path.stat().st_size > MAX_OBJECT_BYTES:
                raise ObjectError("Object exceeds the byte limit")
            with path.open("rb") as source:
                data = source.read(MAX_OBJECT_BYTES + 1)
        except OSError as error:
            raise ObjectError("Object is unavailable") from error
        if len(data) > MAX_OBJECT_BYTES or (expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256):
            raise ObjectError("Object checksum or byte limit mismatch")
        return data

    def put_immutable(self, key, data):
        if not isinstance(data, bytes) or len(data) > MAX_OBJECT_BYTES:
            raise ObjectError("Object exceeds the byte limit")
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path = self._path(key)  # Recheck after creating parents.
        digest = hashlib.sha256(data).hexdigest()
        temporary = path.parent / (".staging-" + uuid4().hex)
        try:
            with temporary.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                # Linking a complete inode is atomic and never replaces a key.
                # A crash before this leaves an unpublished temporary object.
                os.link(temporary, path)
            except FileExistsError:
                if self.get(key, expected_sha256=digest) != data:
                    raise ObjectError("Immutable object identity conflict")
        except OSError as error:
            raise ObjectError("Immutable object publication failed") from error
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(key, digest, len(data))


class S3ObjectStore:
    """Optional injected-client adapter. Tests exercise the contract without AWS."""
    kind = "s3"

    def __init__(self, client, bucket: str, prefix: str = ""):
        self.client, self.bucket = client, bucket
        self.prefix = validate_key(prefix) + "/" if prefix else ""

    def get(self, key, *, expected_sha256=None):
        result = self.client.get_object(Bucket=self.bucket, Key=self.prefix + validate_key(key))
        body = result["Body"]
        try:
            if result.get("ContentLength", MAX_OBJECT_BYTES + 1) > MAX_OBJECT_BYTES:
                raise ObjectError("Object exceeds the byte limit")
            data = body.read(MAX_OBJECT_BYTES + 1)
        finally:
            body.close()
        if len(data) > MAX_OBJECT_BYTES or (expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256):
            raise ObjectError("Object checksum or byte limit mismatch")
        return data

    def put_immutable(self, key, data):
        key = validate_key(key)
        if not isinstance(data, bytes) or len(data) > MAX_OBJECT_BYTES:
            raise ObjectError("Object exceeds the byte limit")
        digest = hashlib.sha256(data).hexdigest()
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data,
                                   ContentLength=len(data), Metadata={"sha256": digest}, IfNoneMatch="*")
        except Exception as error:
            status = getattr(error, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise ObjectError("Conditional object publication failed") from error
            if self.get(key, expected_sha256=digest) != data:
                raise ObjectError("Immutable object identity conflict")
        return StoredObject(key, digest, len(data))


class PostgresObjectStore:
    """Immutable bytea objects scoped by forced RLS; no function disk dependency."""
    kind = "postgres"

    def __init__(self, tenant_id):
        from uuid import UUID
        self.tenant_id = str(UUID(str(tenant_id)))

    def get(self, key, *, expected_sha256=None):
        from sqlalchemy import text
        from .db import tenant_transaction
        key = validate_key(key)
        with tenant_transaction(self.tenant_id,role="pipeline") as conn:
            row = conn.execute(text("SELECT content,sha256 FROM orderflow.stored_objects WHERE tenant_id=:t AND object_key=:k"),
                               {"t":self.tenant_id,"k":key}).one_or_none()
        if row is None: raise ObjectError("Object is unavailable")
        data = bytes(row[0])
        digest = hashlib.sha256(data).hexdigest()
        if len(data)>MAX_OBJECT_BYTES or digest != row[1] or (expected_sha256 and digest != expected_sha256):
            raise ObjectError("Object checksum or byte limit mismatch")
        return data

    def put_immutable(self, key, data):
        from sqlalchemy import text
        from .db import tenant_transaction
        key = validate_key(key)
        if not isinstance(data,bytes) or len(data)>MAX_OBJECT_BYTES: raise ObjectError("Object exceeds the byte limit")
        digest = hashlib.sha256(data).hexdigest()
        with tenant_transaction(self.tenant_id,role="pipeline") as conn:
            conn.execute(text("INSERT INTO orderflow.stored_objects(tenant_id,object_key,sha256,content) VALUES(:t,:k,:h,:v) ON CONFLICT DO NOTHING"),
                         {"t":self.tenant_id,"k":key,"h":digest,"v":data})
            existing = conn.execute(text("SELECT sha256,content FROM orderflow.stored_objects WHERE tenant_id=:t AND object_key=:k"),
                                    {"t":self.tenant_id,"k":key}).one()
            if existing[0] != digest or bytes(existing[1]) != data: raise ObjectError("Immutable object identity conflict")
        return StoredObject(key,digest,len(data))
