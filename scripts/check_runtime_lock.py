"""Capture with stdlib inside an image; verify with locked dev tooling outside it."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def capture(lock_path: Path) -> dict[str, Any]:
    import app

    packages: dict[str, str] = {}
    editable: list[str] = []
    for dist in importlib.metadata.distributions():
        name = normalized(dist.metadata["Name"])
        if name in packages:
            raise ValueError(f"Duplicate installed distribution: {name}")
        packages[name] = dist.version
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            editable.append(name)
    return {
        "packages": dict(sorted(packages.items())),
        "editable": editable,
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "app_location": str(Path(app.__file__).resolve()),
        "marker_environment": {
            "implementation_name": sys.implementation.name,
            "implementation_version": platform.python_version(),
            "os_name": os.name,
            "platform_machine": platform.machine(),
            "platform_python_implementation": platform.python_implementation(),
            "platform_release": platform.release(),
            "platform_system": platform.system(),
            "platform_version": platform.version(),
            "python_full_version": platform.python_version(),
            "python_version": ".".join(platform.python_version_tuple()[:2]),
            "sys_platform": sys.platform,
        },
    }


def expected_packages(requirements: str, environment: dict[str, str]) -> dict[str, str]:
    # packaging is a locked dev dependency. It is not installed in the production image.
    from packaging.requirements import Requirement

    result: dict[str, str] = {}
    for line in requirements.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        pins = list(requirement.specifier)
        if requirement.url or len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise ValueError(f"Expected an exact registry pin: {line}")
        name, version = normalized(requirement.name), pins[0].version
        if name in result and result[name] != version:
            raise ValueError(f"Conflicting active pins for {name}")
        result[name] = version
    if not result:
        raise ValueError("Empty resolved dependency inventory")
    return result


def verify(
    inventory: dict[str, Any], requirements: str, project: dict[str, Any], lock_bytes: bytes,
) -> dict[str, str]:
    environment = inventory["marker_environment"]
    if environment["python_version"] != "3.12" or environment["sys_platform"] != "linux":
        raise ValueError("Production image must use Linux/Python 3.12")
    if inventory["lock_sha256"] != hashlib.sha256(lock_bytes).hexdigest():
        raise ValueError("Image uv.lock differs from the checked-out commit")
    prefix = Path(inventory["prefix"])
    if inventory["prefix"] == inventory["base_prefix"]:
        raise ValueError("Image is not using the isolated runtime virtual environment")
    if not Path(inventory["app_location"]).is_relative_to(prefix):
        raise ValueError("Tests/runtime imported app from source instead of the installed wheel")
    if inventory["editable"]:
        raise ValueError("Editable installs are not allowed in the image")
    expected = expected_packages(requirements, environment)
    expected[normalized(project["name"])] = project["version"]
    actual = inventory["packages"]
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    different = {name: {"expected": expected[name], "actual": actual[name]}
                 for name in expected.keys() & actual.keys() if expected[name] != actual[name]}
    if missing or extra or different:
        raise ValueError(json.dumps({"missing": missing, "extra": extra, "different": different}, sort_keys=True))
    return dict(sorted(expected.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_args = commands.add_parser("capture")
    capture_args.add_argument("--lock", type=Path, default=Path("/app/uv.lock"))
    verify_args = commands.add_parser("verify")
    verify_args.add_argument("--inventory", type=Path, required=True)
    verify_args.add_argument("--requirements", type=Path, required=True)
    verify_args.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    verify_args.add_argument("--lock", type=Path, default=Path("uv.lock"))
    args = parser.parse_args()
    if args.command == "capture":
        print(json.dumps(capture(args.lock), indent=2, sort_keys=True))
    else:
        packages = verify(
            json.loads(args.inventory.read_text()), args.requirements.read_text(),
            tomllib.loads(args.project.read_text())["project"], args.lock.read_bytes(),
        )
        print(f"Verified {len(packages)} exact package versions and uv.lock SHA-256.")
        for name, version in packages.items():
            print(f"{name}=={version}")


if __name__ == "__main__":
    main()
