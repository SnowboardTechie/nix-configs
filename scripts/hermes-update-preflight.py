#!/usr/bin/env python3
"""Channel-aware admission for the Nix-owned Hermes update wrapper.

Only two reviewed local patches may accompany an update, each optional: the classic
/status session-id correction and the #170 matrix-send-through-gateway patch, pinned
to the exact reviewed patch file. Hermes itself owns backup-independent
update/stash/restart mechanics. This helper never checks out code, stashes changes,
or changes the configured update channel.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

STATUS_PATH = "hermes_cli/cli_session_mixin.py"
SHA = re.compile(r"[0-9a-f]{40}")
# bryan/durable-pi-workspace#170, reviewed as hermes-agent bf0203a794..2be71e5f54.
MATRIX_PATCH = Path(__file__).resolve().parent / "hermes-runtime-patches/matrix-send-gateway.patch"
MATRIX_PATCH_SHA256 = "3bbb0f167d581a6ef3e6c26244248b250885cb12e808d494ca8e9976efb1bd72"
MATRIX_MODIFIED = ("gateway/run_bootstrap.py", "tools/send_message_senders.py")
MATRIX_NEW = ("gateway/matrix_send_forward.py", "tests/gateway/test_matrix_send_forward.py")
MATRIX_PATHS = MATRIX_MODIFIED + MATRIX_NEW


def git(root, *args, check=True, input=None, env=None):
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input, capture_output=True,
        timeout=180, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
    )
    if check and result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result


def scratch_directory(root):
    scratch = root.parent / "cache" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


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


def load_matrix_patch(path=None):
    data = Path(path or MATRIX_PATCH).read_bytes()
    if hashlib.sha256(data).hexdigest() != MATRIX_PATCH_SHA256:
        raise ValueError(f"{path or MATRIX_PATCH} is not the reviewed matrix-send patch")
    return data


def matrix_sources(root, commit):
    """Map each patch path to its content in commit with the reviewed patch applied.

    Returns (sources, absorbed): absorbed means commit already contains the change and
    sources is commit's own content. None when the patch neither applies nor is contained.
    """
    patch = load_matrix_patch()
    with tempfile.TemporaryDirectory(prefix="hermes-matrix-index-", dir=scratch_directory(root)) as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        git(root, "read-tree", commit, env=env)
        absorbed = False
        if git(root, "apply", "--cached", "-", input=patch, env=env, check=False).returncode:
            if git(root, "apply", "--cached", "--check", "-R", "-", input=patch, env=env,
                   check=False).returncode:
                return None, False
            absorbed = True
        sources = {path: git(root, "show", f":{path}", env=env).stdout for path in MATRIX_PATHS}
    return sources, absorbed


def regular_file(root, path, label):
    target = root / path
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"{label} target {path} is not a regular file")
    return target


def checkout_patches(root):
    """Reject all drift except unstaged, exact instances of the reviewed patches.

    Returns the set of present patches: "status" and/or "matrix".
    """
    branch = git(root, "branch", "--show-current").stdout.decode().strip()
    if branch not in ("", "main"):
        raise ValueError(f"refusing unintended branch {branch!r}")
    if git(root, "diff", "--cached", "--quiet", check=False).returncode:
        raise ValueError("refusing staged changes")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")[:-1]
    if not set(untracked) <= {path.encode() for path in MATRIX_NEW}:
        raise ValueError("refusing untracked files")
    changes = git(root, "diff", "--name-only", "-z", "HEAD").stdout.split(b"\0")[:-1]
    if not set(changes) <= {path.encode() for path in (STATUS_PATH, *MATRIX_MODIFIED)}:
        raise ValueError("refusing unrelated working-tree changes")
    if git(root, "diff", "--summary", "HEAD").stdout:
        raise ValueError("refusing file mode changes")
    present = set()
    if STATUS_PATH.encode() in changes:
        path = regular_file(root, STATUS_PATH, "status patch")
        original = git(root, "show", f"HEAD:{STATUS_PATH}").stdout
        expected = corrected_status_source(original)
        if expected == original or path.read_bytes() != expected:
            raise ValueError("working-tree changes are not exactly the reviewed /status patch")
        present.add("status")
    if untracked or set(changes) - {STATUS_PATH.encode()}:
        sources, absorbed = matrix_sources(root, "HEAD")
        if sources is None or absorbed:
            raise ValueError("working-tree changes are not exactly the reviewed matrix-send patch")
        for path in MATRIX_PATHS:
            target = regular_file(root, path, "matrix-send patch")
            if target.read_bytes() != sources[path]:
                raise ValueError("working-tree changes are not exactly the reviewed matrix-send patch")
            if path in MATRIX_NEW and target.stat().st_mode & 0o111:
                raise ValueError(f"refusing file mode of untracked {path}")
        present.add("matrix")
    return present


def prepare_checkout(root, target, channel, *, discard_changes=False):
    """Validate a resolved immutable target and predict Hermes's stash merge."""
    if not isinstance(target, str) or not SHA.fullmatch(target):
        raise ValueError("update channel did not resolve to a full commit")
    patches = checkout_patches(root)
    head = git(root, "rev-parse", "HEAD").stdout.decode().strip()
    if git(root, "merge-base", "--is-ancestor", head, "origin/main", check=False).returncode:
        raise ValueError("refusing local commits not contained in origin/main")
    if git(root, "merge-base", "--is-ancestor", head, target, check=False).returncode:
        raise ValueError("refusing an automatic downgrade or divergent update target")
    if patches and head != target and discard_changes:
        raise ValueError("updates.non_interactive_local_changes=discard would lose the required patch")
    # Hermes restores every local change from one stash: a conflict in either patch
    # resets the tree and parks both, so each must merge cleanly before downtime.
    if "status" in patches:
        original = git(root, "show", f"HEAD:{STATUS_PATH}").stdout
        candidate = git(root, "show", f"{target}:{STATUS_PATH}").stdout
        expected = corrected_status_source(candidate)
        with tempfile.TemporaryDirectory(prefix="hermes-update-merge-", dir=scratch_directory(root)) as directory:
            paths = [Path(directory) / name for name in ("patched", "base", "candidate")]
            for path, data in zip(paths, ((root / STATUS_PATH).read_bytes(), original, candidate)):
                path.write_bytes(data)
            result = git(root, "merge-file", "--stdout", *(str(path) for path in paths), check=False)
        if result.returncode or result.stdout != expected:
            raise ValueError("the required /status patch cannot be cleanly preserved on this target")
    if "matrix" in patches:
        predict_matrix_restore(root, target)
    return {"head": head, "target": target, "channel": channel,
            "preserve_status_patch": "status" in patches,
            "preserve_matrix_patch": "matrix" in patches}


