# Host configurations evaluate

Every host this flake exports, three nix-darwin Macs (mbp, a6mbp, studio) and the NixOS desktop gnarbox, evaluates to a
system derivation. This is the first thing `darwin-rebuild` or `nixos-rebuild` does, so a host that fails here cannot
be rebuilt at all.

## Sub-features

- `darwin-hosts`: each `darwinConfigurations.<host>.system.drvPath` evaluates to an existing `.drv`.
- `nixos-hosts`: `nixosConfigurations.gnarbox.config.system.build.toplevel.drvPath` evaluates, for `x86_64-linux`.
- `host-coverage`: every `modules/hosts/<name>.nix` has a configuration named `<name>` in the flake.
- `locked-inputs`: evaluation uses `flake.lock` as it is on disk and fails rather than updating it. Whether that lock
  matches `HEAD` is recorded, not asserted (a branch that updates the lock must still be verifiable): `run.json`'s
  `flake_lock`, the run's third line, and `doctor`'s `WARN`.

## How to get to it (user POV)

- Bryan's `update-system` runs `darwin-rebuild switch --flake .#<host>`, which evaluates the host first.
- An agent changing any module, host file, overlay or `flake.nix`.

## Driving it with nix-verify

Preconditions:

- `$V doctor` passes, with no untracked `.nix` file.

- **Evaluate.** Run `$V run evaluate`. One `evaluate <host>` step per host, each `ok` with its `.drv` path, about 40 s.
- **Coverage.** The step `every modules/hosts/*.nix is a configuration` lists each host with its output kind.
- **Lock.** The third line says `flake.lock matches HEAD` (or that it differs). `worktree untouched` says every file,
  `flake.lock` included, is unchanged.
- **Proof.** `log.txt` holds the four `.drv` paths; `commands.jsonl` the four `nix eval` calls, each with exit 0.

## Gotchas

- Evaluation succeeds for a module file git does not track, because the flake ignores the file. `doctor` warns.
- gnarbox's configuration is stale (the live machine runs Omarchy, see `AGENTS.md`), but it still evaluates, and this
  keeps it evaluating.
- An option with a default that a host overrides can be broken without any host changing. A break or a check of a
  module must touch what a host actually sets.
