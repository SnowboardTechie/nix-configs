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

The only permitted dirty-tree state is the reviewed, unstaged, one-line classic
`/status` approval query correction from `session_key` to `session_id`. Its
producer, `/yolo`, must still use `self.session_id`. No other file, staged work,
untracked file, mode change, extra hunk, or unfamiliar query shape is admitted.
This is the compatibility patch recorded in
`dotfiles/hermes/runtime-patches/cli-status-session.*`.

Before downtime, admission computes the actual three-way Git merge of that
patch onto the selected target and requires exactly the intended corrected
source. Unknown upstream refactors and conflicts refuse without touching the
checkout. A configuration that discards local changes is also refused while
this patch is needed. When upstream incorporates the identical correction,
the normal stash merge absorbs it and the updated tree is clean automatically.

## Execution and verification

After admission, a successful quick state snapshot is a hard gate. The wrapper
announces actual mutation, calls the supported noninteractive `hermes update`
without the old patch-dropping `--keep-stash` flag, and verifies:

- HEAD is the exact preflighted channel target, not the moving main branch;
- no new or unrelated source changes were introduced;
- a required status correction remains effective, either locally or upstream;
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
