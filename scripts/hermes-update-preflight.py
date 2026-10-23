#!/usr/bin/env python3
"""Channel-aware admission for the Nix-owned Hermes update wrapper.

Only the reviewed classic /status session-id correction may accompany an update.
Hermes itself owns backup-independent update/stash/restart mechanics. This helper
never checks out code, stashes changes, or changes the configured update channel.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

STATUS_PATH = "hermes_cli/cli_session_mixin.py"
SHA = re.compile(r"[0-9a-f]{40}")


def git(root, *args, check=True, input=None):
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input, capture_output=True,
        timeout=180, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result


def corrected_status_source(source):
    """Return the one reviewed correction, refusing unfamiliar status/toggle shapes."""
    text = source.decode("utf-8")
    tree = ast.parse(text)
    methods = {}
    for name in ("_show_session_status", "_toggle_yolo"):
        matches = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(matches) != 1:
            raise ValueError("classic /status or /yolo changed shape; review required")
        methods[name] = matches[0]
    queries = [node for node in ast.walk(methods["_show_session_status"])
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "is_approval_bypass_active_for_session"]
    if len(queries) != 1 or len(queries[0].args) != 1 or queries[0].keywords:
        raise ValueError("classic /status approval query changed shape")
    argument = queries[0].args[0]
    expression = ast.unparse(argument)
    if expression not in ("getattr(self, 'session_key', '') or ''",
                           "getattr(self, 'session_id', '') or ''"):
        raise ValueError("classic /status approval identity changed")
    toggles = [node for node in ast.walk(methods["_toggle_yolo"])
               if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "session_key"
                       for target in node.targets)]
    if len(toggles) != 1 or ast.unparse(toggles[0].value) != "self.session_id or 'default'":
        raise ValueError("classic /yolo session identity changed")
    if expression == "getattr(self, 'session_id', '') or ''":
        return source
    if not isinstance(argument, ast.BoolOp) or not isinstance(argument.values[0], ast.Call):
        raise ValueError("classic /status approval expression changed shape")
    literal = argument.values[0].args[1]
    if literal.lineno != literal.end_lineno:
        raise ValueError("multiline session identity needs review")
    lines = source.splitlines(keepends=True)
    line = lines[literal.lineno - 1]
    quote = line[literal.col_offset:literal.col_offset + 1]
    lines[literal.lineno - 1] = (line[:literal.col_offset] + quote + b"session_id" + quote
                                + line[literal.end_col_offset:])
    return b"".join(lines)


def checkout_patch(root):
    """Reject all drift except an unstaged, exact instance of the reviewed patch."""
    branch = git(root, "branch", "--show-current").stdout.decode().strip()
    if branch not in ("", "main"):
        raise ValueError(f"refusing unintended branch {branch!r}")
    if git(root, "diff", "--cached", "--quiet", check=False).returncode:
        raise ValueError("refusing staged changes")
    if git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout:
        raise ValueError("refusing untracked files")
    changes = git(root, "diff", "--name-only", "-z", "HEAD").stdout.split(b"\0")[:-1]
    if not changes:
        return False
    if changes != [STATUS_PATH.encode()]:
        raise ValueError("refusing unrelated working-tree changes")
    path = root / STATUS_PATH
    if path.is_symlink() or not path.is_file():
        raise ValueError("status patch target is not a regular file")
    original = git(root, "show", f"HEAD:{STATUS_PATH}").stdout
    expected = corrected_status_source(original)
    if expected == original or path.read_bytes() != expected:
        raise ValueError("working-tree changes are not exactly the reviewed /status patch")
    mode = git(root, "diff", "--summary").stdout
    if mode:
        raise ValueError("refusing file mode changes")
    return True


def prepare_checkout(root, target, channel, *, discard_changes=False):
    """Validate a resolved immutable target and predict Hermes's stash merge."""
    if not isinstance(target, str) or not SHA.fullmatch(target):
        raise ValueError("update channel did not resolve to a full commit")
    preserve_patch = checkout_patch(root)
    head = git(root, "rev-parse", "HEAD").stdout.decode().strip()
    if git(root, "merge-base", "--is-ancestor", head, "origin/main", check=False).returncode:
        raise ValueError("refusing local commits not contained in origin/main")
    if git(root, "merge-base", "--is-ancestor", head, target, check=False).returncode:
        raise ValueError("refusing an automatic downgrade or divergent update target")
    if preserve_patch:
        if head != target and discard_changes:
            raise ValueError("updates.non_interactive_local_changes=discard would lose the required patch")
        original = git(root, "show", f"HEAD:{STATUS_PATH}").stdout
        candidate = git(root, "show", f"{target}:{STATUS_PATH}").stdout
        expected = corrected_status_source(candidate)
        scratch = root.parent / "cache" / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hermes-update-merge-", dir=scratch) as directory:
            paths = [Path(directory) / name for name in ("patched", "base", "candidate")]
            for path, data in zip(paths, ((root / STATUS_PATH).read_bytes(), original, candidate)):
                path.write_bytes(data)
            result = git(root, "merge-file", "--stdout", *(str(path) for path in paths), check=False)
        if result.returncode or result.stdout != expected:
            raise ValueError("the required /status patch cannot be cleanly preserved on this target")
    return {"head": head, "target": target, "channel": channel, "preserve_status_patch": preserve_patch}


