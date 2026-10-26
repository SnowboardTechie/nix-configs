# Guarded Hermes automatic updates

Studio runs Bryan's updater nightly at 04:00 and Traci's weekly at 05:00 Sunday.
Nix owns the schedules and wrapper; Hermes owns each managed runtime's update,
dependency preparation, local-change preservation, and supported fleet restart.
No system rebuild or update-channel change is performed by the scheduled job.

## Admission and target selection

`scripts/hermes-update-preflight.py` uses the installed Hermes channel resolver.
A stable release is an exact commit, normally on detached HEAD; it is not the
current `origin/main`. Main-channel installations still follow main. The
wrapper deepens shallow installations before checking ancestry, rejects local
commits and unintended branches, and leaves an already-current installation
alone without a backup, restart, or lifecycle notification.

The only permitted dirty-tree states are two reviewed, unstaged patches, each
optional:

- the one-line classic `/status` approval query correction from `session_key`
  to `session_id`. Its producer, `/yolo`, must still use `self.session_id`. It is
  recorded in `dotfiles/hermes/runtime-patches/cli-status-session.*`.
- the Matrix-through-gateway patch,
  `scripts/hermes-runtime-patches/matrix-send-gateway.patch` (#170's
  `matrix-send` plus #191's `matrix-react` and `matrix-redact`), pinned by
  SHA-256. Its three modified files and two new untracked files must equal HEAD
  plus that patch exactly. Until the live checkout moves to it, the superseded
  #170 patch alone (`matrix-send-gateway-170.patch`, also pinned) is admitted
  the same way, so a nightly update between deploying this updater and the move
  carries the old patch instead of refusing. The checkout must be exactly one
  of the two. How the patch is carried, moved, first applied and retired is in
  [`modules/services/AGENTS.md`](../modules/services/AGENTS.md#hermes-local-runtime-patches).

No other file, staged work, untracked file, mode change, extra hunk, or
unfamiliar query shape is admitted.

Before downtime, admission computes the actual three-way Git merge of each
present patch onto the selected target and requires exactly the intended corrected
source. Unknown upstream refactors and conflicts refuse without touching the
checkout. A configuration that discards local changes is also refused while
either patch is present. When upstream incorporates the identical correction,
the normal stash merge absorbs it and the updated tree is clean automatically.

## Execution and verification

After admission, a successful quick state snapshot is a hard gate. The wrapper
announces actual mutation, calls the supported noninteractive `hermes update`
without the old patch-dropping `--keep-stash` flag, and verifies:

- HEAD is the exact preflighted channel target, not the moving main branch;
- no new or unrelated source changes were introduced;
- a required status correction remains effective, either locally or upstream;
- a planned matrix-send patch, the same one of the two, is still exactly applied, or the target contains it;
- the install stamp identifies the selected commit;
- the backend serves the stamped version and any configured gateway is running.

The headless wrapper also coordinates its system LaunchDaemon. Failure and
refusal announcements still name the updater error log. A channel advancing
between admission and mutation fails verification rather than claiming an
unreviewed target succeeded. The upstream updater's backup/stash/recovery state
is retained for diagnosis; the wrapper does not reset or discard user changes.

## Deployment

Publish verified changes, then use `update-system` from clean pushed main.
If a temporary launchd disabled override exists, deliberately release it and
load the newly deployed agent. Verify the 04:00 schedule. Do not call a
pause/disable a repaired updater: completion requires a real guarded update,
retained patch, healthy services, and a silent subsequent no-op check.

Regression: `python3 scripts/test_hermes_updater.py` exercises real Git release,
patch-preservation/absorption, refusal and verification cases, plus the generated
Nix schedule and wrapper. It does not replace the live update acceptance run.

## Verified Studio rollout — 2026-10-23

The repair through `d7bd4b2` was independently reviewed with 23 passing
regressions, `nix flake check`, a Studio system build, and syntax/ShellCheck
validation of both generated updaters. The actual software upgrade reached
v0.22.5 at `bf0203a794a07aac3ff764972ab9087baa4e4f4e`; the install stamp,
exact retained status patch, pre-update snapshot and current gateway code
identity were read back. The initial gateway-owned terminal test was interrupted
by its gateway restart after source verification; that interruption was not
reported as a successful wrapper exit.

After Bryan ran `update-system`, the installed plist and loaded user job were
independently verified against the repaired Nix program. The disabled override
is released and the calendar remains 04:00 daily. A manual kickstart of that
actual launchd job exited 0 on an already-current release, with no new backup
or gateway restart. The HTTPS backend and supervised gateway both report
v0.22.5; the gateway is running without a pending restart request.

The obsolete upstream-fix hold watcher was removed from live cron and its
installed script, with retirement declared in dotfiles commit `e3ed0f7`.
