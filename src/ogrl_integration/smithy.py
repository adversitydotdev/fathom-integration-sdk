"""Query sources over AWS Smithy models (OGRL RFC 0007).

The integration bundles Smithy 2.0 JSON AST models from `aws/api-models-aws`;
organizations define queries over their read operations, named
`<service>.<Operation>`. As with `openapi`, OGRL validates a query before it
is saved and the integration validates it again and runs it. Requests are
serialized and responses read from the model's shapes, so a field path means
the same thing whatever the wire protocol. Standard library only.

    api = Api(settings, {"ec2": ec2_model_json, ...})
    problems, item_schema = validate(api, query)
    for item in execute(ctx, api, query): ...
"""

import base64
import difflib
import importlib
import itertools
import json
import re
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

from ogrl_integration import sigv4
from ogrl_integration.errors import Incomplete, IntegrationError, InvalidConfig, RateLimited
from ogrl_integration.openapi import FIELD_NAME, MAX_PARAM, QUERY_NAME, Problem
from ogrl_integration.openapi import Query as _OpenApiQuery

GENERATED_MODULE = "_ogrl_smithy"
PROTOCOLS = ("awsJson1_0", "awsJson1_1", "restJson1", "awsQuery", "ec2Query", "restXml")
SUPPORTED = ("awsJson1_0", "awsJson1_1", "restJson1", "awsQuery", "ec2Query")
REST = ("restJson1", "restXml")
READ_PREFIX = re.compile(r"^(List|Describe|Get)[A-Z]")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
MEMBER_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
SELF, REGION_PATH = ".", "$region"
MAX_DEPTH = 6
MAX_PAGES = 10_000
THROTTLE_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestLimitExceeded",
        "RequestThrottled",
        "RequestThrottledException",
        "TooManyRequestsException",
        "SlowDown",
    }
)

_SIMPLE = {
    "String": "string",
    "Blob": "blob",
    "Boolean": "boolean",
    "PrimitiveBoolean": "boolean",
    "Byte": "byte",
    "PrimitiveByte": "byte",
    "Short": "short",
    "PrimitiveShort": "short",
    "Integer": "integer",
    "PrimitiveInteger": "integer",
    "Long": "long",
    "PrimitiveLong": "long",
    "Float": "float",
    "PrimitiveFloat": "float",
    "Double": "double",
    "PrimitiveDouble": "double",
    "BigInteger": "bigInteger",
    "BigDecimal": "bigDecimal",
    "Timestamp": "timestamp",
    "Document": "document",
}
PRELUDE: dict[str, dict[str, Any]] = {f"smithy.api#{k}": {"type": v} for k, v in _SIMPLE.items()}
PRELUDE["smithy.api#Unit"] = {"type": "structure", "members": {}}
INTEGERS = ("byte", "short", "integer", "long", "bigInteger", "intEnum")
NUMBERS = ("float", "double", "bigDecimal")
SCALARS = ("string", "enum", "blob", "boolean", "timestamp", *INTEGERS, *NUMBERS)
# Traits the executor and validator read; `trim` drops every other one.
KEPT_TRAITS = frozenset(
    {
        "smithy.api#required",
        "smithy.api#readonly",
        "smithy.api#paginated",
        "smithy.api#http",
        "smithy.api#httpLabel",
        "smithy.api#httpQuery",
        "smithy.api#httpQueryParams",
        "smithy.api#httpHeader",
        "smithy.api#httpPrefixHeaders",
        "smithy.api#httpPayload",
        "smithy.api#httpResponseCode",
        "smithy.api#jsonName",
        "smithy.api#xmlName",
        "smithy.api#xmlFlattened",
        "smithy.api#xmlAttribute",
        "smithy.api#xmlNamespace",
        "smithy.api#timestampFormat",
        "smithy.api#enumValue",
        "smithy.api#title",
        "aws.protocols#ec2QueryName",
        "aws.auth#sigv4",
        "aws.api#service",
        *(f"aws.protocols#{p}" for p in PROTOCOLS),
    }
)


# --- settings ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Service:
    """One entry of the manifest's `smithy.services`."""

    model: str
    endpoint: str
    signing_region: str = "us-east-1"
    target_prefix: str = ""  # awsJson services whose X-Amz-Target predates their Smithy name

    @property
    def regional(self) -> bool:
        return "{region}" in self.endpoint


@dataclass(frozen=True)
class Settings:
    """The manifest's `smithy` block."""

    services: dict[str, Service]
    read_operations: tuple[str, ...] = ()
    deny_operations: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Settings":
        return cls(
            services={
                name: Service(
                    model=block["model"],
                    endpoint=block["endpoint"],
                    signing_region=block.get("signing_region") or "us-east-1",
                    target_prefix=block.get("target_prefix") or "",
                )
                for name, block in (data.get("services") or {}).items()
            },
            read_operations=tuple(data.get("read_operations") or ()),
            deny_operations=tuple(data.get("deny_operations") or ()),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "services": {
                name: {
                    "model": s.model,
                    "endpoint": s.endpoint,
                    "signing_region": s.signing_region,
                    **({"target_prefix": s.target_prefix} if s.target_prefix else {}),
                }
                for name, s in self.services.items()
            },
            "read_operations": list(self.read_operations),
            "deny_operations": list(self.deny_operations),
        }


@dataclass
class Query(_OpenApiQuery):
    """RFC 0006's definition plus `regions`: region codes, or "*" for the connection's."""

    regions: list[str] | str = field(default_factory=list)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Query":
        base = _OpenApiQuery.from_json(data)
        raw = data.get("regions") or []
        regions: list[str] | str = raw if isinstance(raw, str) else [str(r) for r in raw]
        return cls(base.operation, base.params, base.items, base.fields, base.max_items, regions)


