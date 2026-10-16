"""Archive of original files, byte for byte as downloaded.

Default: a local directory, `backend/data/raw/<kind>/<yyyy-mm-dd>/<filename>`, addressed as
`local://raw/<kind>/<date>/<filename>`. When SUPABASE_URL and SUPABASE_SECRET_KEY are both
set, files go instead to the private Supabase Storage bucket `ra-raw` (created on first use)
and are addressed as `supabase://ra-raw/<kind>/<date>/<filename>`.

Same kind + date + filename overwrites: callers that can see different files under one name
(ingest does) put a content hash in the filename. The secret key is only ever sent in request
headers; it is never logged or put in an error message.
"""

import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import BACKEND_DIR, get_settings

log = logging.getLogger(__name__)

RAW_DIR = BACKEND_DIR / "data" / "raw"
BUCKET = "ra-raw"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_bucket_ready: set[str] = set()  # Supabase base URLs whose bucket is known to exist
_sleep = time.sleep  # indirection so tests don't wait out retry backoff


class ArchiveError(RuntimeError):
    """The file could not be stored. Never carries credentials."""


def _safe_part(name: str, fallback: str, max_len: int) -> str:
    """One path segment from untrusted text: no separators, no leading dots, bounded length."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE.sub("_", base).lstrip(".").strip("_")
    if len(base) > max_len:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: max_len - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else base[:max_len]
    return base or fallback


def object_key(kind: str, day: date, filename: str) -> str:
    return f"{_safe_part(kind, 'misc', 40)}/{day.isoformat()}/{_safe_part(filename, 'file', 150)}"


def _supabase() -> tuple[str, str] | None:
    url, key = os.environ.get("SUPABASE_URL", "").strip(), os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    return (url.rstrip("/"), key) if url and key else None


def _http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(get_settings().http_timeout_s, connect=20.0),
        headers={"User-Agent": "ra-backend/0.1 (+mutual fund data sync)"},
    )


def _fail(action: str, resp: httpx.Response, key: str) -> ArchiveError:
    body = resp.text.replace(key, "***")[:200]
    return ArchiveError(f"Supabase Storage {action} failed: HTTP {resp.status_code}: {body}")


def _ensure_bucket(client: httpx.Client, base: str, headers: dict[str, str], key: str) -> None:
    if base in _bucket_ready:
        return
    resp = client.post(f"{base}/storage/v1/bucket", headers=headers, json={"id": BUCKET, "name": BUCKET, "public": False})
    exists = resp.status_code == 409 or (resp.status_code == 400 and re.search(r"already exists|duplicate", resp.text, re.I))
    if not (resp.is_success or exists):
        raise _fail("bucket creation", resp, key)
    _bucket_ready.add(base)


def _upload(base: str, key: str, object_path: str, data: bytes, content_type: str) -> None:
    auth = {"apikey": key, "Authorization": f"Bearer {key}"}
    url = f"{base}/storage/v1/object/{BUCKET}/{quote(object_path, safe='/')}"
    headers = {**auth, "Content-Type": content_type or "application/octet-stream", "x-upsert": "true"}
    last: Exception | None = None
    with _http_client() as client:
        _ensure_bucket(client, base, auth, key)
        for attempt in range(3):
            try:
                resp = client.post(url, headers=headers, content=data)
            except httpx.TransportError as exc:  # message carries no headers, so no key
                last = exc
            else:
                if resp.is_success:
                    return
                if resp.status_code < 500 and resp.status_code != 429:
                    raise _fail("upload", resp, key)
                last = _fail("upload", resp, key)
            log.warning("Supabase upload of %s failed (attempt %d): %s", object_path, attempt + 1, last)
            if attempt < 2:
                _sleep(2 * (attempt + 1))
    raise ArchiveError(f"Supabase Storage upload failed after 3 attempts: {last}")


def archive_file(kind: str, filename: str, data: bytes, content_type: str, *, day: date | None = None) -> str:
    """Store `data` exactly as given and return its storage path (`local://...` or `supabase://...`).
    `day` defaults to today (UTC)."""
    key = object_key(kind, day or datetime.now(timezone.utc).date(), filename)
    remote = _supabase()
    if remote:
        _upload(remote[0], remote[1], key, data, content_type)
        return f"supabase://{BUCKET}/{key}"
    target = RAW_DIR / key
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data)
    os.replace(partial, target)  # never leaves a half-written file under the final name
    return f"local://raw/{key}"


def local_path(storage_path: str) -> Path:
    """Filesystem path of a `local://raw/...` storage path."""
    prefix = "local://raw/"
    if not storage_path.startswith(prefix):
        raise ValueError(f"not a local storage path: {storage_path!r}")
    path = (RAW_DIR / storage_path[len(prefix) :]).resolve()
    if RAW_DIR.resolve() not in path.parents:
        raise ValueError(f"storage path escapes the archive: {storage_path!r}")
    return path
