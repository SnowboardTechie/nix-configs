# Module tests

The repository's own tests, `scripts/test_*.py`, one per service module or script. They evaluate Studio and the other
hosts with options overridden and assert what each module generates (launchd plists, firewall and pf rules, alert rules,
Grafana dashboards), and they run the service scripts against stand-in commands. `modules/services/AGENTS.md` names the
test for each module.

## Sub-features

- `one-module`: `$V run module-tests -- <name>` runs `scripts/test_<name>.py` for the module you touched.
- `all-modules`: `$V run module-tests` runs all 19, about 10 minutes. The count is asserted (`EXPECTED_TESTS` in
  `../scripts/nix-verify`); a new or removed test changes it and this line together. An empty set fails.
- `throwaway-copy`: the tests run in a copy of the worktree under `$TMPDIR`, because their own Nix calls are unlocked.
  The real `flake.lock` and tree cannot be written; a test that changes the copy's `flake.lock` fails the run.
- `no-bytecode`: tests run with `PYTHONDONTWRITEBYTECODE=1`, so `scripts/__pycache__` is not written.

## How to get to it (user POV)

- `python3 scripts/test_<name>.py` from the repository root, as each module's row in `modules/services/AGENTS.md` says.

## Driving it with nix-verify

Preconditions:

- `$V doctor` passes, with `python3` found. Some tests need `promtool` and `pfctl`; they find them themselves.

- **One module.** Run `$V run module-tests -- omlx_module`. The step `test_omlx_module` is `ok` with `OK`, about 25 s.
- **All.** Run `$V run module-tests`. Steps: `test set` (19 test(s)), `throwaway copy` (its path), one step per test,
  `tests left flake.lock alone`, `worktree untouched`. On main (2026-11-17) three tests fail, see Gotchas.
- **Proof.** Each test's `.err` file holds unittest's output; a failing step lists its `FAIL:` and `ERROR:` lines.

## Gotchas

- On main (commit 39befbc, 2026-11-17) three tests fail: `test_dashy_status_api` (two tests),
  `test_monitoring_content_probes` (the `Wiring` class: Studio's `Pi Bots Voice` probe is not in its list) and
  `test_pi_bots_service` (`test_pi_durable_agents_find_it`: the `pi-durable-sampler` agent is off on Studio).
  `../SKILL.md`'s Gotchas give each cause. So the full set fails on main: compare a branch's failures with main's.
- `test_pi_durable_module` takes about 5 minutes alone.
- `modules/services/AGENTS.md` names `scripts/test_matrix_send_forward.py`, which does not exist (2026-11-17).
- The tests evaluate through a `path:` flake reference, so unlike `evaluate` they see untracked files. The copy
  holds tracked and untracked files but not ignored ones, so an ignored file a test needs is missing there.
- The full set takes about ten minutes, so it is not in the default set.