# --- models -----------------------------------------------------------------------------------


class Model:
    """One Smithy JSON AST document holding one AWS service."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        if not str(document.get("smithy", "")).startswith("2."):
            raise ValueError("a Smithy 2.0 JSON AST model is required")
        self.document = document
        self.shapes: Mapping[str, Any] = document.get("shapes") or {}
        services = [k for k, v in self.shapes.items() if v.get("type") == "service"]
        if len(services) != 1:
            raise ValueError(f"expected one service shape, found {len(services)}")
        self.service_id = services[0]
        service = self.shapes[self.service_id]
        traits = service.get("traits") or {}
        self.version = str(service.get("version") or "")
        self.protocol = next((p for p in PROTOCOLS if f"aws.protocols#{p}" in traits), "")
        self.signing_name = (traits.get("aws.auth#sigv4") or {}).get("name") or ""
        self.title = traits.get("smithy.api#title") or self.service_id.split("#")[-1]
        self.paginated = traits.get("smithy.api#paginated") or {}
        self.operations: dict[str, str] = {}  # Operation name -> shape id
        self._collect_operations(service)

    def _collect_operations(self, container: Mapping[str, Any], depth: int = 0) -> None:
        for ref in container.get("operations") or []:
            self.operations[ref["target"].split("#")[-1]] = ref["target"]
        if depth > 8:
            return
        for ref in container.get("resources") or []:
            resource = self.shapes.get(ref["target"]) or {}
            for key in ("create", "put", "read", "update", "delete", "list"):
                if resource.get(key):
                    target = resource[key]["target"]
                    self.operations[target.split("#")[-1]] = target
            for key in ("collectionOperations",):
                for op in resource.get(key) or []:
                    self.operations[op["target"].split("#")[-1]] = op["target"]
            self._collect_operations(resource, depth + 1)

    def shape(self, shape_id: str) -> dict[str, Any]:
        found = self.shapes.get(shape_id) or PRELUDE.get(shape_id)
        if found is None:
            raise ValueError(f"the model has no shape {shape_id}")
        return found

    def kind(self, shape_id: str) -> str:
        return str(self.shape(shape_id).get("type"))

    def members(self, shape_id: str) -> dict[str, tuple[str, dict[str, Any]]]:
        """Member name -> (target, member traits)."""
        shape = self.shape(shape_id)
        return {
            name: (m["target"], m.get("traits") or {}) for name, m in (shape.get("members") or {}).items()
        }

    def traits(self, shape_id: str) -> dict[str, Any]:
        return self.shape(shape_id).get("traits") or {}

    def item(self, shape_id: str) -> tuple[str, dict[str, Any]]:
        """A list's member: (target, traits)."""
        member = self.shape(shape_id).get("member") or {}
        return member.get("target", "smithy.api#Document"), member.get("traits") or {}

    def enum_values(self, shape_id: str) -> list[str]:
        shape = self.shape(shape_id)
        values = []
        for name, member in (shape.get("members") or {}).items():
            value = (member.get("traits") or {}).get("smithy.api#enumValue", name)
            values.append(str(value))
        return values


@dataclass(frozen=True)
class Operation:
    name: str  # "<service>.<Operation>"
    service: str
    shape_id: str
    protocol: str
    method: str
    uri: str
    input: str
    output: str
    paginated: dict[str, str]
    readable: bool
    reason: str  # why it cannot be queried, when it cannot
    summary: str


def classify(
    name: str, operation: str, traits: Mapping[str, Any], protocol: str, settings: Settings
) -> tuple[bool, str]:
    """RFC 0007 section 3: can this operation be queried, and if not, why."""
    looks_read = (
        "smithy.api#readonly" in traits or READ_PREFIX.match(operation) or name in settings.read_operations
    )
    if not looks_read:
        return (
            False,
            f"{name} is not a read: it lacks @readonly and does not start with List, Describe, or Get",
        )
    method = (traits.get("smithy.api#http") or {}).get("method", "POST")
    if protocol in REST and method != "GET":
        return False, f"{name} is {method} under {protocol}: only GET operations can be queried"
    if name in settings.deny_operations:
        return False, f"{name} returns content or secrets; the integration denies it"
    if protocol not in SUPPORTED:
        return (
            False,
            f"{name} uses {protocol or 'an unknown protocol'}, which query sources do not support yet",
        )
    return True, ""


def _summary(traits: Mapping[str, Any]) -> str:
    text = re.sub(r"<[^>]+>", "", str(traits.get("smithy.api#documentation") or ""))
    text = " ".join(text.split())
    first = re.split(r"(?<=\.)\s", text, maxsplit=1)[0]
    return first[:160]


