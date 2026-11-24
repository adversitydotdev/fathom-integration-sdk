"""Query sources over a vendor's OpenAPI description (Fathom RFC 0006).

The integration bundles the vendor's description; the organization defines
queries (one read operation, parameters, the item path, and a field
projection). This module is shared by both sides: Fathom validates a query
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

from fathom_integration.errors import (
    AuthFailed,
    Incomplete,
    IntegrationError,
    InvalidConfig,
    PermissionDenied,
    RateLimited,
)

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
QUERY_NAME = re.compile(r"^[a-z0-9-]+$")
PATH_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
MAX_PARAM = 2048
HOST_VALUE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")  # no /, ?, #, @, or :
MAX_DEPTH = 6
GENERATED_MODULE = "_fathom_openapi"
SELF, PARENT_PREFIX = ".", "$parent."
DEFAULT_LIMIT, MAX_LIMIT = 1000, 5000


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
class ForEach:
    """Per-item calls (RFC 0006 section 3): `operation` once per item a list operation returns."""

    operation: str
    bind: dict[str, str]  # operation parameter -> path in each listed item
    params: dict[str, str] = field(default_factory=dict)
    items: str = ""
    limit: int = DEFAULT_LIMIT

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ForEach":
        return cls(
            operation=str(data.get("operation") or ""),
            bind={str(k): str(v) for k, v in (data.get("bind") or {}).items()},
            params={str(k): str(v) for k, v in (data.get("params") or {}).items()},
            items=str(data.get("items") or ""),
            limit=int(data.get("limit") or DEFAULT_LIMIT),
        )


@dataclass
class Query:
    operation: str
    params: dict[str, str] = field(default_factory=dict)
    items: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    max_items: int | None = None  # a dry run stops early; pages are fetched lazily
    for_each: ForEach | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Query":
        return cls(
            operation=str(data.get("operation") or ""),
            params={str(k): str(v) for k, v in (data.get("params") or {}).items()},
            items=str(data.get("items") or ""),
            fields={str(k): str(v) for k, v in (data.get("fields") or {}).items()},
            max_items=int(data["max_items"]) if data.get("max_items") else None,
            for_each=ForEach.from_json(data["for_each"]) if data.get("for_each") else None,
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


def _find(
    description: Description, settings: Settings, name: str, pointer: str, problems: list[Problem]
) -> Operation | None:
    operation = description.operations.get(name)
    if operation is None:
        close = difflib.get_close_matches(name, list(description.operations), n=3, cutoff=0.6)
        hint = f"; did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
        problems.append(Problem(pointer, f"no operation {name!r} in the description{hint}"))
        return None
    if operation.method != "GET" and operation.id not in settings.read_operations:
        problems.append(
            Problem(
                pointer,
                f"{operation.id} is {operation.method} {operation.path}: only reads can be queried",
            )
        )
        return None
    return operation


def _check_params(
    operation: Operation, params: Mapping[str, str], prefix: str, bound: set[str]
) -> list[Problem]:
    """Section 5 parameter rules; `bound` parameters are filled per item by for_each."""
    problems: list[Problem] = []
    for key, value in params.items():
        pointer = f"{prefix}/{key}"
        declared = operation.parameters.get(key)
        if declared is None:
            problems.append(Problem(pointer, f"{operation.id} has no parameter {key!r}"))
            continue
        if key in bound:
            problems.append(Problem(pointer, f"for_each binds {key}; leave it out of params"))
            continue
        if len(value) > MAX_PARAM:
            problems.append(Problem(pointer, f"longer than {MAX_PARAM} characters"))
        if declared["in"] == "path" and ("/" in value or value in ("", ".", "..")):
            problems.append(Problem(pointer, "a path parameter cannot contain / or be empty"))
        kind = _type(declared["schema"])
        if kind == "integer" and not re.fullmatch(r"-?\d+", value):
            problems.append(Problem(pointer, f"{key} is an integer"))
        if kind == "boolean" and value not in ("true", "false"):
            problems.append(Problem(pointer, f"{key} is true or false"))
        enum = declared["schema"].get("enum")
        if enum and value not in [str(e) for e in enum]:
            problems.append(Problem(pointer, f"{key} is one of {', '.join(map(str, enum))}"))
    for key, declared in operation.parameters.items():
        if declared["in"] == "path" and key not in params and key not in bound:
            problems.append(Problem(f"{prefix}/{key}", f"{operation.path} needs {key}"))
    return problems


def _list_item(
    description: Description, operation: Operation, items: str, pointer: str, problems: list[Problem]
) -> dict[str, Any] | None:
    """The schema of one item of the operation's list, or None after recording why."""
    if operation.response is None:
        problems.append(
            Problem(pointer.replace("/items", "/operation"), f"{operation.id} documents no JSON response")
        )
        return None
    if items and not all(PATH_SEGMENT.match(p) for p in items.split(".")):
        problems.append(Problem(pointer, "a dotted path of property names, or empty"))
        return None
    container, _ = description.at(operation.response, items)
    if container is None:
        problems.append(Problem(pointer, f"the response has no {items!r}"))
        return None
    if _type(container).split(" ")[0] != "array":
        where = f"{items!r}" if items else "the response body"
        problems.append(Problem(pointer, f"{where} is not a list; set items to the path of the list"))
        return None
    return container.get("items") or {}


