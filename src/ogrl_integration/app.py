"""The integration: decorators that register operations, and the protocol-1 loop.

    app = Integration("dev.example.directory", "0.1.0")

    @app.validate
    def validate(ctx): ...                       # returns ctx.identity(...)

    @app.resource("users", version=1)
    def users(ctx): yield {...}                  # one dict per vendor record

    @app.check("admins-have-mfa", version=1)
    def admins(ctx, users): return fail(...)     # keyword per resource the check reads

    if __name__ == "__main__":
        app.run()

`id` and `version` must equal the manifest; `ogrl-integration test` checks.

Nothing here touches the filesystem: collected items stream to the host as
`items` notifications and snapshots arrive inside the `check` request, so a
package runs on a read-only filesystem, even loaded from memory. Do not read
files next to your module; put data in code.
"""

import json
import os
import sys
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import IO, Any

from ogrl_integration.errors import Incomplete, IntegrationError, InvalidConfig, RateLimited
from ogrl_integration.http import Http, Transport
from ogrl_integration.outcomes import Identity, Outcome

PROTOCOL = 1
METHOD_NOT_FOUND = -32601
SERVER_ERROR = -32000
# Items stream back as `items` notifications, each line kept under the host's 1 MB limit.
BATCH_BYTES = 512 * 1024
BATCH_ITEMS = 500
LINE_LIMIT = 1024 * 1024


class _Channel:
    """JSON-RPC over two line streams. Stdout carries protocol messages only."""

    def __init__(self, out: IO[str]) -> None:
        self._out = out

    def send(self, message: dict[str, Any]) -> None:
        self._out.write(json.dumps({"jsonrpc": "2.0", **message}, separators=(",", ":")) + "\n")
        self._out.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def raw(self, line: str) -> None:
        self._out.write(line + "\n")
        self._out.flush()


@dataclass
class Context:
    """What an operation sees. Checks get no secrets and no HTTP client."""

    config: dict[str, str]
    env: Mapping[str, str]
    channel: _Channel | None = None
    http_client: Http | None = None
    args: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)

    @property
    def http(self) -> Http:
        if self.http_client is None:
            raise RuntimeError("checks evaluate snapshots; collect what you need as a resource")
        return self.http_client

    def secret(self, name: str) -> str:
        value = self.env.get(name)
        if not value:
            raise InvalidConfig(f"{name} is not set for this connection")
        return value

    @property
    def access_token(self) -> str:
        """For `oauth2` connections: a fresh token the host obtained."""
        return self.secret("OGRL_ACCESS_TOKEN")

    @property
    def aws_credentials(self) -> dict[str, str]:
        """For `aws_role` connections: short-lived keys the host obtained."""
        names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
        return {name: self.secret(name) for name in names}

    def identity(self, identity: str, scopes: Iterable[str] = (), warnings: Iterable[str] = ()) -> Identity:
        return Identity(identity, list(scopes), list(warnings))

    def log(self, message: str) -> None:
        if self.channel:
            self.channel.notify("log", {"message": message})

    def progress(self, done: int, total: int | None = None) -> None:
        if self.channel:
            self.channel.notify("progress", {"done": done, **({"total": total} if total else {})})


Resource = Callable[[Context], Iterable[Mapping[str, Any]]]
Check = Callable[..., Outcome]


