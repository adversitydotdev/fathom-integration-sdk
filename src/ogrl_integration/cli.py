"""`ogrl-integration`: scaffold, run, test, and pack an integration package.

    ogrl-integration new ./acme-directory --id dev.acme.directory --name "Acme Directory"
    ogrl-integration run ./acme-directory collect users@1 --config domain=acme.example --out users.jsonl
    ogrl-integration test ./acme-directory
    ogrl-integration pack ./acme-directory --out dist/

`run`, `test`, and `pack` read manifest.yaml and need the `cli` extra
(`pip install 'ogrl-integration[cli]'`) for YAML and JSON Schema.
"""

import argparse
import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ogrl_integration
from ogrl_integration.app import Integration
from ogrl_integration.testing import Cassette, Harness, HarnessError

INTEGRATION_ID = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
NAME = re.compile(r"^[a-z0-9-]+$")
EXCLUDED_DIRS = frozenset({"tests", "dist", "__pycache__", ".git", ".venv", "wheels"})


def _need(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError:
        sys.exit(f"this command needs {module}: pip install 'ogrl-integration[cli]'")


def load_manifest(root: Path) -> dict[str, Any]:
    yaml = _need("yaml")
    return yaml.safe_load((root / "manifest.yaml").read_text())


def build_openapi(root: Path, manifest: dict[str, Any] | None = None) -> Path | None:
    """Write the generated description module when the manifest declares `openapi`."""
    from ogrl_integration import openapi

    manifest = manifest or load_manifest(root)
    block = manifest.get("openapi")
    if not block:
        return None
    yaml = _need("yaml")
    source = root / block["description"]
    text = source.read_text()
    document = json.loads(text) if source.suffix == ".json" else yaml.safe_load(text)
    settings = openapi.Settings.from_json(block)
    openapi.Description(document)  # fails early on a malformed description
    target = root / f"{openapi.GENERATED_MODULE}.py"
    target.write_text(openapi.encode(settings, document))
    return target


def smithy_api(root: Path, manifest: dict[str, Any] | None = None) -> Any:
    """The manifest's `smithy` block with its models, untrimmed; None without one."""
    from ogrl_integration import smithy

    block = (manifest or load_manifest(root)).get("smithy")
    if not block:
        return None
    settings = smithy.Settings.from_json(block)
    return smithy.load(settings, lambda path: (root / path).read_bytes())


def trimmed_models(api: Any) -> dict[str, dict[str, Any]]:
    from ogrl_integration import smithy

    return {name: smithy.trim(api, name) for name in api.models}


def build_smithy(root: Path, manifest: dict[str, Any] | None = None) -> Path | None:
    """Write the generated module of trimmed models when the manifest declares `smithy`."""
    from ogrl_integration import smithy

    api = smithy_api(root, manifest)
    if api is None:
        return None
    target = root / f"{smithy.GENERATED_MODULE}.py"
    target.write_text(smithy.encode(api.settings, trimmed_models(api)))
    return target


GENERATED = ("_ogrl_openapi", "_ogrl_smithy")


def load_app(root: Path, manifest: dict[str, Any]) -> Integration:
    """Import the package's `Integration` from its `module:attr.run` entrypoint."""
    module_name, _, attr = manifest["runtime"]["entrypoint"].partition(":")
    build_openapi(root, manifest)
    build_smithy(root, manifest)
    sys.path.insert(0, str(root))
    try:
        sys.modules.pop(module_name, None)
        for generated in GENERATED:
            sys.modules.pop(generated, None)
        module = importlib.import_module(module_name)
        for generated in GENERATED:
            if (root / f"{generated}.py").is_file():
                importlib.import_module(generated)  # while the package is on sys.path
    finally:
        sys.path.remove(str(root))
    target: Any = module
    for part in attr.split(".")[:-1]:  # `app.run` -> app
        target = getattr(target, part)
    if not isinstance(target, Integration):
        sys.exit(f"{manifest['runtime']['entrypoint']} does not point at Integration.run")
    return target


def _pairs(values: list[str] | None) -> dict[str, str]:
    out = {}
    for value in values or []:
        key, sep, val = value.partition("=")
        if not sep:
            sys.exit(f"expected key=value, got {value!r}")
        out[key] = val
    return out


def _snapshot_items(text: str) -> list[dict[str, Any]]:
    """A snapshot document (`{"items": [...]}`), or JSON Lines as `run collect --out` writes."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("items"), list):
        return document["items"]
    if isinstance(document, list):
        return document
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _reference(text: str) -> tuple[str, int]:
    name, _, major = text.partition("@")
    if not NAME.match(name) or not major.isdigit():
        sys.exit(f"expected name@major, such as users@1; got {text!r}")
    return name, int(major)


def _secrets(manifest: dict[str, Any]) -> dict[str, str]:
    """Credentials for a local run come from the environment, by the names the manifest declares."""
    names = {"OGRL_ACCESS_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    for auth in manifest.get("auth", []):
        names |= {f["name"] for f in auth.get("fields", [])}
    return {k: v for k, v in os.environ.items() if k in names}


# --- run ---------------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.package)
    manifest = load_manifest(root)
    app = load_app(root, manifest)
    if args.record:
        app.transport = Cassette(args.record, mode="record")
    elif args.replay:
        app.transport = Cassette(args.replay, mode="replay")
    harness = Harness(app)
    config = _pairs(args.config)
    try:
        if args.operation == "validate":
            print(json.dumps(harness.validate(config, _secrets(manifest)), indent=2))
        elif args.operation == "collect":
            name, major = _reference(args.target or "")
            query = None
            if args.query:
                definition = _need("yaml").safe_load(Path(args.query).read_text())
                keys = ("operation", "params", "items", "fields", "regions")
                query = {k: definition[k] for k in keys if k in definition}
            result = harness.collect(name, major, config, _secrets(manifest), query=query)
            lines = "".join(json.dumps(item) + "\n" for item in result.items)
            if args.out:
                Path(args.out).write_text(lines)
            else:
                sys.stdout.write(lines)
            state = "complete" if result.complete else f"incomplete: {result.reason}"
            print(f"{len(result.items)} items, {state}", file=sys.stderr)
        else:
            name, major = _reference(args.target or "")
            snapshots = {}
            for source, path in _pairs(args.snapshot).items():
                snapshots[source] = _snapshot_items(Path(path).read_text())
            print(json.dumps(harness.check(name, major, snapshots, _pairs(args.arg)), indent=2))
    except HarnessError as exc:
        print(f"{exc.kind}: {exc}", file=sys.stderr)
        return 1
    return 0


# --- test (conformance) ------------------------------------------------------------------------


@dataclass
class Finding:
    ok: bool
    what: str
    detail: str = ""


def _process(root: Path, manifest: dict[str, Any], lines: list[dict[str, Any]], *, wait: float = 10):
    module, _, function = manifest["runtime"]["entrypoint"].partition(":")
    root = root.resolve()  # the process runs from /, so a relative path would not resolve
    paths = [str(root), *map(str, sorted((root / "wheels").glob("*.whl")))]
    # The SDK this command runs from stands in for a packed wheel during development.
    paths.append(str(Path(ogrl_integration.__file__).resolve().parents[1]))
    # As the OGRL host runs it: no writable working directory, no HOME, no bytecode writes.
    env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.pathsep.join(paths)}
    process = subprocess.Popen(
        [sys.executable, "-c", f"import {module} as m; m.{function or 'main'}()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd="/",
        text=True,
    )
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate("".join(json.dumps(m) + "\n" for m in lines), timeout=wait)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return None, stdout, stderr, time.monotonic() - started
    return process.returncode, stdout, stderr, time.monotonic() - started


def conformance(root: Path) -> list[Finding]:
    jsonschema = _need("jsonschema")
    found: list[Finding] = []

    def check(ok: bool, what: str, detail: str = "") -> bool:
        found.append(Finding(bool(ok), what, detail))
        return bool(ok)

    try:
        manifest = load_manifest(root)
    except Exception as exc:
        check(False, "manifest.yaml loads", str(exc))
        return found
    check(bool(INTEGRATION_ID.match(str(manifest.get("id", "")))), "id is a reverse domain name")
    check(bool(SEMVER.match(str(manifest.get("version", "")))), "version is semver")
    check(manifest.get("protocol") == 1, "protocol is 1")
    check(manifest.get("runtime", {}).get("kind") == "python", "runtime is python")
    check(bool(manifest.get("auth")), "at least one auth kind")

    resources = {(r["name"], r["version"]) for r in manifest.get("resources", [])}
    checks = {(c["name"], c["version"]) for c in manifest.get("checks", [])}
    for resource in manifest.get("resources", []):
        where = f"resource {resource['name']}@{resource['version']}"
        try:
            schema = json.loads((root / resource["schema"]).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            check(schema.get("type") == "object", f"{where} schema is a valid object schema")
            props = schema.get("properties", {})
            check(
                "id" in props and "name" in props,
                f"{where} items declare id and name",
                "the rule runner names failing items by them (RFC 0001 7.6)",
            )
        except Exception as exc:
            check(False, f"{where} schema is a valid object schema", str(exc))
    names = {name for name, _ in resources}
    for c in manifest.get("checks", []):
        check(set(c.get("sources", [])) <= names, f"check {c['name']} reads declared resources")
    for t in manifest.get("templates", []):
        run = t.get("test", {}).get("run", {})
        refs = [run.get("source"), run.get("check"), *(run.get("sources") or [])]
        ours = all(r.split("/", 1)[0] == manifest["id"] for r in refs if r)
        check(ours, f"template {t['name']} reads only this integration")

    try:
        app = load_app(root, manifest)
    except Exception as exc:
        check(False, "the entrypoint imports", str(exc))
        return found
    check(
        (app.id, app.version) == (manifest["id"], manifest["version"]),
        "Integration(id, version) equals the manifest",
        f"{app.id} {app.version}",
    )
    check(
        set(app.resources) == resources,
        "every manifest resource is implemented, and no others",
        f"code {sorted(app.resources)} manifest {sorted(resources)}",
    )
    check(
        set(app.checks) == checks,
        "every manifest check is implemented, and no others",
        f"code {sorted(app.checks)} manifest {sorted(checks)}",
    )

    hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocol": 1}}
    unknown = {"jsonrpc": "2.0", "id": 2, "method": "no.such.method", "params": {}}
    code, stdout, stderr, elapsed = _process(root, manifest, [hello, unknown])
    check(code is not None and elapsed < 10, "exits within 10 s of stdin closing", stderr[-500:])
    try:
        messages = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        check(True, "stdout carries only JSON lines")
    except json.JSONDecodeError:
        check(False, "stdout carries only JSON lines", stdout[:500])
        return found
    answers = {m.get("id"): m for m in messages if "id" in m}
    init = answers.get(1, {}).get("result", {})
    check(
        init == {"protocol": 1, "id": manifest["id"], "version": manifest["version"]},
        "initialize answers protocol, id, and version",
        json.dumps(init),
    )
    error = answers.get(2, {}).get("error", {})
    check(bool((error.get("data") or {}).get("kind")), "unknown methods answer an error with a kind")

    harness = Harness(app)
    for c in manifest.get("checks", []):
        empty = {f"{manifest['id']}/{s}@{_major(manifest, s)}": [] for s in c.get("sources", [])}
        try:
            out = harness.check(c["name"], c["version"], empty)
            check(
                out.get("outcome") in ("pass", "fail", "error"),
                f"check {c['name']}@{c['version']} survives empty snapshots",
            )
        except Exception as exc:
            check(False, f"check {c['name']}@{c['version']} survives empty snapshots", str(exc))
    return found


def _major(manifest: dict[str, Any], resource: str) -> int:
    return max(r["version"] for r in manifest["resources"] if r["name"] == resource)


def cmd_test(args: argparse.Namespace) -> int:
    findings = conformance(Path(args.package))
    for f in findings:
        print(
            f"{'PASS' if f.ok else 'FAIL'}  {f.what}"
            + (f"\n      {f.detail}" if f.detail and not f.ok else "")
        )
    failed = sum(not f.ok for f in findings)
    print(f"\n{len(findings) - failed} passed, {failed} failed")
    return 1 if failed else 0


# --- pack --------------------------------------------------------------------------------------


def sdk_wheel() -> tuple[str, bytes]:
    """A pure-Python wheel of this SDK, built from its installed files (zip-importable)."""
    source = Path(ogrl_integration.__file__).resolve().parent
    version = ogrl_integration.__version__
    dist = f"ogrl_integration-{version}.dist-info"
    files = sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    buffer = io.BytesIO()
    records = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as wheel:

        def add(name: str, data: bytes) -> None:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            wheel.writestr(info, data)
            digest = hashlib.sha256(data).digest()
            import base64

            records.append(
                f"{name},sha256={base64.urlsafe_b64encode(digest).rstrip(b'=').decode()},{len(data)}"
            )

        for path in files:
            add(f"ogrl_integration/{path.relative_to(source).as_posix()}", path.read_bytes())
        add(
            f"{dist}/METADATA",
            f"Metadata-Version: 2.1\nName: ogrl-integration\nVersion: {version}\n".encode(),
        )
        add(
            f"{dist}/WHEEL",
            b"Wheel-Version: 1.0\nGenerator: ogrl-integration\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        record = "\n".join([*records, f"{dist}/RECORD,,"]) + "\n"
        info = zipfile.ZipInfo(f"{dist}/RECORD", date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        wheel.writestr(info, record)
    return f"ogrl_integration-{version}-py3-none-any.whl", buffer.getvalue()


def _package_files(root: Path) -> Iterator[Path]:
    manifest = load_manifest(root)
    # The host validates query sources against the raw description (it never imports package code).
    description = (manifest.get("openapi") or {}).get("description")
    models = {s["model"] for s in ((manifest.get("smithy") or {}).get("services") or {}).values()}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or EXCLUDED_DIRS.intersection(relative.parts[:-1]):
            continue
        if (
            relative.parts[0] in ("manifest.yaml", "schemas")
            or path.suffix == ".py"
            or relative.as_posix() == description
            or relative.as_posix() in models
        ):
            yield path


def pack(root: Path, out_dir: Path) -> tuple[Path, str]:
    manifest = load_manifest(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{manifest['id']}-{manifest['version']}.tar.gz"

    def entry(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size, info.mtime, info.mode = len(data), 0, 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        tar.addfile(info, io.BytesIO(data))

    # Smithy models ship trimmed to their queryable operations (RFC 0007 section 2).
    api = smithy_api(root, manifest)
    trimmed: dict[str, bytes] = {}
    if api is not None:
        build_smithy(root, manifest)
        for name, document in trimmed_models(api).items():
            trimmed[api.settings.services[name].model] = json.dumps(
                document, separators=(",", ":"), sort_keys=True
            ).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in _package_files(root):
            relative = path.relative_to(root).as_posix()
            entry(tar, relative, trimmed.get(relative) or path.read_bytes())
        for wheel in sorted((root / "wheels").glob("*.whl")):
            entry(tar, f"wheels/{wheel.name}", wheel.read_bytes())
        name, data = sdk_wheel()
        entry(tar, f"wheels/{name}", data)
    import gzip

    archive.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


def cmd_pack(args: argparse.Namespace) -> int:
    root = Path(args.package)
    findings = conformance(root)
    failed = [f for f in findings if not f.ok]
    if failed and not args.force:
        for f in failed:
            print(f"FAIL  {f.what}", file=sys.stderr)
        print("conformance failed; fix it or pass --force", file=sys.stderr)
        return 1
    archive, digest = pack(root, Path(args.out))
    print(f"{archive}\nsha256 {digest}")
    return 0


# --- new ---------------------------------------------------------------------------------------

MODULE = '''"""{name}: an OGRL integration (RFC 0005)."""

from ogrl_integration import Integration, Subject, fail, pass_

app = Integration("{id}", "0.1.0")


@app.validate
def validate(ctx):
    me = ctx.http.get(f"{{ctx.config['base_url']}}/me", headers=_auth(ctx)).json()
    return ctx.identity(f"{{ctx.config['base_url']}} as {{me['name']}}", scopes=me.get("scopes", []))


@app.resource("items", version=1)
def items(ctx):
    pages = ctx.http.paginate(
        f"{{ctx.config['base_url']}}/items",
        headers=_auth(ctx),
        items=lambda r: r.json()["items"],
        next_url=lambda r: r.json().get("next"),
    )
    for item in pages:
        yield {{"id": item["id"], "name": item["name"], "enabled": item.get("enabled", False)}}


@app.check("all-enabled", version=1)
def all_enabled(ctx, items):
    off = [i for i in items if not i.get("enabled")]
    if off:
        subjects = [Subject.resource(i["name"], i["id"]) for i in off]
        return fail(f"{{len(off)}} of {{len(items)}} items are disabled", subjects)
    return pass_(f"{{len(items)}} items are enabled")


def _auth(ctx):
    return {{"Authorization": f"Bearer {{ctx.secret('{env}')}}"}}
'''


def cmd_new(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    if root.exists() and any(root.iterdir()):
        sys.exit(f"{root} is not empty")
    if not INTEGRATION_ID.match(args.id):
        sys.exit("--id is a reverse domain name, such as dev.acme.directory")
    module = re.sub(r"[^a-z0-9_]", "_", args.id.rsplit(".", 1)[-1]) + "_integration"
    env = re.sub(r"[^A-Z0-9]", "_", args.id.rsplit(".", 1)[-1].upper()) + "_TOKEN"
    (root / "schemas").mkdir(parents=True)
    (root / "tests" / "cassettes").mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        f"""id: {args.id}
name: {args.name}
version: 0.1.0
protocol: 1
publisher: {args.id.split(".")[1] if args.id.count(".") >= 1 else args.id}
description: Items from {args.name}.
runtime:
  kind: python
  entrypoint: {module}:app.run
egress: []
config:
  - name: base_url
    label: API base URL
    pattern: "^https://"
auth:
  - kind: fields
    label: API token
    fields:
      - name: {env}
        label: Read-only API token
resources:
  - name: items
    version: 1
    schema: schemas/items.json
checks:
  - name: all-enabled
    version: 1
    sources: [items]
templates: []
"""
    )
    (root / "schemas" / "items.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["id", "name"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
            },
            indent=2,
        )
        + "\n"
    )
    code = MODULE.format(name=args.name, id=args.id, env=env)
    (root / f"{module}.py").write_text(code)
    base = "https://api.example.test"
    cassette = {
        "interactions": [
            {
                "method": "GET",
                "url": f"{base}/items",
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(
                    {"items": [{"id": "1", "name": "alpha", "enabled": True}], "next": f"{base}/items?page=2"}
                ),
            },
            {
                "method": "GET",
                "url": f"{base}/items?page=2",
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"items": [{"id": "2", "name": "beta", "enabled": False}], "next": None}),
            },
        ]
    }
    (root / "tests" / "cassettes" / "items.json").write_text(json.dumps(cassette, indent=2) + "\n")
    (root / "tests" / f"test_{module}.py").write_text(
        f'''from pathlib import Path

from ogrl_integration.testing import Cassette, Harness
from {module} import app

CASSETTES = Path(__file__).parent / "cassettes"
CONFIG = {{"base_url": "{base}"}}
SECRETS = {{"{env}": "test-token"}}


def test_collects_every_page():
    app.transport = Cassette(CASSETTES / "items.json")
    result = Harness(app).collect("items", 1, CONFIG, SECRETS)
    assert result.complete
    assert [i["id"] for i in result.items] == ["1", "2"]


def test_check_names_disabled_items():
    beta = {{"id": "2", "name": "beta", "enabled": False}}
    out = Harness(app).check("all-enabled", 1, {{"{args.id}/items@1": [beta]}})
    assert out["outcome"] == "fail"
    assert out["subjects"] == [{{"kind": "resource", "name": "beta", "external_id": "2"}}]
'''
    )
    print(f"created {root}. Next: ogrl-integration test {root}")
    return 0


# --- openapi ---------------------------------------------------------------------------------


def cmd_openapi_build(args: argparse.Namespace) -> int:
    target = build_openapi(Path(args.package))
    if target is None:
        sys.exit("the manifest declares no openapi block")
    print(f"{target} ({target.stat().st_size} bytes)")
    return 0


def cmd_openapi_search(args: argparse.Namespace) -> int:
    from ogrl_integration import openapi

    root = Path(args.package).resolve()
    build_openapi(root)
    sys.path.insert(0, str(root))
    sys.modules.pop(openapi.GENERATED_MODULE, None)
    bundled = openapi.load_bundled()
    if bundled is None:
        sys.exit("the manifest declares no openapi block")
    settings, description = bundled
    words = args.text.lower().split()
    for op in description.operations.values():
        if op.method != "GET" and op.id not in settings.read_operations:
            continue
        if all(w in f"{op.id} {op.path} {op.summary}".lower() for w in words):
            print(f"{op.id:40} {op.method} {op.path}  {op.summary[:70]}")
    return 0


# --- smithy ------------------------------------------------------------------------------------


def _smithy_or_exit(root: Path) -> Any:
    api = smithy_api(root)
    if api is None:
        sys.exit("the manifest declares no smithy block")
    return api


def cmd_smithy_build(args: argparse.Namespace) -> int:
    target = build_smithy(Path(args.package))
    if target is None:
        sys.exit("the manifest declares no smithy block")
    print(f"{target} ({target.stat().st_size} bytes)")
    return 0


def cmd_smithy_search(args: argparse.Namespace) -> int:
    api = _smithy_or_exit(Path(args.package))
    for op in api.search(args.text, limit=args.limit):
        print(f"{op.name:55} {op.protocol:11} {op.summary[:70]}")
    return 0


def cmd_smithy_describe(args: argparse.Namespace) -> int:
    api = _smithy_or_exit(Path(args.package))
    if args.operation not in api.operations:
        sys.exit(f"no operation {args.operation}")
    print(json.dumps(api.describe(args.operation), indent=2))
    return 0


def cmd_smithy_validate(args: argparse.Namespace) -> int:
    from ogrl_integration import smithy

    api = _smithy_or_exit(Path(args.package))
    definition = _need("yaml").safe_load(Path(args.query).read_text())
    problems, schema = smithy.validate(api, smithy.Query.from_json(definition), name=definition.get("name"))
    for problem in problems:
        print(f"{problem.pointer}: {problem.detail}", file=sys.stderr)
    if not problems:
        print(json.dumps(schema, indent=2))
    return 1 if problems else 0


# --- entry -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ogrl-integration", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="scaffold a package")
    new.add_argument("directory")
    new.add_argument("--id", required=True)
    new.add_argument("--name", required=True)
    new.set_defaults(func=cmd_new)

    run = sub.add_parser("run", help="run one operation locally; secrets come from the environment")
    run.add_argument("package")
    run.add_argument("operation", choices=["validate", "collect", "check"])
    run.add_argument("target", nargs="?", help="name@major for collect and check")
    run.add_argument("--config", action="append", metavar="KEY=VALUE")
    run.add_argument("--arg", action="append", metavar="KEY=VALUE", help="check arguments")
    run.add_argument("--snapshot", action="append", metavar="SOURCE=PATH", help="check input")
    run.add_argument("--out", help="write collected items as JSON Lines here")
    run.add_argument(
        "--query", metavar="FILE", help="collect a query source defined in a YAML file (RFC 0006)"
    )
    tape = run.add_mutually_exclusive_group()
    tape.add_argument("--record", metavar="CASSETTE", help="record vendor HTTP, credentials scrubbed")
    tape.add_argument("--replay", metavar="CASSETTE")
    run.set_defaults(func=cmd_run)

    api = sub.add_parser("openapi", help="query sources: build the bundled description, or search it")
    api_sub = api.add_subparsers(dest="action", required=True)
    api_build = api_sub.add_parser("build", help="write _ogrl_openapi.py from the manifest's openapi block")
    api_build.add_argument("package")
    api_build.set_defaults(func=cmd_openapi_build)
    api_search = api_sub.add_parser("search", help="find read operations by id, path, or summary")
    api_search.add_argument("package")
    api_search.add_argument("text")
    api_search.set_defaults(func=cmd_openapi_search)

    sm = sub.add_parser("smithy", help="query sources over AWS Smithy models (RFC 0007)")
    sm_sub = sm.add_subparsers(dest="action", required=True)
    sm_build = sm_sub.add_parser("build", help="write _ogrl_smithy.py: the trimmed models")
    sm_build.add_argument("package")
    sm_build.set_defaults(func=cmd_smithy_build)
    sm_search = sm_sub.add_parser("search", help="find queryable operations by name or summary")
    sm_search.add_argument("package")
    sm_search.add_argument("text")
    sm_search.add_argument("--limit", type=int, default=50)
    sm_search.set_defaults(func=cmd_smithy_search)
    sm_describe = sm_sub.add_parser(
        "describe", help="an operation's parameters, item fields, and read status"
    )
    sm_describe.add_argument("package")
    sm_describe.add_argument("operation", help="<service>.<Operation>, such as ec2.DescribeSecurityGroups")
    sm_describe.set_defaults(func=cmd_smithy_describe)
    sm_validate = sm_sub.add_parser("validate", help="statically validate a query definition (YAML)")
    sm_validate.add_argument("package")
    sm_validate.add_argument("query")
    sm_validate.set_defaults(func=cmd_smithy_validate)

    test = sub.add_parser("test", help="run the conformance suite")
    test.add_argument("package")
    test.set_defaults(func=cmd_test)

    pk = sub.add_parser("pack", help="build a deterministic .tar.gz with the SDK wheel inside")
    pk.add_argument("package")
    pk.add_argument("--out", default="dist")
    pk.add_argument("--force", action="store_true", help="pack even when conformance fails")
    pk.set_defaults(func=cmd_pack)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
