"""AWS Signature Version 4, on the standard library (the SDK ships no dependencies).

Used by Smithy query sources (OGRL RFC 0007) and by hand-written AWS resources.

https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html
"""

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlsplit


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _encode(value: str) -> str:
    return quote(value, safe="-_.~")


def sign(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    access_key: str,
    secret_key: str,
    session_token: str | None,
    region: str,
    service: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """`headers` plus Host, X-Amz-Date, the session token, and Authorization."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    parts = urlsplit(url)
    signed = {**headers, "Host": parts.netloc, "X-Amz-Date": amz_date}
    if session_token:
        signed["X-Amz-Security-Token"] = session_token

    canonical_headers = {k.lower(): " ".join(v.split()) for k, v in signed.items()}
    names = sorted(canonical_headers)
    query = sorted((_encode(k), _encode(v)) for k, v in parse_qsl(parts.query, keep_blank_values=True))
    canonical = "\n".join(
        [
            method.upper(),
            quote(parts.path or "/", safe="/-_.~"),
            "&".join(f"{k}={v}" for k, v in query),
            "".join(f"{name}:{canonical_headers[name]}\n" for name in names),
            ";".join(names),
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    signed["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return signed
