# Primary Hermes automatic-update hold

Bryan's primary updater is held with
`services.hermes.autoUpdate.enable = false` in `modules/hosts/studio.nix`.
Traci's weekly updater, both users' services, and the primary runtime patch
remain unchanged. This restores the hold Bryan approved on 2026-10-14; it is
not a claim that automatic upgrades have been repaired or resumed.

## Why the hold must be declarative

The 2026-10-23 04:00 run exited before updating anything:

    Refusing to update bryan: checkout is on branch '', not main.

The previously disabled user LaunchAgent was enabled and loaded again.
The deployed nix-darwin activation script reloads a changed agent with
`launchctl load -w`, which clears the persistent disabled override. A launchd
hold alone therefore does not survive changes to the declared updater.
Omitting the updater from desired state prevents that reload path.

## Remaining upgrade blockers

1. The managed runtime tracks Hermes's `stable` channel. Stable source updates
   deliberately check out an exact release commit on detached HEAD. The shared
   wrapper still assumes a checkout on `main` and compares against
   `origin/main`, both before and after updating. Merely permitting an empty
   branch name would not fix its target selection or post-update checks.
2. The checkout contains the intentional classic CLI `/status` repair:
   `session_key` to `session_id` in the approval-bypass query. Herdr's worker
   preflight depends on truthful approval status. The patch is owned by
   `dotfiles/hermes/runtime-patches/cli-status-session.*`, not accidental drift.
   On 2026-10-23 the committed source in installed v0.22.4, the available stable
   v0.22.5 (`bf0203a794a0`), and upstream main (`db1fa153fe24`) still queried
   `session_key`. Do not discard the patch or weaken the dirty-tree guard to
   make the updater run.

The existing model-free upstream-fix watcher in dotfiles remains the owner of
fix-candidate notifications. Its alert is not adoption or restart permission.

## Release gates

- Verify an immutable replacement provides correct classic `/status` behavior
  and current Herdr admission compatibility before removing the local repair.
- Reconcile the wrapper with the configured update channel's actual target,
  including clean detached release checkouts, silent no-ops, and post-update
  verification of that release rather than `origin/main`. Preserve refusal of
  unrelated dirty trees, local commits, and unintended feature branches.
- Exercise the supported updater with a successful pre-update state backup and
  verify the separately running gateway and backend through their real route.
- Only then deliberately enable the primary updater in Nix, publish, and deploy
  through `update-system` from clean pushed main. Release the launchd disabled
  override deliberately and verify the 04:00 schedule without firing an
  unrequested immediate upgrade.
- Reconcile/retire the dotfiles hold documentation and upstream-fix watcher as
  part of the same release.

Regression: `python3 scripts/test_hermes_update_hold.py` verifies the generated
configuration omits only the primary updater, retains its gateway/dashboard,
and preserves the headless weekly schedule.