class Api:
    """Every bundled service, with operations named `<service>.<Operation>`."""

    def __init__(self, settings: Settings, models: Mapping[str, Mapping[str, Any] | Model]) -> None:
        self.settings = settings
        self.models: dict[str, Model] = {
            name: m if isinstance(m, Model) else Model(m) for name, m in models.items()
        }
        self.operations: dict[str, Operation] = {}
        for service, model in self.models.items():
            for op_name, shape_id in model.operations.items():
                shape = model.shape(shape_id)
                traits = shape.get("traits") or {}
                name = f"{service}.{op_name}"
                readable, reason = classify(name, op_name, traits, model.protocol, settings)
                http = traits.get("smithy.api#http") or {}
                paginated = {**model.paginated, **(traits.get("smithy.api#paginated") or {})}
                self.operations[name] = Operation(
                    name=name,
                    service=service,
                    shape_id=shape_id,
                    protocol=model.protocol,
                    method=http.get("method", "POST"),
                    uri=http.get("uri", "/"),
                    input=(shape.get("input") or {}).get("target", "smithy.api#Unit"),
                    output=(shape.get("output") or {}).get("target", "smithy.api#Unit"),
                    paginated=paginated if "smithy.api#paginated" in traits else {},
                    readable=readable,
                    reason=reason,
                    summary=_summary(traits),
                )

    def model(self, operation: Operation) -> Model:
        return self.models[operation.service]

    def search(self, text: str, limit: int = 50) -> list[Operation]:
        """Queryable operations whose name or summary holds every word, names first."""
        words = text.lower().split()
        found = [
            op
            for op in self.operations.values()
            if op.readable and all(w in f"{op.name} {op.summary}".lower() for w in words)
        ]
        found.sort(key=lambda op: (not all(w in op.name.lower() for w in words), op.name))
        return found[:limit]

    def fields_of(
        self, model: Model, shape_id: str, prefix: str = "", depth: int = 0
    ) -> list[dict[str, Any]]:
        """Every dotted member path in a structure with its type: what a query can project."""
        out: list[dict[str, Any]] = []
        if model.kind(shape_id) not in ("structure", "union"):
            return out
        for name, (target, _) in sorted(model.members(shape_id).items()):
            path = f"{prefix}{name}"
            out.append({"path": path, "type": _describe(model, target)})
            if model.kind(target) == "structure" and depth < MAX_DEPTH:
                out += self.fields_of(model, target, f"{path}.", depth + 1)
        return out

    def describe(self, name: str) -> dict[str, Any]:
        """Parameters, the item shape as flat paths, and the read classification."""
        op = self.operations[name]
        model = self.model(op)
        item, _ = _items_target(model, op, "")
        params = []
        reserved = _reserved_inputs(op)
        for member, (target, traits) in sorted(model.members(op.input).items()):
            if member in reserved or _bound_elsewhere(traits):
                continue
            kind = model.kind(target)
            params.append(
                {
                    "name": member,
                    "type": _describe(model, target),
                    "required": "smithy.api#required" in traits,
                    "json": kind not in SCALARS,
                }
            )
        regional = (
            self.settings.services[op.service].regional if op.service in self.settings.services else False
        )
        return {
            "operation": op.name,
            "protocol": op.protocol,
            "summary": op.summary,
            "queryable": op.readable,
            **({"reason": op.reason} if op.reason else {}),
            "regional": regional,
            "paginated": bool(op.paginated),
            "items": op.paginated.get("items", ""),
            "item": _describe(model, item) if item else None,
            "params": params,
            "fields": self.fields_of(model, item) if item else [],
        }


def _describe(model: Model, target: str) -> str:
    kind = model.kind(target)
    if kind in ("list", "set"):
        return f"array of {_describe(model, model.item(target)[0])}"
    if kind in ("structure", "union", "map", "document"):
        return "object"
    return _json_kind(kind)


def _json_kind(kind: str) -> str:
    if kind in ("string", "enum", "blob", "timestamp"):
        return "string"
    if kind in INTEGERS:
        return "integer"
    if kind in NUMBERS:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind in ("list", "set"):
        return "array"
    return "object"


def _bound_elsewhere(traits: Mapping[str, Any]) -> bool:
    """Input members a query cannot set: headers, payloads, and free-form query maps."""
    return any(
        t in traits
        for t in (
            "smithy.api#httpHeader",
            "smithy.api#httpPrefixHeaders",
            "smithy.api#httpPayload",
            "smithy.api#httpQueryParams",
        )
    )


def _reserved_inputs(op: Operation) -> set[str]:
    return {p.split(".")[0] for p in (op.paginated.get("inputToken"), op.paginated.get("pageSize")) if p}


def _member_at(model: Model, shape_id: str, dotted: str) -> tuple[str | None, bool]:
    """The target at a dotted member path, and whether every step is @required."""
    current, required = shape_id, True
    for part in dotted.split("."):
        if model.kind(current) not in ("structure", "union"):
            return None, False
        members = model.members(current)
        if part not in members:
            return None, False
        target, traits = members[part]
        required = required and "smithy.api#required" in traits
        current = target
    return current, required


def _items_target(model: Model, op: Operation, items: str) -> tuple[str | None, str]:
    """The item shape a query projects from, and the path that holds the list ("" = one item)."""
    path = items or op.paginated.get("items", "")
    if not path:
        return op.output, ""
    target, _ = _member_at(model, op.output, path)
    if target is None or model.kind(target) not in ("list", "set"):
        return None, path
    return model.item(target)[0], path


# --- validating -------------------------------------------------------------------------------


def _hint(path: str, known: list[str], what: str = "top-level members") -> str:
    close = difflib.get_close_matches(path, known, n=3, cutoff=0.6)
    if close:
        return f"did you mean {' or '.join(repr(c) for c in close)}?"
    listed = ", ".join(p for p in known if "." not in p or what != "top-level members")
    return f"{what}: {listed[:300]}" if listed else f"there are no {what}"


def _check_scalar(model: Model, target: str, value: Any, where: str) -> list[str]:
    kind = model.kind(target)
    if kind in ("string", "blob"):
        return [] if isinstance(value, str) else [f"{where} is a string"]
    if kind == "enum":
        allowed = model.enum_values(target)
        return [] if value in allowed else [f"{where} is one of {', '.join(allowed)}"]
    if kind in INTEGERS:
        ok = isinstance(value, int) and not isinstance(value, bool)
        if ok and kind == "intEnum" and str(value) not in model.enum_values(target):
            return [f"{where} is one of {', '.join(model.enum_values(target))}"]
        return [] if ok else [f"{where} is an integer"]
    if kind in NUMBERS:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        return [] if ok else [f"{where} is a number"]
    if kind == "boolean":
        return [] if isinstance(value, bool) else [f"{where} is true or false"]
    if kind == "timestamp":
        if isinstance(value, int | float) and not isinstance(value, bool):
            return []
        return [] if isinstance(value, str) and _parse_time(value) else [f"{where} is an RFC 3339 time"]
    return []


