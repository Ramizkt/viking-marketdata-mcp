from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from scripts.check_runtime_lock import expected_packages, verify

ROOT = Path(__file__).resolve().parent.parent
LOCK = b"fixture lockfile"
PROJECT = {"name": "viking-marketdata-mcp", "version": "0.1.0"}
REQUIREMENTS = 'mcp==1.28.1\ncolorama==0.4.6 ; sys_platform == "win32"\n'
INVENTORY = {
    "packages": {"mcp": "1.28.1", "viking-marketdata-mcp": "0.1.0"},
    "editable": [],
    "prefix": "/app/.venv",
    "base_prefix": "/usr/local",
    "app_location": "/app/.venv/lib/python3.12/site-packages/app/__init__.py",
    "lock_sha256": hashlib.sha256(LOCK).hexdigest(),
    "marker_environment": {"python_version": "3.12", "sys_platform": "linux"},
}


def test_inventory_accepts_exact_pins_and_evaluates_platform_markers():
    assert verify(INVENTORY, REQUIREMENTS, PROJECT, LOCK) == INVENTORY["packages"]


@pytest.mark.parametrize("change", ["version", "missing", "extra", "lock", "editable", "source", "global"])
def test_inventory_rejects_drift(change):
    inventory = copy.deepcopy(INVENTORY)
    if change == "version":
        inventory["packages"]["mcp"] = "1.30.0"
    elif change == "missing":
        inventory["packages"].pop("mcp")
    elif change == "extra":
        inventory["packages"]["pytest"] = "8.4.2"
    elif change == "lock":
        inventory["lock_sha256"] = "wrong"
    elif change == "editable":
        inventory["editable"] = ["viking-marketdata-mcp"]
    elif change == "source":
        inventory["app_location"] = "/checks/app/__init__.py"
    else:
        inventory["prefix"] = inventory["base_prefix"]
    with pytest.raises(ValueError):
        verify(inventory, REQUIREMENTS, PROJECT, LOCK)


@pytest.mark.parametrize("text", ["mcp>=1.27", "mcp==1.*", "", "mcp==1.28.1\nmcp==1.30.0"])
def test_export_verifier_rejects_unpinned_empty_or_conflicting_dependencies(text):
    with pytest.raises(ValueError):
        expected_packages(text, INVENTORY["marker_environment"])


def test_docker_and_ci_keep_same_uv_version_and_locked_install():
    docker = (ROOT / "Dockerfile").read_text()
    ci = (ROOT / ".github/workflows/python-tests.yml").read_text()
    assert "uv:0.12.17" in docker and "version: '0.12.17'" in ci
    assert "COPY pyproject.toml uv.lock README.md ./" in docker
    assert "uv sync --locked --no-dev --no-editable" in docker
    assert "pip install" not in docker
    runtime = docker.split("FROM python-base AS runtime", 1)[1]
    assert "COPY --from=builder /app/.venv /app/.venv" in runtime
    assert "uv sync" not in runtime and "COPY app" not in runtime
    assert "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}" in runtime


def test_container_test_image_does_not_shadow_installed_application():
    docker_ci = (ROOT / "Dockerfile.ci").read_text()
    assert "--only-group dev --no-install-project --inexact" in docker_ci
    assert "COPY app" not in docker_ci and "COPY . " not in docker_ci
    assert "COPY tests ./tests" in docker_ci
