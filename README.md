# Nix Configuration

Declarative, reproducible system configuration using Nix for macOS (nix-darwin) and NixOS. Includes cross-platform development environments for VA projects.

## Architecture

This repository uses a **dendritic (tree-like) modular architecture** with [flake-parts](https://github.com/hercules-ci/flake-parts) and [import-tree](https://github.com/vic/import-tree). Configurations are organized by **feature/capability** rather than by host:

```
modules/
├── base/       # Core system: fonts, homebrew, nix-settings, zsh
├── dev/        # Development: cli-tools, editors, git
├── desktop/    # GUI: gnome, gaming, audio (NixOS)
├── services/   # Daemons: ollama, open-webui, monitoring, smb-mount, syncthing, icloud-backup (Darwin)
├── hosts/      # Host-specific: a6mbp, gnarbox, inix, mbp, studio (mbp/a6mbp/studio/inix darwin, gnarbox NixOS)
└── dev-envs/   # VA project environments
```

Each host imports and composes feature modules. See [modules/README.md](modules/README.md) for detailed structure.

## Hosts

### mbp (personal macOS)

Personal MacBook Pro with syncthing and personal apps (gaming, messaging, document tools).
**Location:** [`modules/hosts/mbp.nix`](modules/hosts/mbp.nix)

### a6mbp (work macOS)

Work MacBook Pro with syncthing and work tools (AWS, Docker, DDEV, Slack, Zoom).
**Location:** [`modules/hosts/a6mbp.nix`](modules/hosts/a6mbp.nix)

### studio (media server macOS)

Media server Mac running the full service stack: ollama, open-webui, monitoring (Prometheus + Grafana), SMB mount, syncthing, and iCloud backup.
**Location:** [`modules/hosts/studio.nix`](modules/hosts/studio.nix)

### iNix (Intel macOS)

Garage and shop machine for reading manuals, project plans, and occasional remote work. Runs on a 2017 iMac Pro (Intel Xeon W) with macOS + nix-darwin. Includes syncthing.
**Location:** [`modules/hosts/inix.nix`](modules/hosts/inix.nix)

### gnarbox (NixOS desktop)

NixOS desktop with GNOME, gaming (Steam + Proton GE), and PipeWire audio. Uses the unstable overlay for select packages.
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

**Intel Mac (`inix`):** Determinate Systems lists `x86_64-darwin` as unsupported. Use the official upstream Nix installer instead (multi-user is the default on macOS):

```bash
curl -L https://nixos.org/nix/install | sh
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

**macOS — Intel (`inix`):**

The upstream Nix installer doesn't enable flakes by default, so do that first (one-time):

```bash
mkdir -p ~/.config/nix
echo "experimental-features = nix-command flakes" >> ~/.config/nix/nix.conf
```

Then bootstrap nix-darwin:

```bash
sudo nix run nix-darwin/master#darwin-rebuild -- switch --flake '.#inix'
```

Subsequent rebuilds:

```bash
darwin-rebuild switch --flake '.#inix'
```

**NixOS** (first build requires experimental features flag):

```bash
sudo nixos-rebuild switch --flake '.#gnarbox' --extra-experimental-features 'nix-command flakes'
```

## Usage

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

## OpenCode & oh-my-opencode

OpenCode is installed via Homebrew on all darwin systems. The oh-my-opencode plugin is automatically installed on rebuild via the shared activation module (`modules/base/activation.nix`).

**Installation:**

The oh-my-opencode plugin installs automatically on the first rebuild after adding the configuration. Run:

```bash
darwin-rebuild switch --flake '.#mbp'
```

**Authentication:**

After installation, authenticate your AI providers:

```bash
opencode auth login
```

Follow the prompts to authenticate with:
- Anthropic (Claude) - select "Claude Team"
- OpenAI (ChatGPT) - select "ChatGPT Team"

**Usage:**

Start OpenCode:

```bash
opencode
```

The oh-my-opencode plugin provides:
- **Sisyphus** - Main orchestrator agent with specialized subagents
- **Multi-model support** - Claude, ChatGPT, Gemini
- **Enhanced tools** - LSP, AST grep, background agents
- **Productivity hooks** - Todo enforcement, comment checker, context management

Include `ultrawork` in your prompts for maximum parallel agent performance.

**Documentation:**
- [OpenCode Repository](https://github.com/opencode-ai/opencode)
- [oh-my-opencode Repository](https://github.com/code-yeongyu/oh-my-opencode)

## Resources

- [Nix Manual](https://nixos.org/manual/nix/stable/)
- [nix-darwin Documentation](https://daiderd.com/nix-darwin/manual/)
- [Nixpkgs Package Search](https://search.nixos.org/packages)
- [flake-parts Documentation](https://flake.parts/)

## 3 gits, one repo

This repository syncs to multiple remotes. The primary repository is at [git.snowboardtechie.com](https://git.snowboardtechie.com/bryan/nix-configs), with backups on [Codeberg](https://codeberg.org/SnowboardTechie/nix-configs) and [GitHub](https://github.com/bryan-thompsoncodes/nix-configs).

## License

This configuration is free to use and modify for your own purposes.