def check_value(model: Model, target: str, value: Any, where: str, depth: int = 0) -> list[str]:
    """Problems with a JSON value against a shape; `where` names it in messages."""
    if depth > 20:
        return [f"{where} nests too deeply"]
    kind = model.kind(target)
    if kind in ("list", "set"):
        if not isinstance(value, list):
            return [f"{where} is a JSON array"]
        member = model.item(target)[0]
        return [
            p for i, v in enumerate(value) for p in check_value(model, member, v, f"{where}[{i}]", depth + 1)
        ]
    if kind == "map":
        if not isinstance(value, dict):
            return [f"{where} is a JSON object"]
        member = model.shape(target)["value"]["target"]
        return [p for k, v in value.items() for p in check_value(model, member, v, f"{where}.{k}", depth + 1)]
    if kind in ("structure", "union"):
        if not isinstance(value, dict):
            return [f"{where} is a JSON object"]
        members = model.members(target)
        problems = []
        for key, item in value.items():
            if key not in members:
                problems.append(f"{where} has no member {key!r}; {_hint(key, list(members))}")
                continue
            problems += check_value(model, members[key][0], item, f"{where}.{key}", depth + 1)
        for key, (_, traits) in members.items():
            if kind == "structure" and "smithy.api#required" in traits and key not in value:
                problems.append(f"{where} needs {key}")
        return problems
    if kind == "document":
        return []
    return _check_scalar(model, target, value, where)


def _coerce(model: Model, target: str, text: str) -> tuple[Any, str | None]:
    """A parameter's string value as the member's type, or an error."""
    kind = model.kind(target)
    if kind in ("string", "blob", "enum", "timestamp"):
        value: Any = text
    elif kind in INTEGERS:
        if not re.fullmatch(r"-?\d+", text):
            return None, "is an integer"
        value = int(text)
    elif kind in NUMBERS:
        try:
            value = float(text)
        except ValueError:
            return None, "is a number"
    elif kind == "boolean":
        if text not in ("true", "false"):
            return None, "is true or false"
        value = text == "true"
    else:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"is JSON text: {exc.msg} at character {exc.pos}"
    return value, None


def _regions(query: Query, config: Mapping[str, str]) -> list[str]:
    if query.regions == "*":
        listed = config.get("regions") or config.get("region") or ""
        return [r.strip() for r in listed.split(",") if r.strip()]
    if isinstance(query.regions, str):
        return [r.strip() for r in query.regions.split(",") if r.strip()]
    return list(query.regions)


