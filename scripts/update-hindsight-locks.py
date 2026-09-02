#!/usr/bin/env python3
"""Refresh coordinated Hindsight server locks for Studio upgrades.

The updater resolves API and Control Plane locks in a temporary copy first,
takes the existing encrypted pre-upgrade backup, then publishes the four lock
artifacts together. Hindsight itself applies bundled database migrations when
the rebuilt API starts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

API_PACKAGE = "hindsight-api"
CONTROL_PLANE_PACKAGE = "@vectorize-io/hindsight-control-plane"
PYPI_URL = "https://pypi.org/pypi/hindsight-api/json"
NPM_URL = "https://registry.npmjs.org/@vectorize-io/hindsight-control-plane/latest"
USER_AGENT = "nix-configs-hindsight-updater/1.0"


def http_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def latest_api_version() -> str:
    return str(http_json(PYPI_URL)["info"]["version"])


def latest_control_plane_version() -> str:
    return str(http_json(NPM_URL)["version"])


def api_pin(pyproject: Path) -> str:
    match = re.search(r'hindsight-api==([^"\s]+)', pyproject.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError(f"Hindsight API pin not found in {pyproject}")
    return match.group(1)


def rewrite_api_pin(pyproject: Path, version: str) -> None:
    text = pyproject.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'hindsight-api==[^"\s]+',
        f"hindsight-api=={version}",
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"expected one Hindsight API pin in {pyproject}, found {count}")
    pyproject.write_text(updated, encoding="utf-8")


def control_plane_pin(package_json: Path) -> str:
    package = json.loads(package_json.read_text(encoding="utf-8"))
    return str(package["dependencies"][CONTROL_PLANE_PACKAGE])


def rewrite_control_plane_pin(package_json: Path, version: str) -> None:
    package = json.loads(package_json.read_text(encoding="utf-8"))
    package["dependencies"][CONTROL_PLANE_PACKAGE] = version
    package_json.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")


def locked_api_version(uv_lock: Path) -> str:
    match = re.search(
        r'\[\[package\]\]\nname = "hindsight-api"\nversion = "([^"]+)"',
        uv_lock.read_text(encoding="utf-8"),
    )
    if not match:
        raise RuntimeError(f"resolved Hindsight API version not found in {uv_lock}")
    return match.group(1)


def locked_control_plane_version(package_lock: Path) -> str:
    lock = json.loads(package_lock.read_text(encoding="utf-8"))
    return str(lock["packages"][f"node_modules/{CONTROL_PLANE_PACKAGE}"]["version"])


def publish_file(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.upgrade-tmp")
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, destination.stat().st_mode & 0o777)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=default_root)
    parser.add_argument("--api-version")
    parser.add_argument("--control-plane-version")
    parser.add_argument("--uv-bin", default=shutil.which("uv") or "uv")
    parser.add_argument("--npm-bin", default=shutil.which("npm") or "npm")
    parser.add_argument(
        "--backup-bin",
        default=shutil.which("hindsight-bryan-backup-now") or "hindsight-bryan-backup-now",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    env_dir = repo_root / "modules/services/hindsight-env"
    pyproject = env_dir / "pyproject.toml"
    uv_lock = env_dir / "uv.lock"
    control_plane_dir = env_dir / "control-plane"
    package_json = control_plane_dir / "package.json"
    package_lock = control_plane_dir / "package-lock.json"

    target_api = args.api_version or latest_api_version()
    target_control_plane = args.control_plane_version or latest_control_plane_version()
    if target_api != target_control_plane:
        print(
            "Hindsight release is not coordinated yet; "
            f"API={target_api}, Control Plane={target_control_plane}. Leaving locks unchanged."
        )
        return 0

    current_api = api_pin(pyproject)
    current_control_plane = control_plane_pin(package_json)
    if current_api == target_api and current_control_plane == target_control_plane:
        print(f"Hindsight server already current at {target_api}.")
        return 0

    with tempfile.TemporaryDirectory(prefix="hindsight-lock-update-") as directory:
        candidate_env = Path(directory) / "hindsight-env"
        shutil.copytree(env_dir, candidate_env)
        candidate_pyproject = candidate_env / "pyproject.toml"
        candidate_uv_lock = candidate_env / "uv.lock"
        candidate_control_plane = candidate_env / "control-plane"
        candidate_package_json = candidate_control_plane / "package.json"
        candidate_package_lock = candidate_control_plane / "package-lock.json"

        rewrite_api_pin(candidate_pyproject, target_api)
        rewrite_control_plane_pin(candidate_package_json, target_control_plane)

        subprocess.run(
            [
                args.uv_bin,
                "lock",
                "--upgrade-package",
                f"{API_PACKAGE}=={target_api}",
            ],
            cwd=candidate_env,
            check=True,
        )
        subprocess.run(
            [
                args.npm_bin,
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ],
            cwd=candidate_control_plane,
            check=True,
        )

        if locked_api_version(candidate_uv_lock) != target_api:
            raise RuntimeError("uv resolved a different Hindsight API version")
        if locked_control_plane_version(candidate_package_lock) != target_control_plane:
            raise RuntimeError("npm resolved a different Hindsight Control Plane version")

        subprocess.run([args.backup_bin, "pre-upgrade"], check=True)

        for candidate, destination in (
            (candidate_pyproject, pyproject),
            (candidate_uv_lock, uv_lock),
            (candidate_package_json, package_json),
            (candidate_package_lock, package_lock),
        ):
            publish_file(candidate, destination)

    print(
        "Hindsight server locks updated: "
        f"API {current_api} -> {target_api}; "
        f"Control Plane {current_control_plane} -> {target_control_plane}."
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except Exception as error:
        print(f"Hindsight lock update failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