def resolve_plan(root, config_path):
    # These are non-bootstrapping leaf APIs, not hermes_cli.main/config entry points.
    # Use the actual source root even when the legacy venv's editable imports omit pm.
    sys.path.insert(0, str(root))
    import yaml
    from hermes_cli.source_releases import resolve_source_target
    from hermes_cli.update_channel import channel_record, resolve_update_channel, rides_default_channel

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError("Hermes configuration is not a mapping")
    channel = resolve_update_channel(config, root)
    git(root, "fetch", "--quiet", "--no-tags", "origin",
        "+refs/heads/main:refs/remotes/origin/main")
    target = resolve_source_target(
        channel, ["git"], root,
        forward_only=rides_default_channel(channel_record(config, root), channel, root),
    )
    if target.commit:
        commit = target.commit
    elif target.branch == "main":
        commit = git(root, "rev-parse", "origin/main").stdout.decode().strip()
    else:
        raise ValueError("automatic updates require a release commit or the main branch")
    if git(root, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
        git(root, "fetch", "--quiet", "--no-tags", "origin", commit)
    updates = config.get("updates") or {}
    discard = str(updates.get("non_interactive_local_changes", "stash")).lower() == "discard"
    return prepare_checkout(root, commit, channel, discard_changes=discard)


def verify_checkout(root, plan):
    head = git(root, "rev-parse", "HEAD").stdout.decode().strip()
    if head != plan["target"]:
        raise ValueError(f"updated checkout is {head}, expected {plan['target']}; channel may have moved")
    patched = checkout_patch(root)
    if patched and not plan["preserve_status_patch"]:
        raise ValueError("update introduced an unexpected status patch")
    if plan["preserve_status_patch"]:
        current = (root / STATUS_PATH).read_bytes()
        if corrected_status_source(current) != current:
            raise ValueError("update did not retain the required /status correction")
    stamp = json.loads((root / "install-stamp.json").read_text(encoding="utf-8-sig"))
    if stamp.get("commit") != plan["target"]:
        raise ValueError("install stamp does not identify the expected updated commit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "verify"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--plan-json")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.action == "plan":
            if args.config is None:
                parser.error("plan requires --config")
            plan = resolve_plan(root, args.config)
            print(f"Hermes {plan['channel']}: {plan['head'][:12]} -> {plan['target'][:12]}; "
                  f"preserve status patch: {plan['preserve_status_patch']}", file=sys.stderr)
            if plan["head"] != plan["target"]:
                print(json.dumps(plan))
        else:
            if args.plan_json is None:
                parser.error("verify requires --plan-json")
            plan = json.loads(args.plan_json)
            verify_checkout(root, plan)
            print(f"Hermes checkout and retained patch verified at {plan['target']}")
    except (ValueError, OSError, subprocess.SubprocessError, SyntaxError) as exc:
        print(f"Hermes automatic update refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