def predict_matrix_restore(root, target):
    """Require Hermes's stash apply to leave exactly target + the reviewed patch."""
    refusal = "the required matrix-send patch cannot be cleanly preserved on this target"
    expected, absorbed = matrix_sources(root, target)
    if expected is None:
        raise ValueError(refusal)
    for path in MATRIX_NEW:
        # `stash apply` keeps a file the target already tracks; that is only right
        # when the target's copy is the reviewed one.
        tracked = not git(root, "cat-file", "-e", f"{target}:{path}", check=False).returncode
        if tracked != absorbed:
            raise ValueError(refusal)
    for path in MATRIX_MODIFIED:
        sides = ((root / path).read_bytes(), git(root, "show", f"HEAD:{path}").stdout,
                 git(root, "show", f"{target}:{path}").stdout)
        with tempfile.TemporaryDirectory(prefix="hermes-update-merge-", dir=scratch_directory(root)) as directory:
            paths = [Path(directory) / name for name in ("patched", "base", "candidate")]
            for scratch_path, data in zip(paths, sides):
                scratch_path.write_bytes(data)
            result = git(root, "merge-file", "--stdout", *(str(p) for p in paths), check=False)
        if result.returncode or result.stdout != expected[path]:
            raise ValueError(refusal)


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
    patches = checkout_patches(root)
    if "status" in patches and not plan["preserve_status_patch"]:
        raise ValueError("update introduced an unexpected status patch")
    if plan["preserve_status_patch"]:
        current = (root / STATUS_PATH).read_bytes()
        if corrected_status_source(current) != current:
            raise ValueError("update did not retain the required /status correction")
    preserve_matrix = plan.get("preserve_matrix_patch", False)
    if "matrix" in patches and not preserve_matrix:
        raise ValueError("update introduced an unexpected matrix-send patch")
    if preserve_matrix and "matrix" not in patches and not matrix_sources(root, "HEAD")[1]:
        raise ValueError("update did not retain the required matrix-send patch")
    stamp = json.loads((root / "install-stamp.json").read_text(encoding="utf-8-sig"))
    if stamp.get("commit") != plan["target"]:
        raise ValueError("install stamp does not identify the expected updated commit")


def main():
    global MATRIX_PATCH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "verify"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--plan-json")
    parser.add_argument("--matrix-patch", type=Path, default=MATRIX_PATCH)
    args = parser.parse_args()
    root = args.root.resolve()
    MATRIX_PATCH = args.matrix_patch
    try:
        load_matrix_patch()
        if args.action == "plan":
            if args.config is None:
                parser.error("plan requires --config")
            plan = resolve_plan(root, args.config)
            print(f"Hermes {plan['channel']}: {plan['head'][:12]} -> {plan['target'][:12]}; "
                  f"preserve status patch: {plan['preserve_status_patch']}; "
                  f"preserve matrix-send patch: {plan['preserve_matrix_patch']}", file=sys.stderr)
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