class Integration:
    def __init__(self, id: str, version: str, *, transport: Transport | None = None) -> None:
        self.id = id
        self.version = version
        self.transport = transport  # tests and cassettes swap this
        self._validate: Callable[[Context], Identity] | None = None
        self.resources: dict[tuple[str, int], Resource] = {}
        self.checks: dict[tuple[str, int], Check] = {}

    # --- registration ---------------------------------------------------------------------

    def validate(self, fn: Callable[[Context], Identity]) -> Callable[[Context], Identity]:
        self._validate = fn
        return fn

    def resource(self, name: str, version: int = 1) -> Callable[[Resource], Resource]:
        def register(fn: Resource) -> Resource:
            self.resources[(name, version)] = fn
            return fn

        return register

    def check(self, name: str, version: int = 1) -> Callable[[Check], Check]:
        def register(fn: Check) -> Check:
            self.checks[(name, version)] = fn
            return fn

        return register

    # --- the loop -------------------------------------------------------------------------

    def run(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        """Serve requests until stdin closes. Logs and tracebacks go to stderr."""
        channel = _Channel(stdout or sys.stdout)
        for line in stdin or sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                channel.send({"id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            self._handle(channel, request)

    def _handle(self, channel: _Channel, request: dict[str, Any]) -> None:
        rid, method, params = request.get("id"), request.get("method"), request.get("params") or {}
        handlers = {
            "initialize": self._initialize,
            "connection.validate": self._connection_validate,
            "collect": self._collect,
            "check": self._check,
        }
        handler = handlers.get(method or "")
        if handler is None:
            channel.send(
                {
                    "id": rid,
                    "error": {
                        "code": METHOD_NOT_FOUND,
                        "message": f"unknown method {method}",
                        "data": {"kind": "invalid_config"},
                    },
                }
            )
            return
        try:
            result = handler(channel, params)
        except IntegrationError as exc:
            channel.send(
                {"id": rid, "error": {"code": SERVER_ERROR, "message": exc.message, "data": exc.data()}}
            )
            return
        except Exception as exc:  # a bug in the integration: report it, keep stdout clean
            traceback.print_exc(file=sys.stderr)
            channel.send(
                {
                    "id": rid,
                    "error": {
                        "code": SERVER_ERROR,
                        "message": f"{type(exc).__name__}: {exc}",
                        "data": {"kind": "vendor_error"},
                    },
                }
            )
            return
        channel.send({"id": rid, "result": result})

    def _context(self, channel: _Channel, params: dict[str, Any], *, credentials: bool) -> Context:
        env = dict(os.environ) if credentials else {}
        return Context(
            config=dict(params.get("config") or {}),
            env=env,
            channel=channel,
            http_client=Http(self.transport) if credentials else None,
            args=dict(params.get("args") or {}),
            params=dict(params.get("params") or {}),
        )

    def _initialize(self, channel: _Channel, params: dict[str, Any]) -> dict[str, Any]:
        return {"protocol": PROTOCOL, "id": self.id, "version": self.version}

    def _connection_validate(self, channel: _Channel, params: dict[str, Any]) -> dict[str, Any]:
        if self._validate is None:
            raise InvalidConfig("this integration does not implement connection.validate")
        identity = self._validate(self._context(channel, params, credentials=True))
        if not isinstance(identity, Identity):
            raise TypeError("validate must return ctx.identity(...)")
        return identity.to_json()

    def _collect(self, channel: _Channel, params: dict[str, Any]) -> dict[str, Any]:
        key = (params.get("resource"), int(params.get("version") or 0))
        fn = self.resources.get(key)  # type: ignore[arg-type]
        if fn is None:
            raise InvalidConfig(f"no resource {key[0]}@{key[1]}")
        ctx = self._context(channel, params, credentials=True)
        count, complete, reason = 0, True, None
        batch: list[str] = []
        size = 0

        def flush() -> None:
            nonlocal batch, size
            if batch:
                channel.raw('{"jsonrpc":"2.0","method":"items","params":{"items":[' + ",".join(batch) + "]}}")
                batch, size = [], 0

        try:
            for item in _items(fn(ctx)):
                encoded = json.dumps(item, separators=(",", ":"), default=str)
                if len(encoded) > LINE_LIMIT - 1024:
                    raise ValueError(f"item {count} is larger than 1 MB; split it or drop fields")
                if size + len(encoded) > BATCH_BYTES or len(batch) >= BATCH_ITEMS:
                    flush()
                batch.append(encoded)
                size += len(encoded) + 1
                count += 1
        except Incomplete as exc:
            complete, reason = False, exc.reason
        except RateLimited as exc:
            if count == 0:
                raise  # nothing collected: let the host retry the whole operation
            complete, reason = False, exc.message
        flush()
        return {"count": count, "complete": complete, **({"reason": reason} if reason else {})}

    def _check(self, channel: _Channel, params: dict[str, Any]) -> dict[str, Any]:
        key = (params.get("check"), int(params.get("version") or 0))
        fn = self.checks.get(key)  # type: ignore[arg-type]
        if fn is None:
            raise InvalidConfig(f"no check {key[0]}@{key[1]}")
        snapshots: dict[str, list[Any]] = {}
        for source, document in (params.get("snapshots") or {}).items():
            name = source.rsplit("/", 1)[-1].split("@", 1)[0]
            snapshots[name.replace("-", "_")] = (document or {}).get("items", [])
        outcome = fn(self._context(channel, params, credentials=False), **snapshots)
        if not isinstance(outcome, Outcome):
            raise TypeError("a check returns pass_(...), fail(...), or error(...)")
        return outcome.to_json()


def _items(produced: Iterable[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
    for item in produced:
        if not isinstance(item, Mapping):
            raise TypeError(f"a resource yields mappings, not {type(item).__name__}")
        yield dict(item)
