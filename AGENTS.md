# Agent Guidelines for nix-configs

**Generated:** 2026-02-12 | **Commit:** df32e01 | **Branch:** main

## Overview

Declarative system configs for 3 macOS hosts (nix-darwin) + 2 NixOS desktops. Uses **flake-parts** + **import-tree** for automatic module discovery.

### Dendritic Architecture

This repo follows a **dendritic (tree-like)** organization: code is organized by **feature/capability**, not by host.

**Why dendritic?** The alternative — per-host config files — leads to duplication. If three macOS hosts all need zsh, you'd copy zsh config into each host file. With dendritic, `zsh.nix` exists once as a feature module, and hosts opt in via imports.

**Mental model:**
- **Branches** = feature modules (`base/`, `dev/`, `services/`, `desktop/`) — self-contained, reusable capabilities
- **Leaves** = host modules (`hosts/`) — compositions that import branches, plus host-specific overrides
- **Roots** = `flake.nix` + `_options.nix` — wiring that makes import-tree auto-discover everything

A new capability always goes on a **branch**. A host-specific setting goes on a **leaf**. This is the single most important architectural decision when adding code.

**Core invariants:**
1. **Feature modules are host-agnostic** — they never assume which host they run on
2. **Every module defines both platforms** — `flake.modules.darwin.{name}` and `flake.modules.nixos.{name}`, even if one is a stub. **Exceptions:** platform-exclusive modules that have no cross-platform equivalent: `homebrew.nix` (darwin-only, NixOS has no Homebrew), `gnome.nix`/`gaming.nix`/`audio.nix` (NixOS-only desktop features with no darwin equivalent). Do NOT add empty stubs to these — it's intentional.
3. **Hosts are compositions, not implementations** — they import features and set `enable` flags, they don't implement capabilities inline
4. **No duplication across hosts** — if 2+ hosts need it, it's a feature module

## Structure

```
nix-configs/
├── flake.nix              # Entry point: flake-parts + import-tree
├── modules/
│   ├── _options.nix       # Declares flake.modules option for import-tree merging
│   ├── base/              # Core: fonts, homebrew, nix-settings, zsh, activation
│   ├── dev/               # Dev tools: cli-tools, editors, git
│   ├── desktop/           # NixOS-only: gnome, gaming, audio
│   ├── services/          # Daemons: ollama, open-webui, monitoring, smb-mount, syncthing, icloud-backup
│   ├── hosts/             # Host compositions: a6mbp, mbp, studio, gnarbox, inix
│   └── dev-envs/          # VA project shells (see dev-envs/AGENTS.md)
├── overlays/              # Single overlay: nixpkgs-unstable → pkgs.unstable
└── hardware-configs/      # Auto-generated NixOS hardware config
```

## Where Does New Code Belong?

This is the key judgment call. Use this decision tree:

```
Is this a host-specific setting (only one host needs it)?
  YES → modules/hosts/{host}.nix (a leaf)
  NO  → Is it a background service/daemon?
          YES → modules/services/{name}.nix (see services/AGENTS.md)
          NO  → Is it a NixOS desktop feature?
                  YES → modules/desktop/{name}.nix
                  NO  → Is it a dev tool or editor config?
                          YES → modules/dev/ (cli-tools.nix, editors.nix, or new file)
                          NO  → modules/base/ (core system capability)
```

**Common mistakes:**
- Adding a package to a host file when 2+ hosts will need it → feature module instead
- Implementing a capability inline in a host file → extract to a branch module
- Creating a service module without both `darwin` and `nixos` aspects → always define both

**Edge cases:**
- A Homebrew cask used on only one host → still goes in `modules/hosts/{host}.nix` under `homebrew.casks`
- A shared CLI tool → goes in the relevant feature module (`cli-tools.nix`, `git.nix`, etc.), not `homebrew.nix`
- A package needed by one host today but likely others later → feature module now, don't wait

## Where to Look

| Task | Location | Notes |
|------|----------|-------|
| Add/remove shared CLI tool | `modules/dev/cli-tools.nix` | Darwin: `homebrew.brews`; NixOS: `environment.systemPackages` |
| Add/remove git/editor tool | `modules/dev/git.nix`, `modules/dev/editors.nix` | Same dual-platform pattern as cli-tools |
| Add/remove homebrew infrastructure | `modules/base/homebrew.nix` | Infrastructure only (onActivation, taps) + darwin-only items (ca-certificates) |
| Add host-specific package | `modules/hosts/{host}.nix` | Update README host section |
| Add new service | `modules/services/` | See `services/AGENTS.md` for pattern |
| Add dev environment | `modules/dev-envs/` | See `dev-envs/AGENTS.md` for pattern |
| Change editor config | `modules/dev/editors.nix` | Vim, Neovim |
| Modify shell/zsh | `modules/base/zsh.nix` | |
| Add NixOS desktop feature | `modules/desktop/` | gnome.nix, gaming.nix, audio.nix |
| Change activation scripts | `modules/base/activation.nix` | Alacritty symlink + oh-my-opencode install |
| Use unstable package | Reference as `unstable.{pkg}` | Overlay in `overlays/default.nix`, enabled on gnarbox |