def validate(
    api: Api, query: Query, *, name: str | None = None
) -> tuple[list[Problem], dict[str, Any] | None]:
    """Static checks (RFC 0007 sections 3, 4, and 6) and the derived item schema."""
    problems: list[Problem] = []
    if name is not None and not QUERY_NAME.match(name):
        problems.append(Problem("/name", "a name matches [a-z0-9-]+"))
    op = api.operations.get(query.operation)
    if op is None:
        service, _, bare = query.operation.partition(".")
        if not bare or service not in api.models:
            detail = (
                f"name an operation as <service>.<Operation>, with service one of {', '.join(api.models)}"
            )
        else:
            known = [n for n, o in api.operations.items() if o.service == service and o.readable]
            close = difflib.get_close_matches(query.operation, known, n=3, cutoff=0.6)
            hint = f"did you mean {' or '.join(repr(c) for c in close)}?" if close else "search for one"
            detail = f"no queryable operation {query.operation!r}; {hint}"
        problems.append(Problem("/operation", detail))
        return problems, None
    if not op.readable:
        problems.append(Problem("/operation", op.reason))
        return problems, None
    model = api.model(op)
    service = api.settings.services.get(op.service)
    if service is None:
        problems.append(Problem("/operation", f"the integration declares no endpoint for {op.service}"))
        return problems, None

    # Regions.
    if isinstance(query.regions, list):
        for i, region in enumerate(query.regions):
            if not REGION.match(region):
                problems.append(
                    Problem(f"/regions/{i}", f"{region!r} is not a region code such as us-east-1")
                )
    elif query.regions not in ("*", ""):
        problems.append(Problem("/regions", 'a list of region codes, or "*" for the connection\'s regions'))
    if service.regional and not query.regions:
        problems.append(Problem("/regions", f'{op.service} is regional: list regions, or "*"'))
    if not service.regional and query.regions:
        problems.append(Problem("/regions", f"{op.service} is global: leave regions out"))

    # Parameters.
    members = model.members(op.input)
    reserved = _reserved_inputs(op)
    for key, value in query.params.items():
        pointer = f"/params/{key}"
        if key not in members:
            problems.append(
                Problem(pointer, f"{op.name} has no parameter {key!r}; {_hint(key, list(members))}")
            )
            continue
        target, traits = members[key]
        if key in reserved:
            problems.append(Problem(pointer, f"{key} is the paginator's; leave it out"))
            continue
        if _bound_elsewhere(traits):
            problems.append(
                Problem(pointer, f"{key} is sent as a header or payload, which queries cannot set")
            )
            continue
        if len(value) > MAX_PARAM:
            problems.append(Problem(pointer, f"longer than {MAX_PARAM} characters"))
            continue
        typed, error = _coerce(model, target, value)
        if error:
            problems.append(Problem(pointer, f"{key} {error}"))
            continue
        if "smithy.api#httpLabel" in traits and ("/" in value or value in ("", ".", "..")):
            problems.append(Problem(pointer, "a path parameter cannot contain / or be empty"))
        for detail in check_value(model, target, typed, key):
            problems.append(Problem(pointer, detail))
    for key, (_, traits) in members.items():
        if "smithy.api#required" in traits and key not in query.params and key not in reserved:
            problems.append(Problem(f"/params/{key}", f"{op.name} needs {key}"))
    if op.protocol in REST and op.method == "GET":
        for key in query.params:
            if key in members and not any(
                t in members[key][1] for t in ("smithy.api#httpLabel", "smithy.api#httpQuery")
            ):
                problems.append(Problem(f"/params/{key}", f"{key} is not sent by GET {op.uri}"))

    # Items.
    if query.items and not MEMBER_PATH.match(query.items):
        problems.append(Problem("/items", "a dotted path of member names, or empty"))
        return problems, None
    item, path = _items_target(model, op, query.items)
    if item is None:
        output_members = [f["path"] for f in api.fields_of(model, op.output)]
        if _member_at(model, op.output, path)[0] is None:
            problems.append(Problem("/items", f"the output has no {path!r}; {_hint(path, output_members)}"))
        else:
            problems.append(Problem("/items", f"{path!r} is not a list"))
        return problems, None

    # Fields.
    if not query.fields:
        problems.append(Problem("/fields", "project at least id and name"))
    for required in ("id", "name"):
        if query.fields and required not in query.fields:
            problems.append(Problem(f"/fields/{required}", f"{required} is required: it names failing items"))
    properties: dict[str, Any] = {}
    structured = model.kind(item) in ("structure", "union")
    known = [f["path"] for f in api.fields_of(model, item)]
    for key, member_path in query.fields.items():
        pointer = f"/fields/{key}"
        if not FIELD_NAME.match(key):
            problems.append(Problem(pointer, "output field names match [a-z][a-z0-9_]*"))
            continue
        if key == "region" and service.regional:
            problems.append(Problem(pointer, "region is added to every item of a regional query"))
            continue
        if member_path == REGION_PATH:
            if not service.regional:
                problems.append(Problem(pointer, f"{op.service} is global: items have no region"))
                continue
            properties[key] = {"type": ["string"]}
            continue
        if member_path == SELF:
            if structured:
                problems.append(Problem(pointer, "'.' is the item itself, for lists of strings or numbers"))
                continue
            properties[key] = {"type": [_json_kind(model.kind(item))]}
            continue
        if not MEMBER_PATH.match(member_path):
            problems.append(Problem(pointer, "a dotted path of member names, such as Placement.Tenancy"))
            continue
        if not structured:
            problems.append(Problem(pointer, f"items are {_describe(model, item)}; project them with '.'"))
            continue
        target, required = _member_at(model, item, member_path)
        if target is None:
            problems.append(Problem(pointer, f"items have no {member_path!r}; {_hint(member_path, known)}"))
            continue
        kind = _json_kind(model.kind(target))
        properties[key] = {"type": [kind] if required else [kind, "null"]}
    if service.regional:
        properties["region"] = {"type": ["string"]}
    schema = {
        "type": "object",
        "required": sorted({*query.fields, *(["region"] if service.regional else [])}),
        "properties": properties,
    }
    return problems, schema


# --- serializing ------------------------------------------------------------------------------


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: Any) -> str:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = _parse_time(str(value))
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else str(value)


def _epoch(value: Any) -> float | int:
    if isinstance(value, int | float):
        return value
    parsed = _parse_time(str(value))
    if parsed is None:
        raise InvalidConfig(f"{value!r} is not a time")
    return int(parsed.timestamp()) if parsed.microsecond == 0 else parsed.timestamp()


def _to_json(model: Model, target: str, value: Any, json_names: bool) -> Any:
    kind = model.kind(target)
    if kind in ("structure", "union") and isinstance(value, dict):
        members = model.members(target)
        out = {}
        for key, item in value.items():
            member_target, traits = members[key]
            wire = traits.get("smithy.api#jsonName", key) if json_names else key
            out[wire] = _to_json(model, member_target, item, json_names)
        return out
    if kind in ("list", "set") and isinstance(value, list):
        member = model.item(target)[0]
        return [_to_json(model, member, v, json_names) for v in value]
    if kind == "map" and isinstance(value, dict):
        member = model.shape(target)["value"]["target"]
        return {k: _to_json(model, member, v, json_names) for k, v in value.items()}
    if kind == "timestamp":
        return _epoch(value)
    return value


def _form_scalar(model: Model, target: str, value: Any) -> str:
    kind = model.kind(target)
    if isinstance(value, bool):
        return "true" if value else "false"
    if kind == "timestamp":
        return _iso(value)
    return str(value)


def _query_key(member: str, traits: Mapping[str, Any], ec2: bool) -> str:
    if ec2:
        if "aws.protocols#ec2QueryName" in traits:
            return str(traits["aws.protocols#ec2QueryName"])
        name = str(traits.get("smithy.api#xmlName", member))
        return name[:1].upper() + name[1:]
    return str(traits.get("smithy.api#xmlName", member))


