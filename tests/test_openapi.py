"""Query sources (Fathom RFC 0006): static validation, the derived schema, and execution."""

import json
import sys

import pytest

from fathom_integration import Integration, openapi
from fathom_integration.http import Request, Response
from fathom_integration.testing import Harness, HarnessError

DOCUMENT = {
    "openapi": "3.0.3",
    "info": {"title": "Vendor", "version": "1"},
    "paths": {
        "/api/v1/apps": {
            "get": {
                "operationId": "listApps",
                "summary": "List applications",
                "parameters": [
                    {"name": "filter", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/App"}}
                            }
                        }
                    }
                },
            },
            "post": {"operationId": "createApp", "responses": {"200": {"description": "ok"}}},
        },
        "/api/v1/groups/{groupId}/members": {
            "parameters": [{"$ref": "#/components/parameters/groupId"}],
            "get": {
                "operationId": "listMembers",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "data": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/Member"},
                                        },
                                        "next": {"type": "string"},
                                    },
                                }
                            }
                        }
                    }
                },
            },
        },
        "/api/v1/groups": {
            "get": {
                "operationId": "listGroups",
                "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Group"}}
                            }
                        }
                    }
                },
            }
        },
        "/api/v1/search": {
            "post": {
                "operationId": "searchApps",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/App"}}
                            }
                        }
                    }
                },
            }
        },
    },
    "components": {
        "parameters": {
            "groupId": {"name": "groupId", "in": "path", "required": True, "schema": {"type": "string"}}
        },
        "schemas": {
            "Base": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}, "created": {"type": "string"}},
            },
            "App": {
                "allOf": [
                    {"$ref": "#/components/schemas/Base"},
                    {
                        "type": "object",
                        "required": ["label"],
                        "properties": {
                            "label": {"type": "string", "description": "What users see."},
                            "status": {"type": "string"},
                            "settings": {
                                "type": "object",
                                "properties": {
                                    "signOn": {"type": "object", "properties": {"mode": {"type": "string"}}}
                                },
                            },
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                ]
            },
            "Group": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "profile": {"type": "object", "properties": {"name": {"type": "string"}}},
                },
            },
            "Member": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "login": {"type": "string"}},
                    },
                    {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "serviceName": {"type": "string"}},
                    },
                ]
            },
        },
    },
}
SETTINGS = openapi.Settings(
    base_url="https://{domain}",
    pagination="link",
    read_operations=("searchApps",),
    authorization="SSWS {API_TOKEN} | Bearer {FATHOM_ACCESS_TOKEN}",
)
APPS = openapi.Query(
    operation="listApps",
    params={"filter": 'status eq "ACTIVE"', "limit": "200"},
    fields={
        "id": "id",
        "name": "label",
        "status": "status",
        "sign_on": "settings.signOn.mode",
        "tags": "tags",
    },
)


@pytest.fixture
def description():
    return openapi.Description(DOCUMENT)


def check(description, query, **kw):
    problems, schema = openapi.validate(description, SETTINGS, query, **kw)
    return {p.pointer: p.detail for p in problems}, schema


def test_a_valid_query_derives_its_item_schema(description):
    problems, schema = check(description, APPS)
    assert problems == {}
    assert schema["required"] == ["id", "name", "sign_on", "status", "tags"]
    props = schema["properties"]
    assert props["id"]["type"] == ["string"]  # required along the whole path (allOf)
    assert props["name"] == {"type": ["string"], "description": "What users see."}
    assert props["sign_on"]["type"] == ["string", "null"]  # nested and optional
    assert props["tags"]["type"] == ["array", "null"]


def test_reads_only(description):
    assert (
        "only reads" in check(description, openapi.Query("createApp", fields={"id": "id"}))[0]["/operation"]
    )
    searched = openapi.Query("searchApps", fields={"id": "id", "name": "label"})
    assert check(description, searched)[0] == {}  # a POST the integration lists as a read
    assert "no operation" in check(description, openapi.Query("dropTables"))[0]["/operation"]


def test_parameters(description):
    bad = openapi.Query(
        "listApps", params={"limit": "lots", "expand": "x"}, fields={"id": "id", "name": "label"}
    )
    problems, _ = check(description, bad)
    assert problems == {
        "/params/limit": "limit is an integer",
        "/params/expand": "listApps has no parameter 'expand'",
    }

    members = openapi.Query("listMembers", items="data", fields={"id": "id", "name": "login"})
    assert check(description, members)[0] == {
        "/params/groupId": "/api/v1/groups/{groupId}/members needs groupId"
    }
    escaped = openapi.Query(
        "listMembers", params={"groupId": "../admin"}, items="data", fields={"id": "id", "name": "login"}
    )
    assert "cannot contain /" in check(description, escaped)[0]["/params/groupId"]


