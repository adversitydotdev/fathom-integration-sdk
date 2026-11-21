"""Query sources over a vendor's OpenAPI description (OGRL RFC 0006).

The integration bundles the vendor's description; the organization defines
queries (one read operation, parameters, the item path, and a field
projection). This module is shared by both sides: OGRL validates a query
statically before saving it, and the integration validates it again and runs
it. Standard library only, like the rest of the SDK.

    description = Description(json_document)
    problems, item_schema = validate(description, settings, query)
    for item in execute(ctx, description, settings, query): ...
"""

import base64
import difflib
import importlib
import itertools
import json
import re
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from ogrl_integration.errors import InvalidConfig

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
QUERY_NAME = re.compile(r"^[a-z0-9-]+$")
PATH_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
MAX_PARAM = 2048
HOST_VALUE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")  # no /, ?, #, @, or :
MAX_DEPTH = 6
GENERATED_MODULE = "_ogrl_openapi"


@dataclass(frozen=True)
class Problem:
    pointer: str
    detail: str
    warning: bool = False

    def to_json(self) -> dict[str, str]:
        return {"pointer": self.pointer, "detail": self.detail}


@dataclass(frozen=True)
class Settings:
    """The manifest's `openapi` block, minus the description itself."""

    base_url: str
    pagination: str = "link"
    read_operations: tuple[str, ...] = ()
    authorization: str = ""

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Settings":
        return cls(
            base_url=data["base_url"],
            pagination=data.get("pagination", "link"),
            read_operations=tuple(data.get("read_operations") or ()),
            authorization=data.get("authorization", ""),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "pagination": self.pagination,
            "read_operations": list(self.read_operations),
            "authorization": self.authorization,
        }


@dataclass(frozen=True)
class Operation:
    id: str
    method: str
    path: str
    summary: str
    parameters: dict[str, dict[str, Any]]  # name -> {in, required, schema, description}
    response: dict[str, Any] | None  # the 2xx application/json schema, $refs resolved lazily


@dataclass
class Query:
    operation: str
    params: dict[str, str] = field(default_factory=dict)
    items: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    max_items: int | None = None  # a dry run stops early; pages are fetched lazily

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Query":
        return cls(
            operation=str(data.get("operation") or ""),
            params={str(k): str(v) for k, v in (data.get("params") or {}).items()},
            items=str(data.get("items") or ""),
            fields={str(k): str(v) for k, v in (data.get("fields") or {}).items()},
            max_items=int(data["max_items"]) if data.get("max_items") else None,
        )


