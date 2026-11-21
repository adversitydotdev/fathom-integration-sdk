"""The protocol loop through the test harness (RFC 0005 section 7)."""

import io
import json

import pytest

from ogrl_integration import (
    Incomplete,
    Integration,
    PermissionDenied,
    RateLimited,
    Subject,
    fail,
    pass_,
)
from ogrl_integration.testing import Harness, HarnessError

app = Integration("dev.example.test", "1.2.3")


@app.validate
def validate(ctx):
    return ctx.identity(f"{ctx.config['tenant']} as {ctx.secret('TOKEN')[:2]}…", scopes=["read"])


@app.resource("users", version=1)
def users(ctx):
    mode = ctx.config.get("mode")
    ctx.log("collecting users")
    yield {"id": "1", "name": "a"}
    if mode == "partial":
        raise Incomplete("stopped after page 1")
    if mode == "throttled":
        raise RateLimited("slow down", retry_after=5)
    if mode == "denied":
        raise PermissionDenied("cannot read users", permission="users:read")
    yield {"id": "2", "name": "b"}


@app.resource("groups", version=1)
def groups(ctx):
    raise RateLimited("slow down", retry_after=5)
    yield {}


@app.check("no-b", version=1)
def no_b(ctx, users):
    assert ctx.env == {}, "checks never see credentials"
    bad = [u for u in users if u["name"] == "b"]
    return fail("b exists", [Subject.account(u["name"], u["id"]) for u in bad]) if bad else pass_("ok")


@app.check("http", version=1)
def uses_http(ctx, users):
    ctx.http.get("https://example.test")
    return pass_("unreachable")


def test_initialize_and_unknown_method():
    out = io.StringIO()
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocol": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "nope", "params": {}},
    ]
    app.run(io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), out)
    first, second = (json.loads(line) for line in out.getvalue().splitlines())
    assert first["result"] == {"protocol": 1, "id": "dev.example.test", "version": "1.2.3"}
    assert second["error"]["data"]["kind"] == "invalid_config"


def test_validate_reads_secrets_from_the_environment():
    out = Harness(app).validate({"tenant": "acme"}, {"TOKEN": "sekret"})
    assert out == {"identity": "acme as se…", "granted_scopes": ["read"], "warnings": []}
    with pytest.raises(HarnessError) as caught:
        Harness(app).validate({"tenant": "acme"}, {})
    assert caught.value.kind == "invalid_config"


def test_collect_complete_partial_and_failures():
    harness = Harness(app)
    full = harness.collect("users", 1, {})
    assert ([u["id"] for u in full.items], full.complete) == (["1", "2"], True)
    assert "collecting users" in full.logs

    partial = harness.collect("users", 1, {"mode": "partial"})
    assert (len(partial.items), partial.complete, partial.reason) == (1, False, "stopped after page 1")

    # Throttled after some items: keep them, mark incomplete. Before any: let the host retry.
    throttled = harness.collect("users", 1, {"mode": "throttled"})
    assert (throttled.complete, throttled.reason) == (False, "slow down")
    with pytest.raises(HarnessError) as caught:
        harness.collect("groups", 1, {})
    assert caught.value.error["data"] == {"kind": "rate_limited", "retry_after": 5}

    with pytest.raises(HarnessError) as caught:
        harness.collect("users", 1, {"mode": "denied"})
    assert caught.value.error["data"] == {"kind": "permission_denied", "permission": "users:read"}


def test_checks_get_snapshots_and_nothing_else():
    harness = Harness(app)
    out = harness.check("no-b", 1, {"dev.example.test/users@1": [{"id": "2", "name": "b"}]})
    assert out == {
        "outcome": "fail",
        "summary": "b exists",
        "subjects": [{"kind": "account", "name": "b", "external_id": "2"}],
    }
    with pytest.raises(HarnessError) as caught:
        harness.check("http", 1, {"dev.example.test/users@1": []})
    assert "checks evaluate snapshots" in caught.value.error["message"]