def _to_form(
    model: Model,
    target: str,
    value: Any,
    prefix: str,
    out: dict[str, str],
    ec2: bool,
    flattened: bool = False,
) -> None:
    """awsQuery and ec2Query: `Name.member.1`, `Filter.1.Value.1`, `Tags.entry.1.key`."""
    kind = model.kind(target)
    if kind in ("structure", "union"):
        members = model.members(target)
        for key, item in value.items():
            member_target, traits = members[key]
            name = _query_key(key, traits, ec2)
            _to_form(
                model,
                member_target,
                item,
                f"{prefix}.{name}" if prefix else name,
                out,
                ec2,
                "smithy.api#xmlFlattened" in traits,
            )
    elif kind in ("list", "set"):
        member, member_traits = model.item(target)
        if not value and not ec2:
            out[prefix] = ""
        for i, item in enumerate(value, 1):
            if ec2 or flattened:
                key = f"{prefix}.{i}"
            else:
                key = f"{prefix}.{member_traits.get('smithy.api#xmlName', 'member')}.{i}"
            _to_form(model, member, item, key, out, ec2)
    elif kind == "map":
        shape = model.shape(target)
        key_name = (shape["key"].get("traits") or {}).get("smithy.api#xmlName", "key")
        value_name = (shape["value"].get("traits") or {}).get("smithy.api#xmlName", "value")
        for i, (k, v) in enumerate(value.items(), 1):
            entry = f"{prefix}.{i}" if flattened or ec2 else f"{prefix}.entry.{i}"
            out[f"{entry}.{key_name}"] = str(k)
            _to_form(model, shape["value"]["target"], v, f"{entry}.{value_name}", out, ec2)
    else:
        out[prefix] = _form_scalar(model, target, value)


# --- deserializing ----------------------------------------------------------------------------


def _from_json(model: Model, target: str, value: Any, json_names: bool, depth: int = 0) -> Any:
    if value is None or depth > 64:
        return value
    kind = model.kind(target)
    if kind in ("structure", "union") and isinstance(value, dict):
        out = {}
        for key, (member_target, traits) in model.members(target).items():
            wire = traits.get("smithy.api#jsonName", key) if json_names else key
            if wire in value:
                out[key] = _from_json(model, member_target, value[wire], json_names, depth + 1)
        return out
    if kind in ("list", "set") and isinstance(value, list):
        member = model.item(target)[0]
        return [_from_json(model, member, v, json_names, depth + 1) for v in value]
    if kind == "map" and isinstance(value, dict):
        member = model.shape(target)["value"]["target"]
        return {k: _from_json(model, member, v, json_names, depth + 1) for k, v in value.items()}
    if kind == "timestamp":
        return _iso(value)
    return value


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_scalar(model: Model, target: str, text: str | None) -> Any:
    text = (text or "").strip()
    kind = model.kind(target)
    if kind in INTEGERS:
        try:
            return int(text)
        except ValueError:
            return None
    if kind in NUMBERS:
        try:
            return float(text)
        except ValueError:
            return None
    if kind == "boolean":
        return text.lower() == "true"
    if kind == "timestamp":
        return _iso(float(text)) if re.fullmatch(r"\d+(\.\d+)?", text) else _iso(text)
    return text


def _from_xml(model: Model, target: str, element: ElementTree.Element, depth: int = 0) -> Any:
    kind = model.kind(target)
    if depth > 64:
        return None
    if kind in ("structure", "union"):
        children: dict[str, list[ElementTree.Element]] = {}
        for child in element:
            children.setdefault(_local(child.tag), []).append(child)
        out: dict[str, Any] = {}
        for key, (member_target, traits) in model.members(target).items():
            wire = str(traits.get("smithy.api#xmlName", key))
            if "smithy.api#xmlAttribute" in traits:
                if wire in element.attrib:
                    out[key] = _xml_scalar(model, member_target, element.attrib[wire])
                continue
            found = children.get(wire)
            if not found:
                continue
            member_kind = model.kind(member_target)
            if "smithy.api#xmlFlattened" in traits and member_kind in ("list", "set"):
                item = model.item(member_target)[0]
                out[key] = [_from_xml(model, item, e, depth + 1) for e in found]
            elif "smithy.api#xmlFlattened" in traits and member_kind == "map":
                out[key] = _xml_map(model, member_target, found, depth)
            else:
                out[key] = _from_xml(model, member_target, found[0], depth + 1)
        return out
    if kind in ("list", "set"):
        item = model.item(target)[0]
        return [_from_xml(model, item, child, depth + 1) for child in element]
    if kind == "map":
        return _xml_map(model, target, list(element), depth)
    if kind == "document":
        return element.text
    return _xml_scalar(model, target, element.text)


def _xml_map(model: Model, target: str, entries: list[ElementTree.Element], depth: int) -> dict[str, Any]:
    shape = model.shape(target)
    key_name = (shape["key"].get("traits") or {}).get("smithy.api#xmlName", "key")
    value_name = (shape["value"].get("traits") or {}).get("smithy.api#xmlName", "value")
    out = {}
    for entry in entries:
        parts = {_local(c.tag): c for c in entry}
        if key_name in parts:
            value = parts.get(value_name)
            out[(parts[key_name].text or "").strip()] = (
                _from_xml(model, shape["value"]["target"], value, depth + 1) if value is not None else None
            )
    return out


def _error_code(response: Any) -> tuple[str, str]:
    """AWS error code and message from any protocol's error body."""
    code = (response.header("x-amzn-errortype") or "").split(":")[0]
    message = ""
    body = response.body or b""
    if body.lstrip().startswith(b"{"):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            code = code or str(data.get("__type") or data.get("code") or data.get("Code") or "")
            message = str(data.get("message") or data.get("Message") or "")
    else:
        text = body.decode(errors="replace")
        found = re.search(r"<Code>([^<]*)</Code>", text)
        code = code or (found.group(1) if found else "")
        found = re.search(r"<Message>([^<]*)</Message>", text)
        message = found.group(1) if found else ""
    return code.rsplit("#", 1)[-1], message


def _throttled(response: Any) -> bool:
    return response.status in (400, 403, 429, 503) and _error_code(response)[0] in THROTTLE_CODES


