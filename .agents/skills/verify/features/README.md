# nix-configs: verification map

This directory is the maintained list of what to verify in this flake. Read this index before you run anything, then use
the matching feature file as the recipe. The harness is `nix-verify`, the control command in `../scripts/nix-verify`
(see `../SKILL.md`).

## Baseline preconditions

- `$V doctor` prints no `FAIL`. Its `untracked .nix` line is `none`, or the files it names are `git add`ed.
- The machine is `aarch64-darwin` with the Nix daemon running. Nothing else: no host is activated, no service started.
- `NIX_VERIFY_EVIDENCE`, if set, is outside the repository.

## Driving conventions

- Run the default set (`$V run`) for any Nix change. Add `module-tests` for the modules you touched, by name
  (`$V run module-tests -- <name>`), and the full set before a change to a shared module.
- Never pass a command that could write `flake.lock` or activate a system. The journeys pass `--no-update-lock-file`
  and `--no-link` and end by checking that the tree did not change.
- Compare failures with main's known ones (`../SKILL.md`, Gotchas) before calling a failure the change's.

## Proof and skip reporting

- Report the run directory, the journey and the commit from `run.json` with every claim.
- A journey not run is not verified. gnarbox is evaluated only; activation is never verified here.
- A failure that main shares is reported as main's, with the test name, not hidden and not blamed on the change.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph on what the feature gives a user of this repository. Four
H2 sections follow, in this order: `Sub-features`, `How to get to it (user POV)`, `Driving it with nix-verify`
(starting with `Preconditions:`) and `Gotchas`.

## Features

| Feature | Journey | Default | Last proved |
| --- | --- | --- | --- |
| [Host configurations evaluate](./host-evaluation.md) | `evaluate` | yes | 2026-11-16 |
| [Darwin systems build](./darwin-build.md) | `build-darwin` | yes | 2026-11-16 |
| [The flake's own check](./flake-check.md) | `flake-check` | yes | 2026-11-16 |
| [Module tests](./module-tests.md) | `module-tests` | no (~12 min) | 2026-11-16, on `omlx_module`; the full set fails on main (two tests) |

"Last proved" means the journey passed on commit 21d51c1 and failed on each break below, on that date.

## Breaks each journey catches

Each row is one temporary edit in the worktree, with the journey run against it and then the file restored with
`git restore`. A journey is proven only once it has failed on the break it exists to catch. Add a row whenever a
journey gains a claim.

| Break | File | Journey | Fails at |
| --- | --- | --- | --- |
| a6mbp's `environment.systemPackages` names a package nixpkgs lacks | `modules/hosts/a6mbp.nix` | `evaluate` | `evaluate a6mbp` |
| gnarbox's `environment.systemPackages` names a package nixpkgs lacks | `modules/hosts/gnarbox.nix` | `evaluate` | `evaluate gnarbox` |
| studio renamed out of `darwinConfigurations` | `flake.nix` | `evaluate` | `every modules/hosts/*.nix is a configuration` |
| `flake.nix` gains an input `flake.lock` lacks | `flake.nix` | `evaluate` | `list darwinConfigurations` ("requires lock file changes but they're not allowed"); `flake.lock` unchanged |
| a6mbp adds a `runCommand` that exits 1: evaluates, fails to build | `modules/hosts/a6mbp.nix` | `build-darwin` | `build a6mbp` |
| the vets-website dev shell names a package nixpkgs lacks | `modules/dev-envs/vets-website.nix` | `flake-check` | `nix flake check` |
| oMLX's agent binds `0.0.0.0`, not `127.0.0.1` | `modules/services/omlx.nix` | `module-tests -- omlx_module` | `test_omlx_module` (two tests) |
| the runner lets Python write bytecode into `scripts/` | `../scripts/nix-verify` | `module-tests -- check_github_status` | `worktree untouched` (before Correction 1; tests now run in a copy) |

Breaks of the harness itself are scripted in `../scripts/selftest` and run against scratch repositories, so nothing
here is edited by hand. Each was also run against the harness before Correction 1, which failed it:

| Break | Before Correction 1 | Now (`selftest` check) |
| --- | --- | --- |
| a repository test runs `nix flake lock` and rewrites `flake.lock` | the real lock rewritten, then reported | the real lock byte-for-byte unchanged; `tests left flake.lock alone` fails |
| `scripts/` holds no test | `module-tests` passed | "an empty test set fails" |
| the full set is not the stated count | passed | "a full set other than the stated count fails" |
| an ignored file changes; an already-dirty file is edited further | not seen (git status unchanged) | both seen by `worktree untouched` |
| an unrelated process whose command line names `nix-verify run`, recorded in a `run.json` | SIGTERMed by `cleanup <dir>` | survives bare and named cleanup |
| a reused PID (start time differs) or a recorded PID that does not hold `run.lock` | would be signalled | not signalled, reason printed |
| `cleanup` of a directory inside the repository, or outside the evidence root | `run.json` rewritten in the repository | refused, exit 2, nothing written |
| bare `cleanup`, `doctor` or `run` with the evidence root inside the repository (Correction 2) | bare `cleanup` rewrote the record there; `doctor` did not refuse | all three refused, exit 2, record untouched |
| a record whose `scratch` names a prefixed temporary directory it did not make (Correction 2) | the directory deleted | kept; also kept for another run's marker, a path outside the temporary directory, and a symlink |

A break that passed and was replaced: oMLX's port default changed from 8000 to 8001. Studio sets `port = 8000` itself,
so nothing any host builds changed; the break was wrong, not the journey.

Not mapped: gnarbox building (no Linux builder), `nix flake check --all-systems` (fails on main), activation, and the
dev shells' contents beyond evaluation.
