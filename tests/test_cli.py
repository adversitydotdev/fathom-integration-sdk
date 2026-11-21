"""Scaffold, conformance, and packing."""

import hashlib
import io
import subprocess
import sys
import tarfile
import zipfile

from ogrl_integration import cli


def test_new_package_passes_conformance_and_its_own_tests(tmp_path):
    root = tmp_path / "acme"
    assert cli.main(["new", str(root), "--id", "dev.acme.directory", "--name", "Acme"]) == 0
    assert all(f.ok for f in cli.conformance(root)), [f for f in cli.conformance(root) if not f.ok]
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root / "tests")],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"},
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr


def test_conformance_catches_drift(tmp_path):
    root = tmp_path / "acme"
    cli.main(["new", str(root), "--id", "dev.acme.directory", "--name", "Acme"])
    manifest = root / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace("version: 0.1.0", "version: 0.2.0"))
    failed = {f.what for f in cli.conformance(root) if not f.ok}
    assert failed == {
        "Integration(id, version) equals the manifest",
        "initialize answers protocol, id, and version",
    }


def test_pack_is_deterministic_and_carries_the_sdk(tmp_path):
    root = tmp_path / "acme"
    cli.main(["new", str(root), "--id", "dev.acme.directory", "--name", "Acme"])
    first, digest = cli.pack(root, tmp_path / "a")
    second, again = cli.pack(root, tmp_path / "b")
    assert digest == again == hashlib.sha256(first.read_bytes()).hexdigest()
    with tarfile.open(first) as tar:
        names = tar.getnames()
        assert "tests/test_acme_integration.py" not in names
        assert {"manifest.yaml", "schemas/items.json", "directory_integration.py"} <= set(names)
        wheel = next(n for n in names if n.startswith("wheels/"))
        data = tar.extractfile(wheel).read()  # type: ignore[union-attr]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "ogrl_integration/app.py" in zf.namelist()
