# Module tests

The repository's own tests, `scripts/test_*.py`, one per service module or script. They evaluate Studio and the other
hosts with options overridden and assert what each module generates (launchd plists, firewall and pf rules, alert rules,
Grafana dashboards), and they run the service scripts against stand-in commands. `modules/services/AGENTS.md` names the
test for each module.

## Sub-features

- `one-module`: `$V run module-tests -- <name>` runs `scripts/test_<name>.py` for the module you touched.
- `all-modules`: `$V run module-tests` runs all 19, about 11 minutes.
- `no-bytecode`: tests run with `PYTHONDONTWRITEBYTECODE=1`, so `scripts/__pycache__` is not written.

## How to get to it (user POV)

- `python3 scripts/test_<name>.py` from the repository root, as each module's row in `modules/services/AGENTS.md` says.

## Driving it with nix-verify

Preconditions:

- `$V doctor` passes, with `python3` found. Some tests need `promtool` and `pfctl`; they find them themselves.

- **One module.** Run `$V run module-tests -- omlx_module`. The step `test_omlx_module` is `ok` with `OK`, about 25 s.
- **All.** Run `$V run module-tests`. Each test is one step. On main (2026-11-16) two fail, see Gotchas.
- **Proof.** Each test's `.err` file holds unittest's output; a failing step lists its `FAIL:` and `ERROR:` lines.

## Gotchas

- On main (commit 21d51c1, 2026-11-16) `test_dashy_status_api` fails in `test_a_pi_durable_link_follows_its_service`
  and `test_pi_bots_service` fails in `test_pi_durable_agents_find_it`. So the full set fails on main: compare a
  branch's failures with main's.
- `test_pi_durable_module` takes about 5 minutes alone.
- The tests evaluate through a `path:` flake reference, so unlike `evaluate` they see untracked files.
- The full set takes over ten minutes, so it is not in the default set.