class Description:
    """An OpenAPI 3.x document with local `$ref`s resolved on demand."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        if not str(document.get("openapi", "")).startswith("3."):
            raise ValueError("an OpenAPI 3.x description is required")
        self.document = document
        self.operations: dict[str, Operation] = {}
        for path, item in (document.get("paths") or {}).items():
            shared = item.get("parameters") or []
            for method in METHODS:
                spec = item.get(method)
                if not isinstance(spec, dict) or not spec.get("operationId"):
                    continue
                params: dict[str, dict[str, Any]] = {}
                for raw in [*shared, *(spec.get("parameters") or [])]:
                    param = self.resolve(raw)
                    if param.get("in") in ("path", "query"):
                        params[param["name"]] = {
                            "in": param["in"],
                            "required": bool(param.get("required")) or param["in"] == "path",
                            "schema": self.resolve(param.get("schema") or {}),
                            "description": param.get("description", ""),
                        }
                self.operations[spec["operationId"]] = Operation(
                    id=spec["operationId"],
                    method=method.upper(),
                    path=path,
                    summary=spec.get("summary") or spec.get("description", "")[:200],
                    parameters=params,
                    response=self._response(spec),
                )

    def resolve(self, node: Any, depth: int = 0) -> Any:
        """Follow a local `$ref` (`#/components/...`)."""
        while isinstance(node, dict) and "$ref" in node and depth < 32:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                raise ValueError(f"only local references are supported: {ref}")
            target: Any = self.document
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            node, depth = target, depth + 1
        return node

    def _response(self, spec: Mapping[str, Any]) -> dict[str, Any] | None:
        for code in ("200", "2XX", "201", "default"):
            response = self.resolve((spec.get("responses") or {}).get(code) or {})
            content = response.get("content") or {}
            for media, body in content.items():
                if "json" in media and body.get("schema") is not None:
                    return body["schema"]
        return None

    def schema(self, node: Any) -> dict[str, Any]:
        """A schema with `$ref` resolved and `allOf` merged; `oneOf`/`anyOf` merged as a union."""
        node = self.resolve(node) or {}
        if not isinstance(node, dict):
            return {}
        merged = {k: v for k, v in node.items() if k not in ("allOf", "oneOf", "anyOf")}
        parts = [*(node.get("allOf") or []), *(node.get("oneOf") or []), *(node.get("anyOf") or [])]
        if parts:
            properties = dict(merged.get("properties") or {})
            required = set(merged.get("required") or []) if not node.get("oneOf") else set()
            for part in parts:
                resolved = self.schema(part)
                properties.update(resolved.get("properties") or {})
                if part in (node.get("allOf") or []):
                    required |= set(resolved.get("required") or [])
                merged.setdefault("type", resolved.get("type"))
                if resolved.get("additionalProperties"):
                    merged["additionalProperties"] = True
                if resolved.get("items") is not None:
                    merged.setdefault("items", resolved["items"])
            merged["properties"] = properties
            merged["required"] = sorted(required)
        if "properties" in merged and not merged.get("type"):
            merged["type"] = "object"
        return merged

    def at(self, schema: Any, dotted: str) -> tuple[dict[str, Any] | None, bool]:
        """The schema at a dotted path, and whether every step is required. None if absent."""
        current = self.schema(schema)
        required = True
        for part in [p for p in dotted.split(".") if p]:
            properties = current.get("properties") or {}
            if part not in properties:
                return None, False
            required = required and part in (current.get("required") or [])
            current = self.schema(properties[part])
        return current, required

    def fields_of(self, schema: Any, prefix: str = "", depth: int = 0) -> list[dict[str, Any]]:
        """Every dotted path in an item schema with its type: what a query can project."""
        out: list[dict[str, Any]] = []
        resolved = self.schema(schema)
        for name, child in sorted((resolved.get("properties") or {}).items()):
            node = self.schema(child)
            path = f"{prefix}{name}"
            kind = _type(node)
            out.append(
                {
                    "path": path,
                    "type": kind,
                    **({"description": node["description"][:200]} if node.get("description") else {}),
                }
            )
            if kind == "object" and depth < MAX_DEPTH:
                out += self.fields_of(node, f"{path}.", depth + 1)
        return out


def _type(schema: Mapping[str, Any]) -> str:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind == "array":
        return (
            f"array of {_type(schema.get('items') or {})}"
            if isinstance(schema.get("items"), dict)
            else "array"
        )
    return kind or ("object" if schema.get("properties") else "any")


def _json_type(schema: Mapping[str, Any]) -> list[str]:
    kind = _type(schema)
    base = "array" if kind.startswith("array") else kind
    return [base, "null"] if base in ("string", "integer", "number", "boolean", "object", "array") else []


def validate(
    description: Description, settings: Settings, query: Query, *, name: str | None = None
) -> tuple[list[Problem], dict[str, Any] | None]:
    """Static checks (RFC 0006 sections 3 and 5) and the derived item schema."""
    problems: list[Problem] = []
    if name is not None and not QUERY_NAME.match(name):
        problems.append(Problem("/name", "a name matches [a-z0-9-]+"))
    operation = description.operations.get(query.operation)
    if operation is None:
        problems.append(Problem("/operation", f"no operation {query.operation!r} in the description"))
        return problems, None
    if operation.method != "GET" and operation.id not in settings.read_operations:
        problems.append(
            Problem(
                "/operation",
                f"{operation.id} is {operation.method} {operation.path}: only reads can be queried",
            )
        )
        return problems, None

    for key, value in query.params.items():
        declared = operation.parameters.get(key)
        if declared is None:
            problems.append(Problem(f"/params/{key}", f"{operation.id} has no parameter {key!r}"))
            continue
        if len(value) > MAX_PARAM:
            problems.append(Problem(f"/params/{key}", f"longer than {MAX_PARAM} characters"))
        if declared["in"] == "path" and ("/" in value or value in ("", ".", "..")):
            problems.append(Problem(f"/params/{key}", "a path parameter cannot contain / or be empty"))
        kind = _type(declared["schema"])
        if kind == "integer" and not re.fullmatch(r"-?\d+", value):
            problems.append(Problem(f"/params/{key}", f"{key} is an integer"))
        if kind == "boolean" and value not in ("true", "false"):
            problems.append(Problem(f"/params/{key}", f"{key} is true or false"))
        enum = declared["schema"].get("enum")
        if enum and value not in [str(e) for e in enum]:
            problems.append(Problem(f"/params/{key}", f"{key} is one of {', '.join(map(str, enum))}"))
    for key, declared in operation.parameters.items():
        if declared["in"] == "path" and key not in query.params:
            problems.append(Problem(f"/params/{key}", f"{operation.path} needs {key}"))

    if operation.response is None:
        problems.append(Problem("/operation", f"{operation.id} documents no JSON response"))
        return problems, None
    if query.items and not all(PATH_SEGMENT.match(p) for p in query.items.split(".")):
        problems.append(Problem("/items", "a dotted path of property names, or empty"))
        return problems, None
    container, _ = description.at(operation.response, query.items)
    if container is None:
        problems.append(Problem("/items", f"the response has no {query.items!r}"))
        return problems, None
    if _type(container).split(" ")[0] != "array":
        where = f"{query.items!r}" if query.items else "the response body"
        problems.append(Problem("/items", f"{where} is not a list; set items to the path of the list"))
        return problems, None
    item = container.get("items") or {}

    if not query.fields:
        problems.append(Problem("/fields", "project at least id and name"))
    for required in ("id", "name"):
        if query.fields and required not in query.fields:
            problems.append(Problem(f"/fields/{required}", f"{required} is required: it names failing items"))
    properties: dict[str, Any] = {}
    required_fields: list[str] = []
    free_form = bool(description.schema(item).get("additionalProperties"))
    for key, path in query.fields.items():
        if not FIELD_NAME.match(key):
            problems.append(Problem(f"/fields/{key}", "output field names match [a-z][a-z0-9_]*"))
            continue
        if not path or not all(PATH_SEGMENT.match(p) for p in path.split(".")):
            problems.append(
                Problem(f"/fields/{key}", "a dotted path of property names, such as profile.login")
            )
            continue
        node, required = description.at(item, path)
        if node is None:
            known = [f["path"] for f in description.fields_of(item) if not f["path"].startswith("_")]
            close = difflib.get_close_matches(path, known, n=3, cutoff=0.6)
            hint = (
                f"did you mean {' or '.join(repr(c) for c in close)}?"
                if close
                else f"top-level fields: {', '.join(p for p in known if '.' not in p)[:300]}"
            )
            problems.append(Problem(f"/fields/{key}", f"items have no {path!r}; {hint}", warning=free_form))
            properties[key] = {}
            continue
        types = _json_type(node)
        properties[key] = (
            {"type": types if not required else [t for t in types if t != "null"]} if types else {}
        )
        if node.get("description"):
            properties[key]["description"] = node["description"][:200]
        if required:
            required_fields.append(key)
    schema = {"type": "object", "required": sorted(query.fields), "properties": properties}
    return problems, schema


# --- executing --------------------------------------------------------------------------------


def _get(data: Any, dotted: str) -> Any:
    for part in [p for p in dotted.split(".") if p]:
        if not isinstance(data, Mapping) or part not in data:
            return None
        data = data[part]
    return data


def _fill(template: str, values: Mapping[str, str]) -> str | None:
    missing = [name for name in re.findall(r"\{(\w+)\}", template) if not values.get(name)]
    if missing:
        return None
    return re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], template)


def authorization(settings: Settings, env: Mapping[str, str]) -> str | None:
    """The first `a | b` alternative whose variables are all set."""
    for alternative in [a.strip() for a in settings.authorization.split("|") if a.strip()]:
        value = _fill(alternative, env)
        if value is not None:
            return value
    return None


def execute(ctx: Any, description: Description, settings: Settings, query: Query) -> Iterator[dict[str, Any]]:
    """Run a validated query and yield projected items. Re-checks the read-only rules."""
    problems, _ = validate(description, settings, query)
    errors = [p for p in problems if not p.warning]
    if errors:
        raise InvalidConfig("; ".join(f"{p.pointer}: {p.detail}" for p in errors))
    operation = description.operations[query.operation]
    unsafe = [
        name
        for name in re.findall(r"\{(\w+)\}", settings.base_url)
        if not HOST_VALUE.match(str(ctx.config.get(name, "")))
    ]
    if unsafe:
        raise InvalidConfig(f"config {', '.join(unsafe)} must be a host name to build base_url")
    base = _fill(settings.base_url, ctx.config)
    if base is None:
        raise InvalidConfig(f"base_url {settings.base_url} needs config it does not have")
    base_parts = urlsplit(base)
    if base_parts.scheme != "https" and base_parts.hostname not in ("localhost", "127.0.0.1"):
        raise InvalidConfig(f"{base} is not an https URL")
    path = operation.path
    query_params: dict[str, str] = {}
    for key, value in query.params.items():
        if operation.parameters[key]["in"] == "path":
            path = path.replace("{" + key + "}", quote(value, safe=""))
        else:
            query_params[key] = value
    url = base.rstrip("/") + path
    headers = {}
    auth = authorization(settings, ctx.env)
    if settings.authorization and auth is None:
        raise InvalidConfig("no credential for this integration's authorization")
    if auth:
        headers["Authorization"] = auth

    def items_of(response: Any) -> list[Any]:
        body = response.json()
        found = _get(body, query.items) if query.items else body
        if not isinstance(found, list):
            raise InvalidConfig(f"the response's {query.items or 'body'} is not a list")
        return found

    kind, _, rest = settings.pagination.partition(":")
    pages: Iterator[Any]
    if kind == "link":
        pages = ctx.http.paginate(url, params=query_params, headers=headers, items=items_of)
    elif kind == "next-url":
        pages = ctx.http.paginate(
            url, params=query_params, headers=headers, items=items_of, next_url=lambda r: _get(r.json(), rest)
        )
    elif kind == "cursor":
        source, _, param = rest.partition(":")
        pages = ctx.http.paginate(
            url,
            params=query_params,
            headers=headers,
            items=items_of,
            cursor=lambda r: _get(r.json(), source),
            cursor_param=param,
        )
    else:
        pages = iter(items_of(ctx.http.get(url, params=query_params, headers=headers)))
    if query.max_items is not None:
        pages = itertools.islice(pages, query.max_items)
    for raw in pages:
        projected = {key: _get(raw, path) for key, path in query.fields.items()}
        for key in ("id", "name"):
            if projected.get(key) is not None and not isinstance(projected[key], str):
                projected[key] = str(projected[key])
        yield projected


# --- bundling ---------------------------------------------------------------------------------


def encode(settings: Settings, document: Mapping[str, Any]) -> str:
    """The generated module `pack` writes: settings and the description, compressed."""
    payload = json.dumps({"settings": settings.to_json(), "description": document}, separators=(",", ":"))
    blob = base64.b64encode(zlib.compress(payload.encode(), 9)).decode()
    lines = [blob[i : i + 100] for i in range(0, len(blob), 100)]
    return (
        '"""Generated by `ogrl-integration openapi build`: the vendor description and query settings."""\n\n'
        "DATA = (\n" + "\n".join(f'    "{line}"' for line in lines) + "\n)\n"
    )


def load_bundled() -> tuple[Settings, Description] | None:
    """The generated module, if the package has one (in memory or on disk)."""
    try:
        module = importlib.import_module(GENERATED_MODULE)
    except ModuleNotFoundError:
        return None
    data = json.loads(zlib.decompress(base64.b64decode(module.DATA)))
    return Settings.from_json(data["settings"]), Description(data["description"])
