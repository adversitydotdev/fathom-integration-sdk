"""A small HTTP client on the standard library, so a package ships no dependencies.

- Retries 429 and 5xx with exponential backoff, honouring `Retry-After`.
- Maps 401 to AuthFailed, 403 to PermissionDenied, an exhausted 429 to
  RateLimited, and other failures to VendorError.
- `paginate` follows RFC 8288 `Link: rel="next"` headers or a cursor you
  extract from each page.
- The transport is swappable, which is how cassettes record and replay.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ogrl_integration import __version__
from ogrl_integration.errors import (
    AuthFailed,
    IntegrationError,
    PermissionDenied,
    RateLimited,
    VendorError,
)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str = ""

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == lowered), None)

    def json(self) -> Any:
        return json.loads(self.body or b"null")

    @property
    def text(self) -> str:
        return self.body.decode(errors="replace")

    @property
    def links(self) -> dict[str, str]:
        """RFC 8288 Link header as {rel: url}."""
        out = {}
        for part in (self.header("link") or "").split(","):
            url, _, params = part.strip().partition(";")
            url = url.strip()
            if not (url.startswith("<") and url.endswith(">")):
                continue
            for param in params.split(";"):
                key, _, value = param.strip().partition("=")
                if key.strip() == "rel":
                    for rel in value.strip().strip('"').split():
                        out[rel] = url[1:-1]
        return out


class Transport(Protocol):
    def send(self, request: Request, timeout: float) -> Response: ...


class UrllibTransport:
    def send(self, request: Request, timeout: float) -> Response:
        req = urllib.request.Request(
            request.url, data=request.body, headers=request.headers, method=request.method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https URLs from manifests
                return Response(resp.status, dict(resp.headers.items()), resp.read(), resp.url)
        except urllib.error.HTTPError as exc:
            return Response(exc.code, dict(exc.headers.items()), exc.read(), request.url)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise VendorError(f"{request.method} {request.url}: {exc}") from exc


def _retry_after(response: Response, clock: Callable[[], float] = time.time) -> int | None:
    """`Retry-After`, else the epoch-seconds reset Okta (`X-Rate-Limit-Reset`) and GitHub
    (`X-RateLimit-Reset`) send instead."""
    value = response.header("retry-after")
    if value is not None:
        try:
            return max(0, int(float(value)))
        except ValueError:
            return None
    reset = response.header("x-rate-limit-reset") or response.header("x-ratelimit-reset")
    if reset is None:
        return None
    try:
        return max(0, int(float(reset) - clock()) + 1)
    except ValueError:
        return None


def _rate_limited(response: Response) -> bool:
    """429, or GitHub's primary limit: 403 with no requests remaining."""
    return response.status == 429 or (
        response.status == 403 and response.header("x-ratelimit-remaining") == "0"
    )


class Http:
    def __init__(
        self,
        transport: Transport | None = None,
        *,
        retries: int = 3,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.retries = retries
        self.timeout = timeout
        self._sleep = sleep
        self.headers = {"User-Agent": f"ogrl-integration/{__version__}", **(headers or {})}

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        data: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> Response:
        """`content` sends bytes as they are, for callers that sign the body (AWS SigV4)."""
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}{'&' if urllib.parse.urlsplit(url).query else '?'}{query}"
        sent = {"Accept": "application/json", **self.headers, **(headers or {})}
        body = content
        if json_body is not None:
            body = json.dumps(json_body).encode()
            sent["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(method.upper(), url, sent, body)

        attempt = 0
        while True:
            response = self.transport.send(request, self.timeout)
            retryable = response.status in RETRY_STATUSES or _rate_limited(response)
            if not retryable or attempt >= self.retries:
                break
            attempt += 1
            wait = _retry_after(response)
            self._sleep(min(wait if wait is not None else 2 ** (attempt - 1), 60))
        return self._raise_for(request, response)

    def _raise_for(self, request: Request, response: Response) -> Response:
        if response.status < 400:
            return response
        where = f"{request.method} {_redact(request.url)} answered {response.status}"
        error: IntegrationError
        if response.status == 401:
            error = AuthFailed(where)
        elif _rate_limited(response):
            error = RateLimited(where, _retry_after(response))
        elif response.status == 403:
            error = PermissionDenied(where)
        else:
            error = VendorError(where)
        error.status = response.status
        raise error

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def paginate(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        items: Callable[[Response], list[Any]] = lambda r: r.json(),
        next_url: Callable[[Response], str | None] | None = None,
        cursor: Callable[[Response], str | None] | None = None,
        cursor_param: str = "cursor",
        max_pages: int = 10_000,
    ) -> Iterator[Any]:
        """Yield every item across pages.

        By default the next page is the Link header's `rel="next"`. Pass
        `next_url` to read it from the body, or `cursor` to read a cursor that
        is sent back as `cursor_param`.
        """
        current_params = dict(params or {})
        for _ in range(max_pages):
            response = self.get(url, params=current_params, headers=headers)
            yield from items(response)
            if cursor is not None:
                token = cursor(response)
                if not token:
                    return
                current_params[cursor_param] = token
                continue
            following = next_url(response) if next_url else response.links.get("next")
            if not following:
                return
            url, current_params = following, {}
        raise VendorError(f"{url}: more than {max_pages} pages")


def _redact(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(query="")) + ("?…" if parts.query else "")