## Build/Lint/Test Commands

```bash
# macOS
darwin-rebuild switch --flake '.#mbp'     # or a6mbp, studio
# NixOS
sudo nixos-rebuild switch --flake '.#gnarbox'
sudo nixos-rebuild switch --flake '.#inix'
# Validate
nix flake check
# Format
nixpkgs-fmt .
# Update inputs
nix flake update
# Dev shells
nix develop '.#vets-website'              # or vets-api, next-build, etc.
```

## Architecture Pattern: import-tree Module Merging

Every `.nix` file under `modules/` is auto-imported by import-tree. Each module contributes to `flake.modules.{darwin|nixos}.{name}`:

```nix
# Pattern for feature modules (e.g. services/ollama.nix)
{ inputs, ... }: {
  flake.modules.darwin.ollama = { config, lib, ... }: {
    options.services.ollama = { ... };    # Declare options
    config = lib.mkIf cfg.enable { ... }; # Implement when enabled
  };
  flake.modules.nixos.ollama = { ... }: {
    # NixOS aspect (or stub)
  };
}

# Pattern for host modules (e.g. hosts/a6mbp.nix)
{ inputs, ... }: {
  flake.modules.darwin.a6mbp = { pkgs, ... }: {
    imports = with inputs.self.modules.darwin; [
      fonts nix-settings zsh homebrew editors git cli-tools activation
      syncthing  # services
    ];
    # Host-specific config...
  };
}

# Pattern for dev-envs (e.g. dev-envs/vets-website.nix)
{ inputs, ... }: {
  perSystem = { pkgs, ... }: {
    devShells.vets-website = pkgs.mkShell { ... };
  };
}
```

## Conventions

- **Formatter:** `nixpkgs-fmt`
- **Naming:** camelCase variables, module filenames are kebab-case
- **Shared lib:** `_nodeLib.nix` prefix = internal shared module (not a flake output)
- **Imports:** Group by type — base features, then services, then host-specific
- **Host structure:** `=== Section ===` comment headers for Core, Services, Packages, Homebrew
- **Darwin services:** Use `lib.mkEnableOption` + `lib.mkIf cfg.enable` pattern
- **Homebrew binaries:** Darwin services reference `/opt/homebrew/bin/{tool}` directly
- **Dual-platform packages:** Feature modules own both platform aspects. Darwin uses `homebrew.brews`/`homebrew.casks`; NixOS uses `environment.systemPackages`. `homebrew.nix` is infrastructure-only (onActivation settings, taps, darwin-only items like ca-certificates).
- **Unstable packages:** Only available on gnarbox via overlay; use `unstable.{pkg}`

## Anti-Patterns

- **NEVER** edit `hardware-configs/gnarbox.nix` — auto-generated by `nixos-generate-config`
- **NEVER** hardcode platform paths — use `pkgs.stdenv.isDarwin` / `pkgs.stdenv.isLinux`
- **NEVER** add packages to host files when they should go in feature modules
- **ALWAYS follow the dendritic pattern** — even when the user specifies host files, if a package belongs on 2+ hosts, add it to the appropriate feature module (`cli-tools.nix`, `editors.nix`, etc.) instead. Ask the user if unsure, but default to the feature module.
- **ALWAYS** update README.md when changing: services, dev-env versions (package lists are NOT in README — code is the source of truth)
- Insecure `openssl-1.1.1w` is permitted on gnarbox only (for sublime4) — tracked upstream issue
- gnarbox uses LTS kernel 6.6 — latest (6.18) breaks xpad-noone driver
- npm lifecycle scripts disabled by default (`npm_config_ignore_scripts=true`) for security

## Documentation Sync

README.md is kept **high-level** — it describes architecture and host purposes, not package lists. Code is the source of truth for specific packages.

| Change | README Section |
|--------|----------------|
| Service module in `modules/services/` | Architecture tree + host sections |
| Host packages in `modules/hosts/*.nix` | Host section (mbp, a6mbp, studio, gnarbox, inix) |
| Dev-env versions in `modules/dev-envs/*.nix` | "Development Environments" |

Verify with:
```bash
grep -E "services\." modules/hosts/*.nix
```

## Gotchas

- `_options.nix` must exist — declares the `flake.modules` option type that import-tree merges into
- gnarbox hardware config lives outside `modules/` because import-tree can't handle `modulesPath`
- `hardware-configs/inix.nix` is a placeholder — run `nixos-generate-config` on the iMac Pro and copy the output to replace it, or use: `scp inix:/etc/nixos/hardware-configuration.nix hardware-configs/inix.nix`
- Darwin services require the tool installed via Homebrew first (Nix packages alone aren't enough for launchd)
- NixOS stubs exist in service modules (`# TODO: Implement NixOS equivalent`) — these are intentional placeholders
- oh-my-opencode auto-installs on activation if not present (see `activation.nix`)
- 3 git remotes: primary on git.snowboardtechie.com, mirrors on Codeberg and GitHub