def _field(
    description: Description, item: Any, path: str, pointer: str, problems: list[Problem]
) -> tuple[dict[str, Any], bool] | None:
    """The projected schema of a dotted path in an item, and whether it is required."""
    if path == SELF:
        return {"type": _json_type(description.schema(item))}, True
    if not path or not all(PATH_SEGMENT.match(p) for p in path.split(".")):
        problems.append(Problem(pointer, "a dotted path of property names, such as profile.login"))
        return None
    node, required = description.at(item, path)
    if node is None:
        known = [f["path"] for f in description.fields_of(item) if not f["path"].startswith("_")]
        close = difflib.get_close_matches(path, known, n=3, cutoff=0.6)
        hint = (
            f"did you mean {' or '.join(repr(c) for c in close)}?"
            if close
            else f"top-level fields: {', '.join(p for p in known if '.' not in p)[:300]}"
        )
        free_form = bool(description.schema(item).get("additionalProperties"))
        problems.append(Problem(pointer, f"items have no {path!r}; {hint}", warning=free_form))
        return {}, False
    types = _json_type(node)
    schema: dict[str, Any] = (
        {"type": types if not required else [t for t in types if t != "null"]} if types else {}
    )
    if node.get("description"):
        schema["description"] = node["description"][:200]
    return schema, required


