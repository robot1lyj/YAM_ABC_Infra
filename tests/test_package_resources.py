"""Check resources from a built wheel, without dependency resolution or installation."""

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


def test_wheel_contains_runtime_static_and_schema_resources(tmp_path):
    pytest.importorskip("setuptools", reason="run artifact checks with the declared build backend")
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(root / name, project / name)
    shutil.copytree(
        root / "yam_abc_reproduce", project / "yam_abc_reproduce",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # Call the backend directly: no pip/uv, build isolation, network, or robot
    # dependencies. A clean copy also excludes stale egg-info resource lists.
    result = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
        cwd=project, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list((project / "dist").glob("*.whl"))
    assert len(wheels) == 1
    expected = [
        *[f"{package}/static/{name}" for package in ("gui", "hil", "dataset_workbench")
          for name in ("app.js", "index.html", "style.css")],
        "hil/static/console.css",
        "data/formats/abc_schemas.binpb",
    ]
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheels[0]) as wheel:
        for resource in expected:
            assert wheel.read(f"yam_abc_reproduce/{resource}") == (
                root / "yam_abc_reproduce" / resource
            ).read_bytes()
        wheel.extractall(installed)
    # Resolve resources with the extracted wheel as the only project location.
    result = subprocess.run(
        [
            sys.executable, "-I", "-c",
            "import importlib.resources, json, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "root = importlib.resources.files('yam_abc_reproduce'); "
            "assert all(root.joinpath(p).is_file() for p in json.loads(sys.argv[2]))",
            str(installed), json.dumps(expected),
        ],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
