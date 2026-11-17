# Darwin systems build

Each nix-darwin host's system closure builds into the Nix store: every package, launchd plist, script and `/etc` file
it declares. This is what `darwin-rebuild switch` builds before it activates. The journey stops before activation.

## Sub-features

- `build`: `nix build --no-link .#darwinConfigurations.<host>.system` succeeds for mbp, a6mbp and studio.
- `shape`: the output holds `activate`, `sw/bin` and `darwin-version.json`, so it is a system, not an empty path.
- `no-activation`: nothing runs the `activate` script; no `result` link is written.

## How to get to it (user POV)

- `update-system` on a Mac builds its host's system, then activates it. This journey is the first half, for every host.

## Driving it with nix-verify

Preconditions:

- `$V doctor` passes and reports `system: aarch64-darwin`.

- **Build.** Run `$V run build-darwin`. One `build <host>` step per darwin host, each `ok` with its store path. About
  2 s when the store holds every path; after a `flake.lock` change it builds or downloads what is missing.
- **Proof.** `log.txt` holds the three store paths. `ls <path>` shows `activate`; do not run it.
- **No link.** `worktree untouched` passes, and `ls result*` in the repository finds nothing.

## Gotchas

- A passing build proves nothing about activation: Homebrew's casks and brews, launchd loading an agent, a service
  answering. Those happen only in `update-system` on the host.
- Homebrew packages are not in the closure. A typo in `homebrew.brews` builds fine and fails only at activation.
- gnarbox is not built: `x86_64-linux` and no Linux builder on this Mac.
