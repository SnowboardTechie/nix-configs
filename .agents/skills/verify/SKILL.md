---
name: verify
description: Prove a change to nix-configs (the flake's darwin and NixOS host configurations, dev shells and service modules) works without activating anything. Evaluates every host, builds every darwin host's system with --no-link, runs `nix flake check` and the repository's scripts/test_*.py module tests, and keeps evidence outside the repository. Use before committing a Nix change, to prove a host still evaluates and builds, or when a ticket's gate asks for the repository's verification skill. Never switches, activates or updates the lock file.
---

# Verify nix-configs

The flake's surface is its outputs: `darwinConfigurations` (mbp, a6mbp, studio), `nixosConfigurations.gnarbox`,
`devShells` for two systems, and `overlays`. A user of this repository rebuilds a host from them. This skill proves the
part of that which is safe to do anywhere: every host evaluates, every darwin host's system builds into the store, the
flake's own check passes, and the module tests in `scripts/` pass. It never runs the activation.

Everything goes through one control command, `.agents/skills/verify/scripts/nix-verify` (Python 3, standard library
only). Every Nix call it makes passes `--no-update-lock-file`, and builds pass `--no-link`, so no `result` link is left
in the tree. The repository's own tests make Nix calls of their own without that flag, so `module-tests` runs them in a
throwaway copy of the worktree, where nothing they write can reach the real `flake.lock` or tree. Run it from the
repository root, the checkout being verified.

Never run `update-system`, `upgrade-system`, `darwin-rebuild switch|activate`, `nixos-rebuild switch|boot|test`,
`nix flake update`, `nix flake lock`, or a built system's `activate` script while verifying. Those change the
running machine or the lock file, and nothing here needs them.

## Setup (once per shell)

```sh
export NIX_VERIFY_EVIDENCE=/Users/bryan/.pi/pstack-trial/evidence/nix-verify/runs   # optional; default $TMPDIR/nix-verify
V=.agents/skills/verify/scripts/nix-verify
```

`NIX_VERIFY_EVIDENCE` is the evidence root. It must be outside the repository; `run` refuses a path inside it.

## Launch

There is no server. Each `run` drives Nix against the checkout as it is on disk and stops. Launch is:

```sh
$V run                          # the default set: evaluate, build-darwin, flake-check (~55 s warm, see timings)
$V run evaluate                 # one journey
$V run module-tests             # opt-in: every scripts/test_*.py, in a throwaway copy (~12 min)
$V run module-tests -- omlx_module pi_durable_module   # chosen tests, by the name after test_
$V journeys                     # list journeys, default or opt-in, with timings
```

Each run prints its evidence directory first, logs every step as `step <name>: ok|FAIL (<seconds> s)`, and ends with
`<journey>: PASSED` or `FAILED`. Exit 0 means every journey passed, 1 that one failed, 2 a usage error.

Timings on the Mac Studio (aarch64-darwin, 2026-11-16, store warm):

| Journey | Default | Time | What dominates |
| --- | --- | --- | --- |
| `evaluate` | yes | ~40 s | gnarbox's NixOS evaluation (~15 s), mbp and studio (~9 s each) |
| `build-darwin` | yes | ~2 s cached; minutes after a nixpkgs change | Building what the store lacks |
| `flake-check` | yes | ~15 s | Evaluating the eight dev shells and gnarbox |
| `module-tests` | no | ~12 min for all 19 | `test_pi_durable_module.py` (~5 min), `test_forgejo_runner_module.py` and `test_hermes_updater.py` (~70 s each) |

A cold store after a `flake.lock` change can make `build-darwin` take far longer; that is downloading, not a hang.
The step's `.err` file shows Nix's progress.

Ctrl-C or SIGTERM stops the Nix call in progress, marks the run failed and keeps its evidence.

## Doctor

```sh
$V doctor
```

Read-only apart from creating the evidence root. Exits 1 on any `FAIL`. It reports:

- `nix`: present, with its version. `system`: `aarch64-darwin`, or a `WARN` that `build-darwin` needs it.
- `linux builder`: whether an `x86_64-linux` builder is configured. Without one gnarbox is evaluated, never built.
- `flake.lock`: matches `HEAD`, or a `WARN` that the run verifies an edited lock. Runs verify the lock as it is on
  disk, committed or not; each run records which in `run.json` (`flake_lock`) and on its third line.
- `untracked .nix`: a `.nix` file git does not track is invisible to the flake (see Gotchas); `git add` it first.
- `result links`: a `result*` link left in the repository by someone's `nix build`.
- `python3`, needed by `module-tests`; `evidence`: the root is outside the repository and writable.
- `runs`: runs still marked running. One whose `run.lock` no process holds is a `FAIL`: run `cleanup`.

Run it first, and again whenever a run looks wrong.

## Drive

Journeys are built into the command; `$V journeys` lists them. What each proves, how, and the breaks it has been seen
to catch are in `features/`. In short:

- **`evaluate`** lists `darwinConfigurations` and `nixosConfigurations` from the flake, evaluates each host's system
  derivation (`system.drvPath`, or `config.system.build.toplevel.drvPath` for NixOS), checks the `.drv` exists, and
  checks every `modules/hosts/*.nix` has a configuration of the same name.
- **`build-darwin`** builds each darwin host's `system` with `--no-link --print-out-paths` and checks the output holds
  `activate`, `sw/bin` and `darwin-version.json`. It never runs `activate`.
- **`flake-check`** runs `nix flake check --no-update-lock-file` for this machine's system.
- **`module-tests`** runs `scripts/test_*.py` with `PYTHONDONTWRITEBYTECODE=1`. These are the repository's own tests:
  they evaluate hosts with options overridden and run service scripts against stand-ins. Read
  `modules/services/AGENTS.md` for which test covers which module. Their Nix calls are unlocked (for example
  `scripts/test_hermes_updater.py`'s `nix eval` and `nix build`), so the journey copies the worktree's tracked and
  untracked, not ignored, files to a fresh directory under `$TMPDIR` (`nix-verify-tests-*`) and runs every test there.
  The tests find the flake from their own path, so any lock update or file they write lands in the copy, which is
  removed after. The real `flake.lock` cannot be written. The journey fails when no test is found, when the full set
  is not the 19 `features/module-tests.md` states, or when a test changed the copy's `flake.lock`.

Every journey ends with `worktree untouched`: every file in the worktree, ignored files included and only git's own
metadata left out, has the same mode and SHA-256 after the run as before. A run that writes the lock, adds, removes
or changes any file, or edits further a file that was already dirty, fails. It detects; for `module-tests` the copy is
what prevents.

To check one module beyond the journeys, use the same shape the tests use: `nix eval --no-update-lock-file --json
'.#darwinConfigurations.studio.config.<option path>'`. Record the command and its output with the run.

## Evidence

Each run writes `$NIX_VERIFY_EVIDENCE/<UTC time>-<journey>/`:

| File | What it shows |
| --- | --- |
| `log.txt` | Each step, its outcome, time and detail (the `.drv` or store path, the test summary) |
| `result.json` | Each step's outcome, time and error; `passed` |
| `commands.jsonl` | Each command run: argv, exit code, seconds |
| `NN-<step>.out`, `NN-<step>.err` | That command's full stdout and stderr (Nix's error trace, the test's output) |
| `tree-before.txt`, `tree-after.txt` | Each worktree file's mode and SHA-256 before and after |
| `run.json` | The journey, the commit, whether `flake.lock` matched `HEAD`, the process ID and start time, the run directory, the throwaway copy, the status |
| `run.lock` | Held by the run's process while it runs; cleanup reads a free lock as "the process is gone" |

Proof standards:

- Verify the checkout as it is: the git flake sees tracked files with their working-tree contents. Report the commit
  from `run.json` and say whether the tree was dirty.
- A passing `build-darwin` proves the system closure builds. It proves nothing about activation: launchd agents
  loading, Homebrew installing, a service starting, or a port answering. Say so when you report, and leave activation
  to Bryan's `update-system`.
- gnarbox is evaluated, not built (see Gotchas). Do not report it as built.
- Report the run directory with every claim, and which journeys ran. A journey not run is not verified.

## Cleanup

```sh
$V cleanup                  # runs still marked running whose process is gone
$V cleanup <run directory>  # one run, stopping it if it is still in progress
```

Without an argument it marks each run whose `run.lock` no process holds `abandoned` and leaves runs in progress
alone, since another agent may own them. Named, the directory must be under the evidence root and outside the
repository, or cleanup refuses it (exit 2) and writes nothing. It then sends SIGTERM only to a process that matches
all three of: the PID in `run.json`, the start time recorded with it, and holding that directory's `run.lock` (checked
with `lsof`). A `run.json` that names another run directory is left alone. SIGTERM stops the run's Nix or test child,
removes its throwaway copy and finishes the run as failed; cleanup then marks it `abandoned`. It never kills by process
name, never signals a process that fails a check (it says which and exits 1), and never removes evidence.

The safety claims above have a self-test: `.agents/skills/verify/scripts/selftest` (about 5 s, scratch repositories
and processes only). It runs a test that rewrites `flake.lock` and checks the real one is untouched, an empty test
set, an ignored file and an already-dirty file changing, and decoy processes whose command lines match a run's: no
lock, a reused PID, a PID that does not hold the lock, a copied `run.json`, directories inside the repository and
outside the evidence root. Run it after any change to `nix-verify`. Nix leaves nothing else to clean: builds use `--no-link`, and store paths are collected by `nix store gc`
only when nothing roots them.

## Gotchas

- **Untracked files are invisible.** The flake is a git flake, so a new `.nix` file is ignored until `git add`, and
  `evaluate` passes without it. `doctor` warns about untracked `.nix` files. The module tests evaluate through a
  `path:` reference instead, so they do see them.
- **gnarbox cannot be built here.** It is `x86_64-linux`, this Mac has no Linux builder, and its closure was 1,049
  builds and 8 GB of downloads on 2026-11-16. Per `AGENTS.md`, the live Gnarbox runs Omarchy, not this flake, so
  `evaluate` keeps the configuration from rotting and nothing more.
- **`nix flake check --all-systems` fails on main** (2026-11-16, commit 21d51c1): the `x86_64-linux` dev shells pull
  `gtk+-2.24.33`, which the pinned nixpkgs marks broken (`modules/dev-envs/_nodeLib.nix`, `browserTestingDeps`). That is
  why `flake-check` checks this machine's system only. It is not a journey until the dev shells are fixed.
- **Two module tests fail on main** (2026-11-16, commit 21d51c1), unrelated to any change being verified:
  `test_dashy_status_api.py` (`test_a_pi_durable_link_follows_its_service`, turning planning off trips the
  `planning.modelProxy` assertion) and `test_pi_bots_service.py` (`test_pi_durable_agents_find_it` expects the
  `pi-durable-sampler` agent, which commit 21d51c1 turned off on Studio). So `$V run module-tests` fails on main.
  Compare a branch's failures with main's, or run the tests for the modules you touched by name.
- **`nixpkgs-fmt --check .` fails on main**: 17 of 52 files would be reformatted (nixpkgs-fmt 1.3.0). Check only the
  files you changed: `nix run --no-update-lock-file --inputs-from . nixpkgs#nixpkgs-fmt -- --check <files>`.
- **A break must hit what the host sets.** Studio sets `services.omlx.port` itself, so changing the option's default
  changes nothing `evaluate` or the tests see. A journey that passes on a break first needs the break checked.
- Python writes `__pycache__` into `scripts/` when a test is run by hand; `.gitignore` does not cover it. The journey
  runs in a copy with bytecode off. If you ran tests by hand, remove the `.pyc` files you made before committing.
- `worktree untouched` hashes every file in the worktree, so an editor or another agent writing in the same
  worktree during a run fails it. Don't edit while a run is going.
- A journey counts as proven only after it has been seen to fail on the break it exists to catch: break the file in the
  worktree, run the journey, record the failure, `git restore` the file. `features/README.md` lists the breaks.

## Other surfaces (not covered here)

- **Activation**: `darwin-rebuild switch` through `update-system`, on the host itself. Bryan's step, never an agent's.
- **Dev shells' contents**: `nix develop '.#<shell>'` builds and enters one; `flake-check` only evaluates them.
- **Running services**: whether a launchd agent answers after activation. That is the monitoring module's content
  probes on the live host, not this repository's checkout.

## Keeping it current

`features/` is the maintained list of what to verify. When a host, a check or a test is added, update the matching
feature file and the journey in the same change. `/maintain-verification-skill` audits the map against the repository.
