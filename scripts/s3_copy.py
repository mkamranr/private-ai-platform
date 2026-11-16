"""Off-host backup copies to S3-compatible storage, for the ops runner (E31).

Stdlib only, like the runner: it runs on the host with whatever Python is there, and an
air-gapped host has no package index to fetch boto3 from. What it needs of S3 is small: HEAD, PUT,
GET (list), DELETE and multipart upload, signed with AWS Signature Version 4. Addressing
is path-style (``https://host:port/bucket/key``), which MinIO, Ceph RGW, Garage and AWS
all accept, so the usual target is the site's own object store.

**Layout.** A backup ``NAME`` becomes objects ``PREFIX/NAME/<relative path>``, and last
of all ``PREFIX/NAME/.complete``: a small JSON file listing every file's SHA-256. A copy
counts only with that marker, so an interrupted upload is never mistaken for a copy.

**Integrity.** Every request is signed over the SHA-256 of its body
(``x-amz-content-sha256``), and S3 refuses a body that does not match. So each file, or
each part of a large one, is checked by the server on arrival; nothing is read back.

**Secrets.** The access and secret keys come from the host's ``.env`` and are never
logged or reported: the console sees the endpoint, bucket and prefix only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

#: Files larger than this go up in parts of this size (S3's single PUT tops out at 5 GB,
#: and a part is held in memory to be hashed and signed).
PART_SIZE = 64 * 1024 * 1024
MARKER = ".complete"
#: For the checks made with every heartbeat (is the bucket there, what is copied): an
#: endpoint that hangs must not stall the runner for the upload timeout.
PROBE_TIMEOUT = 5.0
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3Error(Exception):
    """A request S3 refused, or could not be made. The message is safe to show."""


# ---------------------------------------------------------------------------
# Signature Version 4
# ---------------------------------------------------------------------------
def _quote(value: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(value, safe=safe)


def canonical_query(params: dict[str, str]) -> str:
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(params.items()))


def sign(
    *,
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    headers: dict[str, str],
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    now: dt.datetime,
) -> dict[str, str]:
    """The headers to send, including ``Authorization``. ``path`` is already encoded."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    all_headers = {
        **{k.lower(): v.strip() for k, v in headers.items()},
        "host": host,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }
    names = sorted(all_headers)
    canonical = "\n".join(
        [
            method,
            path,
            canonical_query(query),
            "".join(f"{n}:{all_headers[n]}\n" for n in names),
            ";".join(names),
            payload_sha256,
        ]
    )
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = f"AWS4{secret_key}".encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    all_headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    all_headers.pop("host")  # urllib sets it from the URL
    return all_headers


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------
class S3Target:
    """One bucket and prefix on one endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        prefix: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        ca_file: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise S3Error(f"OPS__BACKUP_COPY_S3_ENDPOINT is not an http(s) URL: {endpoint!r}")
        self.scheme = parsed.scheme
        self.host = parsed.netloc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._access = access_key
        self._secret = secret_key
        self.region = region or "us-east-1"
        self._timeout = timeout
        self._context = ssl.create_default_context(cafile=ca_file) if ca_file else None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> S3Target | None:
        endpoint = (env.get("OPS__BACKUP_COPY_S3_ENDPOINT") or "").strip()
        if not endpoint:
            return None
        missing = [
            name
            for name in (
                "OPS__BACKUP_COPY_S3_BUCKET",
                "OPS__BACKUP_COPY_S3_ACCESS_KEY",
                "OPS__BACKUP_COPY_S3_SECRET_KEY",
            )
            if not (env.get(name) or "").strip()
        ]
        if missing:
            raise S3Error(f"S3 copies need {', '.join(missing)} as well.")
        return cls(
            endpoint=endpoint,
            bucket=env["OPS__BACKUP_COPY_S3_BUCKET"].strip(),
            prefix=(env.get("OPS__BACKUP_COPY_S3_PREFIX") or "ai-platform-backups").strip(),
            access_key=env["OPS__BACKUP_COPY_S3_ACCESS_KEY"].strip(),
            secret_key=env["OPS__BACKUP_COPY_S3_SECRET_KEY"].strip(),
            region=(env.get("OPS__BACKUP_COPY_S3_REGION") or "us-east-1").strip(),
            ca_file=(env.get("OPS__BACKUP_COPY_S3_CA_FILE") or "").strip() or None,
        )

    @property
    def location(self) -> str:
        """Where copies go, without credentials: shown in the console."""
        return f"s3://{self.bucket}/{self.prefix} at {self.scheme}://{self.host}"

    # -- requests ------------------------------------------------------------------
    def _key(self, *parts: str) -> str:
        return "/".join(p.strip("/") for p in (self.prefix, *parts) if p)

    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200,),
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        query = query or {}
        path = "/" + _quote(self.bucket) + ("/" + _quote(key, safe="-_.~/") if key else "")
        payload = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        signed = sign(
            method=method,
            host=self.host,
            path=path,
            query=query,
            headers=headers or {},
            payload_sha256=payload,
            access_key=self._access,
            secret_key=self._secret,
            region=self.region,
            now=dt.datetime.now(dt.UTC),
        )
        url = f"{self.scheme}://{self.host}{path}"
        if query:
            url += "?" + canonical_query(query)
        request = urllib.request.Request(url, data=body or None, method=method, headers=signed)
        try:
            with urllib.request.urlopen(  # noqa: S310 — the operator's own endpoint
                request, timeout=timeout or self._timeout, context=self._context
            ) as response:
                status, data = response.status, response.read()
                received = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status, data, received = exc.code, exc.read() or b"", {}
        except (urllib.error.URLError, OSError) as exc:
            raise S3Error(f"Could not reach {self.scheme}://{self.host}: {exc}") from exc
        if status not in ok:
            raise S3Error(f"{method} {key or self.bucket} answered {status}: {_s3_message(data)}")
        return status, received, data

    # -- what the runner needs -----------------------------------------------------
    def reachable(self) -> bool:
        try:
            self._request("HEAD", timeout=PROBE_TIMEOUT)
        except S3Error:
            return False
        return True

    def copied(self, name: str) -> bool:
        try:
            status, _, _ = self._request(
                "HEAD", self._key(name, MARKER), ok=(200, 404), timeout=PROBE_TIMEOUT
            )
        except S3Error:
            return False
        return status == 200

    def complete_names(self) -> set[str]:
        """Names of the backups with a complete copy: one listing, not one HEAD each."""
        names = set()
        base = self._key() + "/" if self.prefix else ""
        for key in self._list(base, timeout=PROBE_TIMEOUT):
            rest = key[len(base) :]
            if rest.count("/") == 1 and rest.endswith("/" + MARKER):
                names.add(rest.split("/", 1)[0])
        return names

    def keys(self, name: str) -> list[str]:
        """Every object under ``PREFIX/NAME/``."""
        return self._list(self._key(name) + "/")

    def _list(self, prefix: str, timeout: float | None = None) -> list[str]:
        found: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            _, _, data = self._request("GET", query=query, timeout=timeout)
            root = ET.fromstring(data)  # noqa: S314 — the operator's own object store
            found += [el.text or "" for el in root.iter(f"{_NS}Key")]
            truncated = (root.findtext(f"{_NS}IsTruncated") or "false") == "true"
            token = root.findtext(f"{_NS}NextContinuationToken")
            if not truncated or not token:
                return found

    def upload(self, source: Path, name: str, digests: dict[str, str], log: Any) -> None:
        """Upload ``source`` as backup ``name``; the marker goes last."""
        for relative in sorted(digests):
            path = source / relative
            key = self._key(name, relative)
            size = path.stat().st_size
            if size > PART_SIZE:
                self._multipart(path, key, size, log)
            else:
                self._request("PUT", key, body=path.read_bytes())
            log(f"  uploaded {relative} ({size:,} bytes)")
        marker = json.dumps({"backup": name, "files": digests}, indent=2, sort_keys=True)
        self._request(
            "PUT",
            self._key(name, MARKER),
            body=marker.encode(),
            headers={"content-type": "application/json"},
        )

    def _multipart(self, path: Path, key: str, size: int, log: Any) -> None:
        _, _, data = self._request("POST", key, query={"uploads": ""})
        upload_id = ET.fromstring(data).findtext(f"{_NS}UploadId")  # noqa: S314
        if not upload_id:
            raise S3Error(f"No upload id for {key}.")
        parts: list[tuple[int, str]] = []
        try:
            with path.open("rb") as handle:
                number = 0
                while chunk := handle.read(PART_SIZE):
                    number += 1
                    _, headers, _ = self._request(
                        "PUT",
                        key,
                        query={"partNumber": str(number), "uploadId": upload_id},
                        body=chunk,
                    )
                    parts.append((number, headers.get("etag", "")))
                    log(f"  part {number} of {-(-size // PART_SIZE)} for {path.name}")
            body = "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{etag}</ETag></Part>"
                for n, etag in parts
            )
            self._request(
                "POST",
                key,
                query={"uploadId": upload_id},
                body=f"<CompleteMultipartUpload>{body}</CompleteMultipartUpload>".encode(),
                headers={"content-type": "application/xml"},
            )
        except Exception:
            try:
                self._request("DELETE", key, query={"uploadId": upload_id}, ok=(200, 204, 404))
            except S3Error:
                pass
            raise

    def delete(self, name: str) -> int:
        """Delete a backup's copy, marker first so it stops counting at once."""
        keys = self.keys(name)
        marker = self._key(name, MARKER)
        ordered = ([marker] if marker in keys else []) + [k for k in keys if k != marker]
        for key in ordered:
            self._request("DELETE", key, ok=(200, 204, 404))
        return len(ordered)


def _s3_message(data: bytes) -> str:
    try:
        root = ET.fromstring(data)  # noqa: S314
    except ET.ParseError:
        return data[:200].decode("utf-8", "replace") or "(no body)"
    code = root.findtext("Code") or root.findtext(f"{_NS}Code") or ""
    message = root.findtext("Message") or root.findtext(f"{_NS}Message") or ""
    return f"{code} {message}".strip() or "(no detail)"