def test_items_and_fields(description):
    wrong = openapi.Query("listMembers", params={"groupId": "g1"}, fields={"id": "id", "name": "login"})
    assert "set items to the path of the list" in check(description, wrong)[0]["/items"]

    right = openapi.Query(
        "listMembers", params={"groupId": "g1"}, items="data", fields={"id": "id", "name": "login"}
    )
    problems, schema = check(description, right)
    assert problems == {}
    assert schema["properties"]["id"]["type"] == ["integer", "null"]  # oneOf: a union, nothing required

    missing = openapi.Query("listApps", fields={"id": "id", "name": "lable", "Bad": "x"})
    problems, _ = check(description, missing)
    assert problems["/fields/name"] == "items have no 'lable'; did you mean 'label'?"
    assert problems["/fields/Bad"] == "output field names match [a-z][a-z0-9_]*"
    assert check(description, openapi.Query("listApps", fields={"status": "status"}))[0] == {
        "/fields/id": "id is required: it names failing items",
        "/fields/name": "name is required: it names failing items",
    }


def test_fields_of_flattens_nested_paths(description):
    paths = {
        f["path"]: f["type"]
        for f in description.fields_of(description.operations["listApps"].response["items"])
    }
    assert paths["settings.signOn.mode"] == "string"
    assert paths["tags"] == "array of string"


# --- execution --------------------------------------------------------------------------------


class Vendor:
    def __init__(self) -> None:
        self.requests: list[Request] = []

    def send(self, request: Request, timeout: float) -> Response:
        self.requests.append(request)
        if "page=2" in request.url:
            body = [{"id": "a3", "label": "Wiki", "status": "ACTIVE", "settings": {}}]
            return Response(200, {}, json.dumps(body).encode())
        body = [
            {
                "id": "a1",
                "label": "Slack",
                "status": "ACTIVE",
                "settings": {"signOn": {"mode": "SAML_2_0"}},
                "tags": ["chat"],
            },
            {"id": "a2", "label": "Jira", "status": "ACTIVE", "settings": {"signOn": {"mode": "BOOKMARK"}}},
        ]
        link = '<https://acme.vendor.test/api/v1/apps?page=2>; rel="next"'
        return Response(200, {"Link": link}, json.dumps(body).encode())


@pytest.fixture
def bundled(tmp_path, monkeypatch):
    (tmp_path / "_fathom_openapi.py").write_text(openapi.encode(SETTINGS, DOCUMENT))
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("_fathom_openapi", None)
    yield
    sys.modules.pop("_fathom_openapi", None)


def query_json(query: openapi.Query) -> dict:
    return {
        "operation": query.operation,
        "params": query.params,
        "items": query.items,
        "fields": query.fields,
    }


def test_execute_paginates_projects_and_authenticates(bundled):
    app = Integration("dev.example.vendor", "1.0.0")
    app.transport = vendor = Vendor()
    out = Harness(app).collect(
        "apps", 1, {"domain": "acme.vendor.test"}, {"API_TOKEN": "t0k"}, query=query_json(APPS)
    )
    assert [i["name"] for i in out.items] == ["Slack", "Jira", "Wiki"]
    assert out.items[0] == {
        "id": "a1",
        "name": "Slack",
        "status": "ACTIVE",
        "sign_on": "SAML_2_0",
        "tags": ["chat"],
    }
    assert out.items[2]["sign_on"] is None
    first = vendor.requests[0]
    assert first.url == "https://acme.vendor.test/api/v1/apps?filter=status+eq+%22ACTIVE%22&limit=200"
    assert first.headers["Authorization"] == "SSWS t0k"

    app.transport = vendor = Vendor()
    Harness(app).collect(
        "apps", 1, {"domain": "acme.vendor.test"}, {"FATHOM_ACCESS_TOKEN": "ey"}, query=query_json(APPS)
    )
    assert vendor.requests[0].headers["Authorization"] == "Bearer ey"


def test_execute_rechecks_read_only_and_needs_a_description(bundled):
    app = Integration("dev.example.vendor", "1.0.0")
    app.transport = Vendor()
    write = {"operation": "createApp", "fields": {"id": "id", "name": "label"}}
    with pytest.raises(HarnessError) as caught:
        Harness(app).collect("apps", 1, {"domain": "acme.vendor.test"}, {"API_TOKEN": "t"}, query=write)
    assert caught.value.kind == "invalid_config" and "only reads" in caught.value.error["message"]
    with pytest.raises(HarnessError) as caught:
        Harness(app).collect(
            "apps", 1, {"domain": "evil.test/x?"}, {"API_TOKEN": "t"}, query=query_json(APPS)
        )
    assert caught.value.kind == "invalid_config"


def test_without_a_bundled_description_queries_are_refused():
    sys.modules.pop("_fathom_openapi", None)
    app = Integration("dev.example.plain", "1.0.0")
    with pytest.raises(HarnessError) as caught:
        Harness(app).collect("x", 1, {}, {}, query=query_json(APPS))
    assert "does not support query sources" in caught.value.error["message"]


