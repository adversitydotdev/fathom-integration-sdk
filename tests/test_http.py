"""The HTTP client: retries, error mapping, and pagination, over a fake transport."""

import json

import pytest

from fathom_integration import AuthFailed, Http, PermissionDenied, RateLimited, VendorError
from fathom_integration.http import Request, Response


class Script:
    """A transport that answers from a list and records what it was asked."""

    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.sent: list[Request] = []

    def send(self, request: Request, timeout: float) -> Response:
        self.sent.append(request)
        return self.responses.pop(0)


def ok(body, headers=None) -> Response:
    return Response(200, {"Content-Type": "application/json", **(headers or {})}, json.dumps(body).encode())


def test_retries_honour_retry_after():
    waits: list[float] = []
    transport = Script(Response(429, {"Retry-After": "7"}, b""), Response(503, {}, b""), ok({"x": 1}))
    http = Http(transport, sleep=waits.append)
    assert http.get("https://api.test/x").json() == {"x": 1}
    assert waits == [7, 2]


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthFailed), (403, PermissionDenied), (404, VendorError), (500, VendorError)],
)
def test_status_mapping(status, error):
    http = Http(Script(*[Response(status, {}, b"")] * 4), sleep=lambda _: None)
    with pytest.raises(error) as caught:
        http.get("https://api.test/x?token=secret")
    assert caught.value.status == status


def test_exhausted_rate_limit_carries_retry_after_and_hides_the_query():
    http = Http(Script(*[Response(429, {"retry-after": "30"}, b"")] * 4), sleep=lambda _: None)
    with pytest.raises(RateLimited) as caught:
        http.get("https://api.test/x", params={"token": "secret"})
    assert caught.value.retry_after == 30
    assert "secret" not in caught.value.message


def test_paginate_link_header_and_cursor():
    transport = Script(
        ok(
            [1, 2],
            {"Link": '<https://api.test/x?page=2>; rel="next", <https://api.test/x?page=9>; rel="last"'},
        ),
        ok([3]),
    )
    assert list(Http(transport).paginate("https://api.test/x", headers={"A": "b"})) == [1, 2, 3]
    assert transport.sent[1].url == "https://api.test/x?page=2"
    assert transport.sent[1].headers["A"] == "b"

    transport = Script(ok({"data": [1], "next": "c2"}), ok({"data": [2], "next": None}))
    pages = Http(transport).paginate(
        "https://api.test/y",
        params={"limit": 1},
        items=lambda r: r.json()["data"],
        cursor=lambda r: r.json()["next"],
        cursor_param="after",
    )
    assert list(pages) == [1, 2]
    assert transport.sent[1].url == "https://api.test/y?limit=1&after=c2"


def test_reset_headers_set_the_wait(monkeypatch):
    monkeypatch.setattr("fathom_integration.http.time.time", lambda: 1000.0)
    waits: list[float] = []
    okta = Script(Response(429, {"X-Rate-Limit-Reset": "1009"}, b""), ok([]))
    Http(okta, sleep=waits.append).get("https://acme.okta.com/api/v1/users")
    assert waits == [10]


def test_github_primary_rate_limit_is_a_rate_limit_not_a_denial(monkeypatch):
    monkeypatch.setattr("fathom_integration.http.time.time", lambda: 1000.0)
    limited = Response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}, b"")
    waits: list[float] = []
    assert Http(Script(limited, ok({"x": 1})), sleep=waits.append).get("https://api.github.com/x").json() == {
        "x": 1
    }
    assert waits == [31]
    with pytest.raises(RateLimited) as caught:
        Http(Script(*[limited] * 4), sleep=lambda _: None).get("https://api.github.com/x")
    assert caught.value.retry_after == 31
    denied = Response(403, {"x-ratelimit-remaining": "4999"}, b"")
    with pytest.raises(PermissionDenied):
        Http(Script(denied), sleep=lambda _: None).get("https://api.github.com/x")
