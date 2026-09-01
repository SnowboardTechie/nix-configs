# Nix Configuration

Declarative, reproducible system configuration using Nix for macOS (nix-darwin) and NixOS. Includes cross-platform development environments for VA projects.

## Architecture

This repository uses a **dendritic (tree-like) modular architecture** with [flake-parts](https://github.com/hercules-ci/flake-parts) and [import-tree](https://github.com/vic/import-tree). Configurations are organized by **feature/capability** rather than by host:

```
modules/
├── base/       # Core system: fonts, homebrew, nix-settings, zsh
├── dev/        # Development: cli-tools, editors, git
├── desktop/    # GUI: gnome, gaming, audio (NixOS)
├── services/   # Daemons/agents: Hermes, Hindsight, Obsidian Sync/backup, ollama, monitoring, SMB, syncthing
├── hosts/      # Host-specific: a6mbp, gnarbox, mbp, studio (mbp/a6mbp/studio darwin, gnarbox NixOS)
└── dev-envs/   # VA project environments
```

Each host imports and composes feature modules. See [modules/README.md](modules/README.md) for detailed structure.

## Hosts

### mbp (personal macOS)

Personal MacBook Pro with syncthing, Tailscale, a Studio-backed Hermes client, Obsidian Headless Sync for the active `/Users/bryan/second-brain` vault, and personal apps (gaming, messaging, document tools). MBP is a Sync replica and must not automatically commit, pull, or push this vault.
**Location:** [`modules/hosts/mbp.nix`](modules/hosts/mbp.nix)

### a6mbp (work macOS)

Work MacBook Pro with syncthing and work tools (AWS, Docker, DDEV, Slack, Zoom).
**Location:** [`modules/hosts/a6mbp.nix`](modules/hosts/a6mbp.nix)

### studio (media server macOS)

Media server Mac running the primary Hermes gateway and per-user Tailscale-only remote backends, plus ollama, open-webui, monitoring (Prometheus + Grafana), SMB mount, syncthing, and iCloud backup. Prometheus and blackbox-exporter run as user-owned system LaunchDaemons so macOS Local Network Privacy cannot strand their LAN probes when Nix store identities change. They wait for the Nix volume before exec, while activation keeps monitoring state directories owned by the service user. Nix owns Hermes launchd supervision, permissions, ports, and Tailscale exposure; Bryan and Traci use isolated managed runtimes that update nightly and weekly, respectively. Actual updates announce their start and verified completion in Bryan's primary Matrix channel, while no-op checks stay silent. Bryan's primary backend is available at `https://bryans-mac-studio.tail5ba690.ts.net` through Tailscale Serve. Ollama remains bound to loopback and is forwarded tailnet-only at `http://100.121.238.48:11434`.
Traci's isolated headless backend runs under her macOS account and is available at `https://bryans-mac-studio.tail5ba690.ts.net:9120` through Tailscale Serve.

Studio also hosts the self-hosted [Hindsight](https://github.com/vectorize-io/hindsight) shared agent-memory service (bryan instance): dedicated PostgreSQL 17 + pgvector, a uv-locked API on loopback `8888`, and an npm-locked Control Plane on IPv6 loopback `9999`. The API is exposed tailnet-only at `https://bryans-mac-studio.tail5ba690.ts.net:9443` (bearer-authenticated); the key-authenticated Control Plane is exposed at `:9444` through an IPv4 loopback compatibility proxy on `9998` that preserves working locale rewrites behind Tailscale Serve. All extraction/consolidation runs through local Ollama. Six-hourly age-encrypted logical backups with tiered retention (48h/14d/4w + pre-upgrade) live under `~/.local/state/hindsight-bryan/backups/`, with a monthly disposable restore test; `hindsight-bryan-backup-now pre-upgrade` takes the mandatory pre-upgrade snapshot. Versions are pinned by `modules/services/hindsight-env/` lock files; `scripts/check-hindsight-releases.py` is the daily read-only Hermes release watch (register with `hermes cron add`, no-agent mode, workdir this repo). Secrets live in `~/.secrets/hindsight-bryan/` and never enter the store.

The private Studio service portal is available over Tailscale at `http://100.121.238.48:8088`. It uses the immutable Nixpkgs Dashy static build for links and a read-only local adapter for health state from the model-free Hermes service watchdog; it has no public ingress, arbitrary proxy, or mutable web configuration.

Studio and MBP use Obsidian Headless for live synchronization of `/Users/bryan/second-brain`. Separately, Studio is the sole Git writer and takes a conservative snapshot nightly at 03:00 local time before pushing normally to the existing `origin/main`. The Git job refuses divergent history, staged work, in-progress operations, or an existing backup lock; it never pulls or rewrites history.
**Location:** [`modules/hosts/studio.nix`](modules/hosts/studio.nix)

### gnarbox (NixOS desktop)

NixOS desktop with GNOME, gaming (Steam + Proton GE), PipeWire audio, Tailscale, and a Studio-backed Hermes client. Uses the unstable overlay for select packages.
**Location:** [`modules/hosts/gnarbox.nix`](modules/hosts/gnarbox.nix)

### Shared Configuration

All darwin hosts share common packages via feature modules. All hosts (including NixOS) share Nix packages for CLI tools, editors, and fonts.

Feature modules own both platform aspects: darwin uses `homebrew.brews`/`homebrew.casks`, NixOS uses `environment.systemPackages`. This keeps each capability self-contained.

- **CLI tools (both platforms):** [`modules/dev/cli-tools.nix`](modules/dev/cli-tools.nix)
- **Git tools (both platforms):** [`modules/dev/git.nix`](modules/dev/git.nix)
- **Editor tools (both platforms):** [`modules/dev/editors.nix`](modules/dev/editors.nix)
- **Homebrew infrastructure:** [`modules/base/homebrew.nix`](modules/base/homebrew.nix) (onActivation settings, taps, darwin-only items)
- **Font:** MesloLGS Nerd Font (see [`modules/base/fonts.nix`](modules/base/fonts.nix))

## Prerequisites

**Apple Silicon Macs (`mbp`, `a6mbp`, `studio`):** Install [Determinate Nix Installer](https://github.com/DeterminateSystems/nix-installer):

```bash
curl -fsSL https://install.determinate.systems/nix | sh -s -- install --determinate
```

**All macOS hosts also need Homebrew installed first.** nix-darwin's `homebrew` module manages your Brewfile; it does not install Homebrew itself. If `brew` isn't already on the machine:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**NixOS:** Nix comes pre-installed.

## Installation

Clone the repository:

```bash
git clone https://git.snowboardtechie.com/bryan/nix-configs.git ~/code/nix-configs
cd ~/code/nix-configs
```

Build and activate:

**macOS — Apple Silicon (`mbp`, `a6mbp`, `studio`):**

First-time bootstrap of nix-darwin (Determinate has flakes enabled already):

```bash
sudo nix run nix-darwin/master#darwin-rebuild -- switch --flake '.#mbp'  # or '.#a6mbp', '.#studio'
```

Subsequent rebuilds:

```bash
darwin-rebuild switch --flake '.#mbp'
```

**NixOS** (first build requires experimental features flag):

```bash
sudo nixos-rebuild switch --flake '.#gnarbox' --extra-experimental-features 'nix-command flakes'
```

## Usage

### Obsidian Headless migration

Live Obsidian synchronization and nightly Git backup are independent. Headless Sync runs on Studio and MBP; only Studio performs the Git backup. Do not use the stale Syncthing copy at `/Users/bryan/notes/second-brain`. Phone Sync remains enabled. The Mac Headless clients exclude the `core-plugin` category so the phone's enabled state and each Mac's disabled desktop state remain device-specific.

Perform these steps on one host at a time. Start with Studio, verify it fully, and then repeat on MBP:

1. In desktop Obsidian, open `/Users/bryan/second-brain`, disable the core **Sync** plugin, and quit Obsidian. Disable automatic commit, pull, and push actions in the Obsidian Git community plugin as well. Desktop Obsidian may be reopened after Headless Sync is healthy, but its Sync plugin must remain off.
2. Verify `.obsidian/core-plugins.json` contains `"sync": false`. Do not continue while it is `true`.
3. Apply the host configuration only after step 2: `darwin-rebuild switch --flake '~/code/nix-configs#studio'` on Studio or `darwin-rebuild switch --flake '~/code/nix-configs#mbp'` on MBP. The managed job waits without a crash loop while interactive setup is incomplete.
4. Log in without command-line credentials: `ob login`.
5. List existing remote vaults: `ob sync-list-remote`. Select the existing remote vault; do not run `ob sync-create-remote`.
6. Connect the existing local vault, allowing any E2E password to be prompted interactively: `ob sync-setup --vault "<existing-vault-name-or-id>" --path /Users/bryan/second-brain --device-name studio` on Studio, or use `--device-name mbp` on MBP. Do not use `--password`, reset history, or create a replacement remote.
7. Keep core-plugin activation state device-specific: `ob sync-config --path /Users/bryan/second-brain --configs "app,appearance,appearance-data,hotkey,core-plugin-data"`. Omitting `core-plugin` prevents the phone's enabled Sync state from overwriting the disabled Mac state, and prevents the Mac state from disabling Sync on the phone.
8. Verify configuration with `ob sync-status --path /Users/bryan/second-brain`; its `Configs` line must list `core-plugin-data` but must not list a standalone `core-plugin` entry.
9. Start the configured managed process immediately: `launchctl kickstart -k gui/$(id -u)/md.obsidian.headless-sync`.
10. Inspect `/tmp/obsidian-headless.log` and `/tmp/obsidian-headless.error.log`, then rerun `ob sync-status --path /Users/bryan/second-brain` and verify `.obsidian/core-plugins.json` still contains `"sync": false` before reopening desktop Obsidian.

Before Studio's first scheduled Git snapshot, review exactly which untracked, non-ignored files it would include:

```bash
git -C /Users/bryan/second-brain ls-files --others --exclude-standard
```

The nightly job logs to `/tmp/vault-git-backup.log` and `/tmp/vault-git-backup.error.log`. Any nonzero result requires manual investigation; do not respond by pulling, rebasing, resetting, or force-pushing automatically. MBP's `.git` directory remains untouched, but no agent or plugin on MBP may commit, pull, or push this vault.

Follow-up outside this repository: update `/Users/bryan/second-brain/AGENTS.md` so agents treat Studio as the sole Git writer, do not commit or push from MBP, and regard the nightly Studio snapshot as the fallback for uncommitted vault edits. That policy change belongs in the `second-brain` repository and is intentionally not part of this `nix-configs` change.

### Inkling-Small release watchdog

The tracked watchdog in [`scripts/check-inkling-small-release.py`](scripts/check-inkling-small-release.py) checks the official Thinking Machines Hugging Face namespace daily for a public, ungated Inkling-Small repository with actual weight files. It stays silent while the release is pending. Once weights appear, its Hermes cron job reviews the official model card, formats and sizes, Apple Silicon runtime support, quantized availability, licensing, and fit on Studio's M2 Ultra with 128 GB unified memory before sending one notification. The release remains pending until that review explicitly acknowledges it, so an interrupted review retries on the next daily run. The job never downloads weights, installs software, or changes the running Ollama/Open WebUI stack.

The recreatable job definition is [`scripts/inkling-small-release-watch.job.json`](scripts/inkling-small-release-watch.job.json). Runtime state is private under `~/.hermes/state/inkling-small-release-watch.json` and is not committed.

### Colibrì GLM-5.2 readiness watchdog

The Hermes watchdog checks grouped-quality Metal support, a complete compatible checkpoint, reproducible Ultra-class Mac Studio evidence, tool-calling stabilization, and blocking regressions before recommending a controlled GLM-5.2 proof of concept. The recreatable, read-only job definition is [`scripts/colibri-glm52-readiness-watch.job.json`](scripts/colibri-glm52-readiness-watch.job.json). Runtime notification state remains private under `~/.hermes/state/` and is not committed.

### GitHub incident recovery watchdog

The tracked monitor in [`scripts/check-github-status.py`](scripts/check-github-status.py) polls GitHub's official Statuspage summary every five minutes and emits a timestamp-free snapshot. Hermes suppresses unchanged ticks and uses the OpenAI Codex subscription only when the official status changes. This incident-specific finite watch stays silent for its initial baseline and source-health noise, notifies Bryan in Matrix when service meaningfully improves or worsens, and automatically stops after 36 checks (about three hours). The recreatable job definition is [`scripts/github-status-watch.job.json`](scripts/github-status-watch.job.json).

### Hindsight release compatibility watchdog

The deterministic watchdog in [`scripts/check-hindsight-releases.py`](scripts/check-hindsight-releases.py) compares the committed Hindsight API, Control Plane, and coding-agent pins with their latest public releases. A newer coding-agent package is not treated as actionable until the latest released `hindsight-api-slim` wheel proves that `UpdateNodeRequest` accepts knowledge-page trigger patches and `KnowledgeNode` reports the effective trigger. This keeps the known `coding-agents` 0.4.1 versus API 0.9.1 incompatibility silent while still notifying Bryan when a server release changes or the coordinated update gates pass. The job is read-only and never changes locks, installs packages, restarts services, or updates clients.

The recreatable no-agent job definition is [`scripts/hindsight-release-watch.job.json`](scripts/hindsight-release-watch.job.json). Runtime notification state remains private under `~/.hermes/state/hindsight-release-watch.json` and is not committed.

### Apply Changes

**macOS:**

```bash
darwin-rebuild switch --flake '~/code/nix-configs#mbp'
```

**NixOS:**

```bash
sudo nixos-rebuild switch --flake '~/code/nix-configs#gnarbox'
```

### Update Dependencies

```bash
nix flake update
# then rebuild using commands above
```

## Development Environments

Cross-platform development environments for VA projects:

- **vets-website:** Node 22.22.0, Yarn 1.x, Cypress → [vets-website](https://github.com/department-of-veterans-affairs/vets-website)
- **vets-api:** Ruby 3.3.6, PostgreSQL, Redis, Kafka → [vets-api](https://github.com/department-of-veterans-affairs/vets-api)
- **next-build:** Node 24, Yarn 3.x, Playwright → [next-build](https://github.com/department-of-veterans-affairs/next-build)
- **component-library:** Node 22, Yarn 4.x, Puppeteer → [component-library](https://github.com/department-of-veterans-affairs/component-library)
- **content-build:** Node 14.15.0, Yarn 1.x, Cypress → content-build
- **simpler-grants:** Node 20, Python 3.11, pnpm (corepack) → [simpler-grants-protocol](https://github.com/HHS/simpler-grants-protocol) *(Poetry must be installed separately via `brew install poetry` or `pipx install poetry`)*

**Activate manually:**

```bash
nix develop '~/code/nix-configs#vets-website'
```

Development environment definitions are located in `modules/dev-envs/`.

## OpenCode

The OpenCode CLI is installed via Homebrew (`opencode`) on darwin systems and via nixpkgs on NixOS. The OpenCode desktop app (`opencode-desktop` cask) is installed on `mbp`.

**Authentication:**

Authenticate your AI providers:

```bash
opencode auth login
```

**Usage:**

Start the CLI:

```bash
opencode
```

**Documentation:**
- [OpenCode](https://opencode.ai/)

## Resources

- [Nix Manual](https://nixos.org/manual/nix/stable/)
- [nix-darwin Documentation](https://daiderd.com/nix-darwin/manual/)
- [Nixpkgs Package Search](https://search.nixos.org/packages)
- [flake-parts Documentation](https://flake.parts/)

## 3 gits, one repo

This repository syncs to multiple remotes. The primary repository is at [git.snowboardtechie.com](https://git.snowboardtechie.com/bryan/nix-configs), with backups on [Codeberg](https://codeberg.org/SnowboardTechie/nix-configs) and [GitHub](https://github.com/bryan-thompsoncodes/nix-configs).

## License

This configuration is free to use and modify for your own purposes.