def _error_detail(response: Any) -> str | None:
    code, message = _error_code(response)
    return f"{code}: {message}".strip(": ") or None


# --- executing --------------------------------------------------------------------------------


def _get(data: Any, dotted: str) -> Any:
    for part in [p for p in dotted.split(".") if p]:
        if not isinstance(data, Mapping) or part not in data:
            return None
        data = data[part]
    return data


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, last = dotted.split(".")
    for part in parents:
        data = data.setdefault(part, {})
    data[last] = value


@dataclass(frozen=True)
class _Call:
    """One signed request, built from an operation and its input."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes


def _request(api: Api, op: Operation, endpoint: str, values: dict[str, Any]) -> _Call:
    model = api.model(op)
    protocol = op.protocol
    base = endpoint.rstrip("/")
    if protocol in ("awsJson1_0", "awsJson1_1"):
        service = api.settings.services[op.service]
        prefix = service.target_prefix or model.service_id.split("#")[-1]
        body = json.dumps(_to_json(model, op.input, values, False), separators=(",", ":")).encode()
        headers = {
            "Content-Type": f"application/x-amz-json-{protocol[-3:].replace('_', '.')}",
            "X-Amz-Target": f"{prefix}.{op.name.split('.', 1)[1]}",
        }
        return _Call("POST", base + "/", headers, body)
    if protocol in ("awsQuery", "ec2Query"):
        form = {"Action": op.name.split(".", 1)[1], "Version": model.version}
        if values:
            _to_form(model, op.input, values, "", form, protocol == "ec2Query")
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8", "Accept": "text/xml"}
        return _Call("POST", base + "/", headers, urlencode(form).encode())
    if protocol == "restJson1":
        members = model.members(op.input)
        path, _, literal = op.uri.partition("?")
        query: list[tuple[str, str]] = [
            tuple(p.split("=", 1)) if "=" in p else (p, "") for p in literal.split("&") if p
        ]  # type: ignore[misc]
        for key, value in values.items():
            target, traits = members[key]
            if "smithy.api#httpLabel" in traits:
                greedy = "{" + key + "+}" in path
                encoded = quote(str(value), safe="/" if greedy else "")
                path = path.replace("{" + key + "+}" if greedy else "{" + key + "}", encoded)
            elif "smithy.api#httpQuery" in traits:
                name = traits["smithy.api#httpQuery"]
                items = value if isinstance(value, list) else [value]
                item_target = model.item(target)[0] if isinstance(value, list) else target
                query += [(name, _form_scalar(model, item_target, v)) for v in items]
        url = base + path + (f"?{urlencode(query)}" if query else "")
        return _Call(op.method, url, {"Accept": "application/json"}, b"")
    raise InvalidConfig(f"{op.protocol} is not supported")


def _response(api: Api, op: Operation, response: Any) -> dict[str, Any]:
    model = api.model(op)
    if op.protocol in ("awsJson1_0", "awsJson1_1", "restJson1"):
        data = response.json() or {}
        if op.protocol == "restJson1":
            members = model.members(op.output)
            payload = next((k for k, (_, t) in members.items() if "smithy.api#httpPayload" in t), None)
            if payload:
                return {payload: _from_json(model, members[payload][0], data, True)}
        return _from_json(model, op.output, data, op.protocol == "restJson1")
    root = ElementTree.fromstring(response.body)  # noqa: S314 - AWS responses; no DTDs or entities are resolved
    if op.protocol == "awsQuery":
        wrapper = next((c for c in root if _local(c.tag) == f"{op.name.split('.', 1)[1]}Result"), None)
        return _from_xml(model, op.output, wrapper) if wrapper is not None else {}
    return _from_xml(model, op.output, root)


def _send(ctx: Any, call: _Call, region: str, signing_name: str) -> Any:
    credentials = ctx.aws_credentials
    signed = sigv4.sign(
        call.method,
        call.url,
        call.headers,
        call.body,
        access_key=credentials["AWS_ACCESS_KEY_ID"],
        secret_key=credentials["AWS_SECRET_ACCESS_KEY"],
        session_token=credentials.get("AWS_SESSION_TOKEN"),
        region=region,
        service=signing_name,
    )
    return ctx.http.request(
        call.method, call.url, headers=signed, content=call.body or None,
        throttled=_throttled, error_detail=_error_detail,
    )  # fmt: skip


def _pages(ctx: Any, api: Api, op: Operation, query: Query, region: str | None) -> Iterator[Any]:
    model = api.model(op)
    service = api.settings.services[op.service]
    endpoint = service.endpoint.replace("{region}", region) if region else service.endpoint
    parts = urlsplit(endpoint)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
        raise InvalidConfig(f"{endpoint} is not an https URL")
    members = model.members(op.input)
    values = {key: _coerce(model, members[key][0], text)[0] for key, text in query.params.items()}
    _, items_path = _items_target(model, op, query.items)
    signing_region = region or service.signing_region
    token, seen = None, set()
    for _ in range(MAX_PAGES):
        page_values = dict(values)
        if token is not None:
            _set(page_values, op.paginated["inputToken"], token)
        output = _response(
            api, op, _send(ctx, _request(api, op, endpoint, page_values), signing_region, model.signing_name)
        )
        if items_path:
            found = _get(output, items_path)
            yield from found if isinstance(found, list) else []
        else:
            yield output
        if not op.paginated.get("outputToken"):
            return
        token = _get(output, op.paginated["outputToken"])
        if not token or token in seen:
            return
        seen.add(token)
    raise IntegrationError(f"{op.name}: more than {MAX_PAGES} pages")


def _project(raw: Any, fields: Mapping[str, str], region: str | None) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key, path in fields.items():
        if path == SELF:
            projected[key] = raw
        elif path == REGION_PATH:
            projected[key] = region
        else:
            projected[key] = _get(raw, path)
    for key in ("id", "name"):
        if projected.get(key) is not None and not isinstance(projected[key], str):
            projected[key] = str(projected[key])
    if region:
        projected["region"] = region
    return projected


def execute(ctx: Any, api: Api, query: Query) -> Iterator[dict[str, Any]]:
    """Run a validated query, region by region, and yield projected items.

    A region that fails is skipped and makes the collection incomplete; if every
    region fails, the first failure is raised. Rate limits always propagate.
    """
    problems, _ = validate(api, query)
    if problems:
        raise InvalidConfig("; ".join(f"{p.pointer}: {p.detail}" for p in problems))
    op = api.operations[query.operation]
    service = api.settings.services[op.service]
    regions: list[str | None] = [None]
    if service.regional:
        listed = _regions(query, ctx.config)
        bad = [r for r in listed if not REGION.match(r)]
        if not listed or bad:
            raise InvalidConfig(
                f"the connection's regions are not region codes: {', '.join(bad) or 'none set'}"
            )
        regions = list(dict.fromkeys(listed))

    def run() -> Iterator[dict[str, Any]]:
        failures: list[tuple[str | None, IntegrationError]] = []
        for region in regions:
            try:
                for raw in _pages(ctx, api, op, query, region):
                    yield _project(raw, query.fields, region)
            except RateLimited:
                raise
            except IntegrationError as exc:
                failures.append((region, exc))
        if failures and len(failures) == len(regions):
            raise failures[0][1]
        if failures:
            raise Incomplete("; ".join(f"{region}: {exc.message}" for region, exc in failures))

    produced: Iterator[dict[str, Any]] = run()
    if query.max_items is not None:
        produced = itertools.islice(produced, query.max_items)
    yield from produced


# --- bundling ---------------------------------------------------------------------------------


def _short_docs(traits: Mapping[str, Any]) -> dict[str, Any]:
    kept = {k: v for k, v in traits.items() if k in KEPT_TRAITS}
    summary = _summary(traits)
    if summary:
        kept["smithy.api#documentation"] = summary
    return kept


def trim(api: Api, service: str) -> dict[str, Any]:
    """A service's model reduced to its queryable operations and the shapes they reach.

    Documentation is cut to one sentence per operation; endpoint rules, examples, and
    errors are dropped. Resources are flattened into the service's operation list.
    """
    model = api.models[service]
    keep_ops = [op.shape_id for op in api.operations.values() if op.service == service and op.readable]
    service_shape = model.shapes[model.service_id]
    shapes: dict[str, Any] = {
        model.service_id: {
            "type": "service",
            **({"version": service_shape["version"]} if "version" in service_shape else {}),
            "operations": [{"target": t} for t in sorted(keep_ops)],
            "traits": {k: v for k, v in (service_shape.get("traits") or {}).items() if k in KEPT_TRAITS},
        }
    }
    pending: list[str] = []
    for shape_id in keep_ops:
        shape = model.shapes[shape_id]
        trimmed = {"type": "operation", "traits": _short_docs(shape.get("traits") or {})}
        for key in ("input", "output"):
            if shape.get(key):
                trimmed[key] = shape[key]
                pending.append(shape[key]["target"])
        shapes[shape_id] = trimmed
    while pending:
        shape_id = pending.pop()
        if shape_id in shapes or shape_id in PRELUDE:
            continue
        shape = model.shapes[shape_id]
        trimmed = {k: v for k, v in shape.items() if k not in ("traits", "members", "member", "key", "value")}
        traits = {k: v for k, v in (shape.get("traits") or {}).items() if k in KEPT_TRAITS}
        if traits:
            trimmed["traits"] = traits
        for key in ("member", "key", "value"):
            if key in shape:
                trimmed[key] = _trim_member(shape[key])
                pending.append(shape[key]["target"])
        if "members" in shape:
            trimmed["members"] = {name: _trim_member(m) for name, m in shape["members"].items()}
            pending += [m["target"] for m in shape["members"].values()]
        shapes[shape_id] = trimmed
    return {"smithy": model.document.get("smithy", "2.0"), "shapes": shapes}


def _trim_member(member: Mapping[str, Any]) -> dict[str, Any]:
    traits = {k: v for k, v in (member.get("traits") or {}).items() if k in KEPT_TRAITS}
    return {"target": member["target"], **({"traits": traits} if traits else {})}


def encode(settings: Settings, models: Mapping[str, Mapping[str, Any]]) -> str:
    """The generated module `pack` writes: settings and the trimmed models, compressed."""
    payload = json.dumps({"settings": settings.to_json(), "models": models}, separators=(",", ":"))
    blob = base64.b64encode(zlib.compress(payload.encode(), 9)).decode()
    lines = [blob[i : i + 100] for i in range(0, len(blob), 100)]
    return (
        '"""Generated by `ogrl-integration smithy build`: trimmed AWS models and query settings."""\n\n'
        "DATA = (\n" + "\n".join(f'    "{line}"' for line in lines) + "\n)\n"
    )


def load_bundled() -> Api | None:
    """The generated module, if the package has one (in memory or on disk)."""
    try:
        module = importlib.import_module(GENERATED_MODULE)
    except ModuleNotFoundError:
        return None
    data = json.loads(zlib.decompress(base64.b64decode(module.DATA)))
    return Api(Settings.from_json(data["settings"]), data["models"])


def load(settings: Settings, read: Callable[[str], bytes]) -> Api:
    """An Api from the manifest's settings, reading each model through `read(path)`."""
    return Api(settings, {name: json.loads(read(s.model)) for name, s in settings.services.items()})
