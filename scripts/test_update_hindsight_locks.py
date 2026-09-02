#!/usr/bin/env python3
"""Integration checks for the Studio Hindsight lock updater."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("update-hindsight-locks.py")


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def make_repo(root: Path) -> None:
    env = root / "modules/services/hindsight-env"
    control_plane = env / "control-plane"
    control_plane.mkdir(parents=True)
    (env / "pyproject.toml").write_text(
        '[project]\nname = "hindsight-env"\ndependencies = [\n    "hindsight-api==0.9.2",\n]\n',
        encoding="utf-8",
    )
    (env / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "hindsight-api"\nversion = "0.9.2"\n',
        encoding="utf-8",
    )
    (control_plane / "package.json").write_text(
        json.dumps(
            {
                "private": True,
                "dependencies": {"@vectorize-io/hindsight-control-plane": "0.9.2"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (control_plane / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "node_modules/@vectorize-io/hindsight-control-plane": {"version": "0.9.2"}
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def make_tools(root: Path) -> tuple[Path, Path, Path, Path]:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    calls = root / "calls.log"
    uv = bin_dir / "uv"
    npm = bin_dir / "npm"
    backup = bin_dir / "backup"
    write_executable(
        uv,
        """#!/usr/bin/env python3
import re
from pathlib import Path
text = Path('pyproject.toml').read_text()
version = re.search(r'hindsight-api==([^\"]+)', text).group(1)
Path('uv.lock').write_text(f'version = 1\\n\\n[[package]]\\nname = \"hindsight-api\"\\nversion = \"{version}\"\\n')
""",
    )
    write_executable(
        npm,
        """#!/usr/bin/env python3
import json
from pathlib import Path
package = json.loads(Path('package.json').read_text())
version = package['dependencies']['@vectorize-io/hindsight-control-plane']
Path('package-lock.json').write_text(json.dumps({'packages': {'node_modules/@vectorize-io/hindsight-control-plane': {'version': version}}}, indent=2) + '\\n')
""",
    )
    write_executable(
        backup,
        """#!/usr/bin/env bash
printf 'backup %s\\n' "$*" >> "$CALLS_LOG"
""",
    )
    return uv, npm, backup, calls


def command(root: Path, uv: Path, npm: Path, backup: Path, *versions: str) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(root),
        "--api-version",
        versions[0],
        "--control-plane-version",
        versions[1],
        "--uv-bin",
        str(uv),
        "--npm-bin",
        str(npm),
        "--backup-bin",
        str(backup),
    ]


def test_updates_coordinated_versions_after_backup() -> None:
    with tempfile.TemporaryDirectory(prefix="hindsight-lock-update-") as directory:
        root = Path(directory)
        make_repo(root)
        uv, npm, backup, calls = make_tools(root)
        env = {**os.environ, "CALLS_LOG": str(calls)}
        result = subprocess.run(
            command(root, uv, npm, backup, "0.9.3", "0.9.3"),
            text=True,
            capture_output=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        env_dir = root / "modules/services/hindsight-env"
        assert "hindsight-api==0.9.3" in (env_dir / "pyproject.toml").read_text()
        assert 'version = "0.9.3"' in (env_dir / "uv.lock").read_text()
        package = json.loads((env_dir / "control-plane/package.json").read_text())
        lock = json.loads((env_dir / "control-plane/package-lock.json").read_text())
        assert package["dependencies"]["@vectorize-io/hindsight-control-plane"] == "0.9.3"
        assert lock["packages"]["node_modules/@vectorize-io/hindsight-control-plane"]["version"] == "0.9.3"
        assert calls.read_text() == "backup pre-upgrade\n"
        assert "0.9.2 -> 0.9.3" in result.stdout


def test_mismatched_registry_versions_are_held_without_changes() -> None:
    with tempfile.TemporaryDirectory(prefix="hindsight-lock-update-") as directory:
        root = Path(directory)
        make_repo(root)
        uv, npm, backup, calls = make_tools(root)
        before = {
            path.relative_to(root): path.read_bytes()
            for path in (root / "modules/services/hindsight-env").rglob("*")
            if path.is_file()
        }
        result = subprocess.run(
            command(root, uv, npm, backup, "0.9.3", "0.9.2"),
            text=True,
            capture_output=True,
            env={**os.environ, "CALLS_LOG": str(calls)},
        )
        after = {
            path.relative_to(root): path.read_bytes()
            for path in (root / "modules/services/hindsight-env").rglob("*")
            if path.is_file()
        }
        assert result.returncode == 0, result.stderr
        assert before == after
        assert not calls.exists()
        assert "not coordinated" in result.stdout


def test_resolution_failure_leaves_source_and_backup_untouched() -> None:
    with tempfile.TemporaryDirectory(prefix="hindsight-lock-update-") as directory:
        root = Path(directory)
        make_repo(root)
        uv, npm, backup, calls = make_tools(root)
        write_executable(uv, "#!/usr/bin/env bash\nexit 7\n")
        before = {
            path.relative_to(root): path.read_bytes()
            for path in (root / "modules/services/hindsight-env").rglob("*")
            if path.is_file()
        }
        result = subprocess.run(
            command(root, uv, npm, backup, "0.9.3", "0.9.3"),
            text=True,
            capture_output=True,
            env={**os.environ, "CALLS_LOG": str(calls)},
        )
        after = {
            path.relative_to(root): path.read_bytes()
            for path in (root / "modules/services/hindsight-env").rglob("*")
            if path.is_file()
        }
        assert result.returncode != 0
        assert before == after
        assert not calls.exists()


if __name__ == "__main__":
    test_updates_coordinated_versions_after_backup()
    test_mismatched_registry_versions_are_held_without_changes()
    test_resolution_failure_leaves_source_and_backup_untouched()
    print("ok")