def validate(
    description: Description, settings: Settings, query: Query, *, name: str | None = None
) -> tuple[list[Problem], dict[str, Any] | None]:
    """Static checks (RFC 0006 sections 3 and 5) and the derived item schema."""
    problems: list[Problem] = []
    if name is not None and not QUERY_NAME.match(name):
        problems.append(Problem("/name", "a name matches [a-z0-9-]+"))
    operation = _find(description, settings, query.operation, "/operation", problems)
    if operation is None:
        return problems, None

    # Per-item calls, then parameters.
    fe = query.for_each
    parent_item: Any = None
    if fe is not None:
        parent = _find(description, settings, fe.operation, "/for_each/operation", problems)
        if parent is not None:
            if not (1 <= fe.limit <= MAX_LIMIT):
                problems.append(Problem("/for_each/limit", f"between 1 and {MAX_LIMIT}"))
            problems += _check_params(parent, fe.params, "/for_each/params", set())
            parent_item = _list_item(description, parent, fe.items, "/for_each/items", problems)
            if not fe.bind:
                problems.append(Problem("/for_each/bind", f"bind at least one parameter of {operation.id}"))
            for key, path in fe.bind.items():
                pointer = f"/for_each/bind/{key}"
                if key not in operation.parameters:
                    problems.append(Problem(pointer, f"{operation.id} has no parameter {key!r}"))
                elif parent_item is not None:
                    _field(description, parent_item, path, pointer, problems)
    problems += _check_params(operation, query.params, "/params", set(fe.bind) if fe else set())

    item = _list_item(description, operation, query.items, "/items", problems)
    if item is None:
        return problems, None

    if not query.fields:
        problems.append(Problem("/fields", "project at least id and name"))
    for required in ("id", "name"):
        if query.fields and required not in query.fields:
            problems.append(Problem(f"/fields/{required}", f"{required} is required: it names failing items"))
    properties: dict[str, Any] = {}
    for key, path in query.fields.items():
        pointer = f"/fields/{key}"
        if not FIELD_NAME.match(key):
            problems.append(Problem(pointer, "output field names match [a-z][a-z0-9_]*"))
            continue
        if fe is not None and key == "error":
            problems.append(Problem(pointer, "error is added to every item of this query"))
            continue
        if path.startswith(PARENT_PREFIX):
            if fe is None:
                problems.append(Problem(pointer, "$parent reads the listed item of a for_each query"))
            elif parent_item is not None:
                found = _field(description, parent_item, path[len(PARENT_PREFIX) :], pointer, problems)
                if found is not None:
                    properties[key] = found[0]
            continue
        found = _field(description, item, path, pointer, problems)
        if found is None:
            continue
        schema, _ = found
        if fe is not None and "type" in schema and "null" not in schema["type"]:
            schema = {**schema, "type": [*schema["type"], "null"]}  # empty when the call failed
        properties[key] = schema
    if fe is not None:
        properties["error"] = {"type": ["string", "null"]}
    schema = {
        "type": "object",
        "required": sorted({*query.fields, *(["error"] if fe else [])}),
        "properties": properties,
    }
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


def _vendor_detail(response: Any) -> str | None:
    """The vendor's error code and message, from common JSON error shapes (Okta, GitHub)."""
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, Mapping):
        return None
    code = body.get("errorCode") or body.get("code") or body.get("error")
    message = body.get("errorSummary") or body.get("message") or body.get("error_description")
    if isinstance(code, str) and code:
        return f"{code}: {message}" if isinstance(message, str) and message else code
    return message if isinstance(message, str) and message else None


def _vendor_code(exc: IntegrationError) -> str:
    found = re.search(r"answered \d+: ([A-Za-z0-9_.-]+)", exc.message)
    return found.group(1) if found else f"HTTP {exc.status or 'error'}"


def _pages(
    ctx: Any,
    settings: Settings,
    operation: Operation,
    params: Mapping[str, str],
    items: str,
    base: str,
    headers: Mapping[str, str],
) -> Iterator[Any]:
    """Every raw item of one operation, following the declared pagination."""
    path = operation.path
    query_params: dict[str, str] = {}
    for key, value in params.items():
        if operation.parameters[key]["in"] == "path":
            path = path.replace("{" + key + "}", quote(value, safe=""))
        else:
            query_params[key] = value
    url = base.rstrip("/") + path

    def items_of(response: Any) -> list[Any]:
        body = response.json()
        found = _get(body, items) if items else body
        if not isinstance(found, list):
            raise InvalidConfig(f"the response's {items or 'body'} is not a list")
        return found

    common = {"params": query_params, "headers": headers, "items": items_of, "error_detail": _vendor_detail}
    kind, _, rest = settings.pagination.partition(":")
    if kind == "link":
        return ctx.http.paginate(url, **common)
    if kind == "next-url":
        return ctx.http.paginate(url, **common, next_url=lambda r: _get(r.json(), rest))
    if kind == "cursor":
        source, _, param = rest.partition(":")
        return ctx.http.paginate(url, **common, cursor=lambda r: _get(r.json(), source), cursor_param=param)
    return iter(
        items_of(ctx.http.get(url, params=query_params, headers=headers, error_detail=_vendor_detail))
    )


