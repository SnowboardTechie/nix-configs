# Pi Durable: separate development from deployment

## Authority and current boundary

Bryan approved repository closeout and workspace separation on 2026-10-18. Nix owns launchd; Bryan activates only through the standard `update-system` from clean pushed `nix-configs/main`. Do not hand-edit the installed plist, use a temporary worktree/store path, start a second service, or change credentials, firewall, sandbox, page route, provider or room policy.

Source workspace: `/Users/bryan/code/durable-pi-workspace`.
Dedicated deployment: `/Users/bryan/.local/share/pi-durable/sgg-deployment` (independent Git checkout).
Config: `/Users/bryan/.config/pi-durable/sgg.json` (unchanged by this Nix change).
State: `/Users/bryan/.local/state/pi-durable/sgg` (one writer).
Source of live acceptance: application `workspaces/sgg/OPERATIONS.md` and `vault/INDEX.md`.

The Nix change only selects the dedicated checkout. Evidence-location migration is a separate application/configuration operation: do not infer it happened because the launch path changed. Historical recorded case paths and old Matrix notice commands may still name the source workspace. Preserve that workspace and those records; its tools can access the same explicit config/state. Existing plugin commands must remain available until separately reconciled.

## Prepared baseline

The dedicated checkout was cloned from the real origin at pushed main `086e5d5ec7db8ba4b9c17e410fd86b83b46d8e57`. Existing installed locked dependencies were copied, not downloaded, and `npm run build` produced a clean commit-bound stamp. No writer was launched from it. The source application was rebuilt/restarted at the same commit in a bounded window after proving all five tasks terminal and no case VM present; starts/polling resumed and the TailNet dashboard returned HTTP 200. These are preparation observations, not activation evidence and not a promise of unchanged state.

## Operator activation — Studio, independent terminal

First ensure the prepared checkout is still clean, remote main is known, and the stamp verifies. Do not update/build it under a running process. A later documentation change under `workspaces/` is part of the application's source fingerprint too.

```bash
set -eu
DEPLOY=/Users/bryan/.local/share/pi-durable/sgg-deployment
SOURCE=/Users/bryan/code/durable-pi-workspace
NIX=/Users/bryan/code/nix-configs
AGENT="gui/$(id -u)/org.nixos.pi-durable-sgg"
test -z "$(git -C "$NIX" status --porcelain)"
git -C "$NIX" fetch origin main
test "$(git -C "$NIX" branch --show-current)" = main
test "$(git -C "$NIX" rev-parse HEAD)" = "$(git -C "$NIX" rev-parse origin/main)"
test -z "$(git -C "$DEPLOY" status --porcelain)"
git -C "$DEPLOY" fetch origin main
test "$(git -C "$DEPLOY" branch --show-current)" = main
test "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/main)"
node --input-type=module -e 'import {verifyBuild} from "file:///Users/bryan/.local/share/pi-durable/sgg-deployment/dist/packages/foundation/src/build-identity.js"; const v=verifyBuild("/Users/bryan/.local/share/pi-durable/sgg-deployment"); console.log(v); if(v.problem) process.exit(1)'
node "$SOURCE/dist/workspaces/sgg/src/main.js" status
```

Stop if branch/build identity fails, there is an unexpected pause/hold, or a review is in progress. If remote main advanced, update the inactive deployment checkout with a safe fast-forward and rebuild/test from its clean published bytes before restarting this procedure. Do not waive identity checks or build over a running deployment.

Record the existing pause mode. Pause starts using the current source CLI; then confirm the dashboard shows no active review, `container list --all` shows no `pi-sgg-*` VM, and the read-only task inspection shows no nonterminal task. Do not kill a review to get past the gate. With the store drained, run the usual system update from the same independent terminal:

```bash
node /Users/bryan/code/durable-pi-workspace/dist/workspaces/sgg/src/main.js pause "deployment checkout cutover"
cd /Users/bryan/code/nix-configs
update-system
launchctl print "gui/$(id -u)/org.nixos.pi-durable-sgg"
node /Users/bryan/.local/share/pi-durable/sgg-deployment/dist/workspaces/sgg/src/main.js status
```

Readback must show the ProgramArguments path under `sgg-deployment`, one running service, correct verified build identity, unchanged sandbox version, and no new commissioning gap. If starts were allowed before the window, unpause using the deployment CLI; otherwise retain the original pause. Fetch the standing page and metrics, then verify a fresh normal intake cycle and evidence persistence with no missing cases/records. Check the plugin's read-only path using its existing supported status command; do not submit a public review as a probe. A startup check is not end-to-end evidence.

## Failure and rollback

If `update-system` fails, inspect `launchctl print` before acting: activation may have partially completed. Keep starts paused while identifying the actual command path and build. Do not start another writer manually. Restoring the prior source deployment means reverting only the dedicated-checkout declaration in `modules/services/pi-durable.nix` through a reviewed, committed/pushed change on clean `nix-configs/main`, then Bryan running the standard `update-system` again. First prove the original source build verifies; never silently restore an old compiled tree over newer code.

The config/state/evidence were not moved by this Nix change, so no stale SQLite restore or data copy is part of rollback. If a later evidence migration processed new records, preserve/reconcile the newest evidence and receipts before changing its path. Never roll back mutable state to an old snapshot while retaining newer external effects.

## Verification

`python3 scripts/test_pi_durable_module.py` covers the exact launch command and existing proxy/firewall/route controls. `nix flake check` and exact Studio option evaluation validate declarations without activation. A targeted independent deployment-boundary review precedes publication. Live activation remains pending until the exact installed command, process and HTTP/status readbacks establish it.
