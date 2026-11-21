"""Test kit: drive an integration through the real protocol, offline.

- `Harness(app)` runs operations in process over the same JSON-RPC framing
  the OGRL host uses, and returns what the host would receive.
- `Cassette` records vendor HTTP traffic once (with credentials scrubbed) and
  replays it, so tests never call a real vendor.

    app.transport = Cassette("tests/cassettes/users.json", mode="replay")
    result = Harness(app).collect("users", 1, config={"domain": "acme.okta.com"})
    assert result.complete and result.items[0]["id"] == "00u1"
"""

import io
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from unittest import mock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ogrl_integration.app import Integration
from ogrl_integration.http import Request, Response, Transport, UrllibTransport

SCRUB_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"})
SCRUB_PARAMS = frozenset({"access_token", "token", "api_key", "apikey", "key", "client_secret", "code"})


class HarnessError(Exception):
    """The integration answered with a JSON-RPC error."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message"))
        self.error = error
        self.kind = (error.get("data") or {}).get("kind")


@dataclass
class Collected:
    items: list[dict[str, Any]]
    complete: bool
    reason: str | None
    logs: list[str] = field(default_factory=list)


class Harness:
    def __init__(self, app: Integration) -> None:
        self.app = app
        self.logs: list[str] = []

    def call(self, method: str, params: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Any:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocol": 1}},
            {"jsonrpc": "2.0", "id": 2, "method": method, "params": dict(params)},
        ]
        stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, dict(env or {}), clear=True):
            self.app.run(stdin, stdout)
        messages = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.logs += [m["params"]["message"] for m in messages if m.get("method") == "log"]
        (answer,) = [m for m in messages if m.get("id") == 2]
        if "error" in answer:
            raise HarnessError(answer["error"])
        return answer["result"]

    def validate(self, config: Mapping[str, str], secrets: Mapping[str, str] | None = None) -> dict:
        return self.call("connection.validate", {"config": dict(config)}, secrets)

    def collect(
        self,
        resource: str,
        version: int,
        config: Mapping[str, str],
        secrets: Mapping[str, str] | None = None,
    ) -> Collected:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch) / "items.jsonl"
            result = self.call(
                "collect",
                {"resource": resource, "version": version, "config": dict(config), "output": str(output)},
                secrets,
            )
            items = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
        assert result["count"] == len(items), "count must equal the items written"
        return Collected(items, result["complete"], result.get("reason"), list(self.logs))

    def check(
        self,
        name: str,
        version: int,
        snapshots: Mapping[str, list[dict[str, Any]]],
        args: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """`snapshots` maps a source (`dev.example/users@1`) to its items."""
        with tempfile.TemporaryDirectory() as scratch:
            paths = {}
            for index, (source, items) in enumerate(snapshots.items()):
                path = Path(scratch) / f"snapshot-{index}.json"
                path.write_text(json.dumps({"source": source, "complete": True, "items": items}))
                paths[source] = str(path)
            return self.call(
                "check",
                {
                    "check": name,
                    "version": version,
                    "args": dict(args or {}),
                    "params": dict(params or {}),
                    "snapshots": paths,
                },
            )


# --- Cassettes ---------------------------------------------------------------------------------


def _scrub_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, "SCRUBBED" if k.lower() in SCRUB_PARAMS else v) for k, v in parse_qsl(parts.query)]
    return urlunsplit(parts._replace(query=urlencode(query)))


class CassetteMiss(AssertionError):
    pass


class Cassette:
    """Record or replay HTTP exchanges as JSON. Replay matches method and scrubbed URL, in order."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        mode: Literal["record", "replay"] = "replay",
        transport: Transport | None = None,
    ) -> None:
        self.path = Path(path)
        self.mode = mode
        self._real = transport or UrllibTransport()
        self._recorded: list[dict[str, Any]] = []
        self._queues: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        if mode == "replay":
            for entry in json.loads(self.path.read_text())["interactions"]:
                self._queues[(entry["method"], entry["url"])].append(entry)

    def send(self, request: Request, timeout: float) -> Response:
        key = (request.method, _scrub_url(request.url))
        if self.mode == "replay":
            queue = self._queues.get(key)
            if not queue:
                raise CassetteMiss(f"no recorded response for {key[0]} {key[1]}")
            entry = queue.pop(0)
            return Response(entry["status"], entry["headers"], entry["body"].encode(), request.url)
        response = self._real.send(request, timeout)
        self._recorded.append(
            {
                "method": key[0],
                "url": key[1],
                "status": response.status,
                "headers": {k: v for k, v in response.headers.items() if k.lower() not in SCRUB_HEADERS},
                "body": response.body.decode(errors="replace"),
            }
        )
        self.save()
        return response

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"interactions": self._recorded}, indent=2) + "\n")
