# Pi Durable: separate development from deployment

## Authority and current boundary

After clarification of the applied changes and remaining scope, Bryan explicitly authorized completing the separation on 2026-10-18 and performed standard `update-system` from clean pushed main `7fb90e0`. Live launchctl readback names the dedicated deployment path; the service is restored to unpaused operation. The application runbook §3.12 owns detailed acceptance. Nix owns launchd; future activations likewise use Bryan's standard `update-system`, never a hand-edited plist, temporary service override or second writer. Credentials, firewall, sandbox, page route, provider and room policy remain outside this path change.

Source workspace: `/Users/bryan/code/durable-pi-workspace`.
Dedicated deployment: `/Users/bryan/.local/share/pi-durable/sgg-deployment` (independent Git checkout).
Config: `/Users/bryan/.config/pi-durable/sgg.json` (unchanged by this Nix change).
State: `/Users/bryan/.local/state/pi-durable/sgg` (one writer).
Source of live acceptance: application `workspaces/sgg/OPERATIONS.md` and `vault/INDEX.md`.

The Nix change selects only the dedicated service checkout. Application PR42 separately supplies the evidence relocation and submission-path reconciliation; both are now applied and verified in the application runbook. New notice commands, terminal skills and the installed Hermes plugin use the deployment checkout. Historical notes remain unchanged and bind through the configured evidence root. Do not delete historical tracked evidence or infer runtime health from the Nix declaration alone. The procedure below is retained for future activation and rollback, not a pending initial migration.

## Operator activation — Studio, independent terminal

First ensure the prepared checkout is still clean, remote main is known, and the stamp verifies. Do not update/build it under a running process. A later documentation change under `workspaces/` is part of the application's source fingerprint too.

```bash
set -eu
DEPLOY=/Users/bryan/.local/share/pi-durable/sgg-deployment
SOURCE=/Users/bryan/code/durable-pi-workspace
NIX=/Users/bryan/code/nix-configs

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

If `update-system` fails, inspect `launchctl print` before acting: activation may have partially completed. Keep starts paused while identifying the actual command path and build. Do not start another writer manually. Restoring the prior source deployment means reverting the separation changes together (module declaration, matching test, README and service/host guidance) through a reviewed, committed/pushed change on clean `nix-configs/main`, then Bryan running the standard `update-system` again. First prove the original source build verifies; never silently restore an old compiled tree over newer code. Preserve unrelated changes, including any intervening system fixes.

The config/state/evidence were not moved by this Nix change, so no stale SQLite restore or data copy is part of rollback. If a later evidence migration processed new records, preserve/reconcile the newest evidence and receipts before changing its path. Never roll back mutable state to an old snapshot while retaining newer external effects.