def _project(
    raw: Any,
    fields: Mapping[str, str],
    parent: Any = None,
    error: str | None = None,
    per_item: bool = False,
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key, path in fields.items():
        if path.startswith(PARENT_PREFIX):
            rest = path[len(PARENT_PREFIX) :]
            projected[key] = parent if rest == SELF else _get(parent, rest)
        elif raw is None:
            projected[key] = None
        else:
            projected[key] = raw if path == SELF else _get(raw, path)
    for key in ("id", "name"):
        if projected.get(key) is not None and not isinstance(projected[key], str):
            projected[key] = str(projected[key])
    if per_item:
        projected["error"] = error
    return projected


def _per_item(
    ctx: Any,
    description: Description,
    settings: Settings,
    query: Query,
    base: str,
    headers: Mapping[str, str],
) -> Iterator[dict[str, Any]]:
    """for_each (RFC 0006 section 3): one call of the operation per listed item."""
    fe = query.for_each
    assert fe is not None
    operation = description.operations[query.operation]
    parent_op = description.operations[fe.operation]
    listed = _pages(ctx, settings, parent_op, fe.params, fe.items, base, headers)
    for count, parent in enumerate(listed, 1):
        if count > fe.limit:
            raise Incomplete(f"{fe.operation} listed more than {fe.limit} items")
        bound: dict[str, str] = {}
        for key, path in fe.bind.items():
            value = parent if path == SELF else _get(parent, path)
            if value is not None:
                bound[key] = value if isinstance(value, str) else json.dumps(value)
        if len(bound) < len(fe.bind):
            yield _project(None, query.fields, parent, "MissingParentValue", True)
            continue
        unsafe = [
            key
            for key, value in bound.items()
            if operation.parameters[key]["in"] == "path"
            and ("/" in value or value in ("", ".", "..") or len(value) > MAX_PARAM)
        ]
        if unsafe:
            yield _project(None, query.fields, parent, "InvalidParentValue", True)
            continue
        try:
            for raw in _pages(
                ctx, settings, operation, {**query.params, **bound}, query.items, base, headers
            ):
                yield _project(raw, query.fields, parent, None, True)
        except (AuthFailed, PermissionDenied, RateLimited):
            raise
        except IntegrationError as exc:
            if exc.status is None or not 400 <= exc.status < 500:
                raise
            yield _project(None, query.fields, parent, _vendor_code(exc), True)


def execute(ctx: Any, description: Description, settings: Settings, query: Query) -> Iterator[dict[str, Any]]:
    """Run a validated query and yield projected items. Re-checks the read-only rules."""
    problems, _ = validate(description, settings, query)
    errors = [p for p in problems if not p.warning]
    if errors:
        raise InvalidConfig("; ".join(f"{p.pointer}: {p.detail}" for p in errors))
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
    headers = {}
    auth = authorization(settings, ctx.env)
    if settings.authorization and auth is None:
        raise InvalidConfig("no credential for this integration's authorization")
    if auth:
        headers["Authorization"] = auth

    produced: Iterator[dict[str, Any]]
    if query.for_each is None:
        operation = description.operations[query.operation]
        produced = (
            _project(raw, query.fields)
            for raw in _pages(ctx, settings, operation, query.params, query.items, base, headers)
        )
    else:
        produced = _per_item(ctx, description, settings, query, base, headers)
    if query.max_items is not None:
        produced = itertools.islice(produced, query.max_items)
    yield from produced


# --- bundling ---------------------------------------------------------------------------------


def encode(settings: Settings, document: Mapping[str, Any]) -> str:
    """The generated module `pack` writes: settings and the description, compressed."""
    payload = json.dumps({"settings": settings.to_json(), "description": document}, separators=(",", ":"))
    blob = base64.b64encode(zlib.compress(payload.encode(), 9)).decode()
    lines = [blob[i : i + 100] for i in range(0, len(blob), 100)]
    return (
        '"""Generated by `fathom-integration openapi build`: vendor description and query settings."""\n\n'
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