def test_max_items_stops_before_the_next_page(bundled):
    app = Integration("dev.example.vendor", "1.0.0")
    app.transport = vendor = Vendor()
    query = {**query_json(APPS), "max_items": 2}
    out = Harness(app).collect("apps", 1, {"domain": "acme.vendor.test"}, {"API_TOKEN": "t"}, query=query)
    assert [i["name"] for i in out.items] == ["Slack", "Jira"]
    assert len(vendor.requests) == 1  # page 2 was never fetched


# --- for_each (RFC 0006 section 3) --------------------------------------------------------------

MEMBERS = openapi.Query(
    "listMembers",
    items="data",
    fields={"id": "id", "name": "login", "group": "$parent.profile.name"},
    for_each=openapi.ForEach("listGroups", bind={"groupId": "id"}, params={"q": "Admin"}),
)


def test_for_each_validates_both_calls(description):
    problems, schema = check(description, MEMBERS)
    assert problems == {}
    assert schema["required"] == ["error", "group", "id", "name"]
    assert schema["properties"]["error"] == {"type": ["string", "null"]}
    assert schema["properties"]["id"]["type"] == ["integer", "null"]
    assert schema["properties"]["group"]["type"] == ["string", "null"]

    def problems_with(**changes):
        fe = {**MEMBERS.for_each.__dict__, **changes.pop("for_each", {})}
        query = openapi.Query(**{**MEMBERS.__dict__, **changes, "for_each": openapi.ForEach(**fe)})
        return check(description, query)[0]

    assert problems_with(for_each={"bind": {"groupId": "idd"}}) == {
        "/for_each/bind/groupId": "items have no 'idd'; did you mean 'id'?"
    }
    assert problems_with(for_each={"bind": {"group": "id"}}) == {
        "/for_each/bind/group": "listMembers has no parameter 'group'",
        "/params/groupId": "/api/v1/groups/{groupId}/members needs groupId",
    }
    assert problems_with(params={"groupId": "g1"}) == {
        "/params/groupId": "for_each binds groupId; leave it out of params"
    }
    assert "only reads" in problems_with(for_each={"operation": "createApp"})["/for_each/operation"]
    assert problems_with(for_each={"limit": 9000}) == {"/for_each/limit": "between 1 and 5000"}
    plain = openapi.Query("listApps", fields={"id": "id", "name": "$parent.label"})
    assert check(description, plain)[0] == {
        "/fields/name": "$parent reads the listed item of a for_each query"
    }


class Groups:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def send(self, request: Request, timeout: float) -> Response:
        self.requests.append(request.url)
        path = request.url.split("vendor.test", 1)[1]
        if path.startswith("/api/v1/groups?"):
            body = [
                {"id": "g1", "profile": {"name": "Admins"}},
                {"id": "g2", "profile": {"name": "Gone"}},
                {"id": "../apps", "profile": {"name": "Sneaky"}},
            ]
            return Response(200, {}, json.dumps(body).encode())
        if path == "/api/v1/groups/g1/members":
            return Response(200, {}, json.dumps({"data": [{"id": 7, "login": "ada@acme.test"}]}).encode())
        error = {"errorCode": "E0000007", "errorSummary": "Not found: Resource not found: g2 (UserGroup)"}
        return Response(404, {}, json.dumps(error).encode())


def test_for_each_calls_once_per_listed_item(bundled):
    app = Integration("dev.example.vendor", "1.0.0")
    app.transport = vendor = Groups()
    spec = {**query_json(MEMBERS), "for_each": MEMBERS.for_each.__dict__}
    out = Harness(app).collect("members", 1, {"domain": "acme.vendor.test"}, {"API_TOKEN": "t"}, query=spec)
    assert out.complete
    assert out.items == [
        {"id": "7", "name": "ada@acme.test", "group": "Admins", "error": None},
        {"id": None, "name": None, "group": "Gone", "error": "E0000007"},
        {"id": None, "name": None, "group": "Sneaky", "error": "InvalidParentValue"},
    ]
    assert vendor.requests == [
        "https://acme.vendor.test/api/v1/groups?q=Admin",
        "https://acme.vendor.test/api/v1/groups/g1/members",
        "https://acme.vendor.test/api/v1/groups/g2/members",
    ]  # the group id with a slash never reached a URL


def test_for_each_limit_makes_the_collection_incomplete(bundled):
    app = Integration("dev.example.vendor", "1.0.0")
    app.transport = Groups()
    limited = {**MEMBERS.for_each.__dict__, "limit": 1}
    spec = {**query_json(MEMBERS), "for_each": limited}
    out = Harness(app).collect("members", 1, {"domain": "acme.vendor.test"}, {"API_TOKEN": "t"}, query=spec)
    assert [i["name"] for i in out.items] == ["ada@acme.test"]
    assert (out.complete, out.reason) == (False, "listGroups listed more than 1 items")
