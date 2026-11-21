"""Cassettes record once with credentials scrubbed, then replay offline."""

import json

import pytest

from ogrl_integration import Http
from ogrl_integration.http import Response
from ogrl_integration.testing import Cassette, CassetteMiss


class Vendor:
    def send(self, request, timeout):
        assert request.headers["Authorization"] == "Bearer live"
        return Response(200, {"Set-Cookie": "s=1", "Content-Type": "application/json"}, b'{"ok": true}')


def test_record_scrubs_then_replays(tmp_path):
    path = tmp_path / "tape.json"
    Http(Cassette(path, "record", transport=Vendor())).get(
        "https://api.test/u",
        params={"access_token": "live", "page": "1"},
        headers={"Authorization": "Bearer live"},
    )
    tape = path.read_text()
    assert "live" not in tape and "Set-Cookie" not in tape
    assert json.loads(tape)["interactions"][0]["url"] == "https://api.test/u?access_token=SCRUBBED&page=1"

    replay = Http(Cassette(path))
    assert replay.get("https://api.test/u", params={"access_token": "other", "page": "1"}).json() == {
        "ok": True
    }
    with pytest.raises(CassetteMiss):
        replay.get("https://api.test/u", params={"page": "2"})
