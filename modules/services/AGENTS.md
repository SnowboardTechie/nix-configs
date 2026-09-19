# Services Module

Darwin launchd services with NixOS stubs. Each service follows a consistent pattern.

## Pattern

Every service module defines **both** platform aspects:

```nix
{ inputs, ... }: {
  flake.modules.darwin.{name} = { config, lib, ... }: {
    options.services.{name} = {
      enable = lib.mkEnableOption "{description}";
      # Additional options with lib.mkOption...
    };
    config = lib.mkIf cfg.enable {
      launchd.user.agents.{name} = {
        serviceConfig = {
          ProgramArguments = [ "/opt/homebrew/bin/{tool}" ... ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/{name}.log";
          StandardErrorPath = "/tmp/{name}.error.log";
        };
      };
      # Firewall rules / setup steps — see "Activation scripts" below.
      system.activationScripts.extraActivation.text = lib.mkAfter ''
        # === {name} firewall ===
        ...
      '';
    };
  };
  flake.modules.nixos.{name} = { ... }: {
    # Stub or native NixOS module usage
  };
}
```

## Activation scripts — critical footgun

**nix-darwin's `system.activationScripts` is NOT arbitrary-attribute like NixOS.**
It composes a **fixed, hard-coded list** of named phases into the final activate
script (see `nix-darwin/modules/system/activation-scripts.nix`):

```
preActivation  →  checks  →  createRun  →  extraActivation  →  groups  →
users  →  applications  →  pam  →  patches  →  openssh  →  etc  →  defaults  →
userDefaults  →  launchd  →  userLaunchd  →  nix-daemon  →  time  →
networking  →  power  →  keyboard  →  fonts  →  nvram  →  homebrew  →
postActivation
```

Anything else — `ollama-firewall`, `monitoring-setup`, `foo-bar` — **is defined
in the option set, a derivation gets built for it, and then the name is never
referenced**. The activation runs your init/checks/etc but your custom block
silently does nothing.

**This bit us hard in April 2026**: ollama's auto-restart, all service firewall
registrations, and all setup-info echoes had been silently skipped for weeks.
The real damage was on open-webui, where a broken pip-user dep tree sent the
service into a 65-hour crash loop behind a Cloudflare tunnel.

**Correct pattern:** fold your scriptlet into `extraActivation.text` with
`lib.mkAfter`:

```nix
system.activationScripts.extraActivation.text = lib.mkAfter ''
  # === {name} firewall ===
  /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/bin/{tool} >/dev/null 2>&1 || true
  /usr/libexec/ApplicationFirewall/socketfilterfw --unblock /opt/homebrew/bin/{tool} >/dev/null 2>&1 || true
'';
```

Multiple modules can each contribute a `mkAfter` block — they concatenate
cleanly because `extraActivation.text` is a `lines`-type option.

`preActivation` and `postActivation` work the same way if you need
before/after ordering relative to the rest of the activate script.

**Do NOT use** `system.activationScripts.{my-custom-name}.text` — it compiles
but does not execute.

### Homebrew upgrades and launchd cdhash pins

The phase list above has a second edge. `homebrew` is **second to last**, so
anything that must react to a brew upgrade cannot live in `extraActivation`
near the start — it has to be `postActivation`.

This matters because launchd pins every job to the **cdhash** of the binary
present when the job was bootstrapped. Homebrew kegs are ad-hoc signed
(`Signature=adhoc`, TeamIdentifier not set), so every upgrade mints a fresh
cdhash. `homebrew.onActivation.upgrade` replaces the binary in place without
changing the plist, so nix-darwin's `userLaunchd` phase leaves the job alone
and the pin goes stale. The job then dies with `EX_CONFIG` (78) and
`job state = spawn failed` on its next respawn — permanently, because
`KeepAlive` just retries the same rejected exec. Diagnostic tell: the job's
`runs` counter climbs while its `StandardErrorPath` log stays untouched,
because the process never actually executes.

`launchctl kickstart` **cannot** repair this. It restarts a job without
re-registering it, so it never re-reads the plist or re-takes the cdhash.
Only `bootout` + `bootstrap` does.

`modules/base/homebrew.nix` handles this centrally in `postActivation`: it
derives the brew-backed agents by filtering `config.launchd.user.agents` for a
`/opt/homebrew` program path, then re-pins each one after the homebrew phase.
The filter sees evaluated paths, so an agent written as
`"${config.homebrew.prefix}/bin/foo"` is covered too. **Service modules must
not add their own restart block** — a module-local one runs in the wrong phase
and duplicates the shared work.

**Ollama lost ~85 minutes to this on 2026-09-19**: brew poured 0.34.2 at
10:37:23, the running 0.34.1 process died at 10:42, and launchd then failed to
spawn it 1034 times until the agent was booted out and bootstrapped. The
pre-existing mitigation was wrong twice over — it sat in `extraActivation`
(before the upgrade it meant to react to) and used `kickstart` (which cannot
re-pin). Fixed in `824b9ad`.

## Services

| Service | Port(s) | Binary | Scheduling | Notes |
|---------|---------|--------|------------|-------|
| ollama | 11434 | `/opt/homebrew/bin/ollama` | Always-on | Flash attention, q8_0 KV cache. Re-pinned after brew upgrades by the shared `postActivation` block in `base/homebrew.nix`, not by a module-local restart. Studio forwards the loopback listener tailnet-only through Tailscale Serve. |
| open-webui | 8080 | uv tool venv `~/.local/bin/open-webui` | Always-on + daily updater | Installed via `uv tool install`. Updater has an import probe — refuses to kickstart a broken install. |
| monitoring | 9090, 9093, 9100, 33000 (Studio Grafana; module default 3000), 3100, 12345, 9115 | prometheus, alertmanager, node_exporter, grafana, loki, alloy, blackbox_exporter | Always-on | Binds 0.0.0.0 (except Alertmanager — loopback-only, its silence API is unauthenticated); 7 agents; SMTP via smtp2go; alertmanager + blackbox_exporter via nixpkgs derivations; alloy replaced promtail (EOL March 2026). Grafana's reusable default is 3000, but `hosts/studio.nix` overrides `services.monitoring.grafana.port = 33000` to keep port 3000 free for development servers; the launchd `GF_SERVER_HTTP_PORT`, the Prometheus scrape target, and `services.dashy.grafanaBaseUrl` all derive from that one option. Alerts: Prometheus → Alertmanager → email. |
| syncthing | 8384, 22000 | `/opt/homebrew/bin/syncthing` | Always-on | NixOS uses native module directly |
| smb-mount | — | mount_smbfs | Event-driven (WatchPaths) | Soft mount, no polling |
| icloud-backup | — | /usr/bin/rsync | Calendar (2:00 AM) | Excludes .stversions/.syncthing* |
| obsidian-headless | — | nixpkgs `obsidian-headless` (`ob`) | Always-on after interactive setup | **Retired 2026-09-16 (no host enables it).** Synchronized `/Users/bryan/second-brain` on Studio and MBP until the personal second brain moved to Apple Notes. Module kept as the rollback path. |
| vault-git-backup | — | nixpkgs Git | Calendar (3:00 AM) | **Retired 2026-09-16 (no host enables it).** Studio-only snapshot of `/Users/bryan/second-brain` to `origin/main`; `scripts/test_vault_git_backup.py` still guards the script for rollback. |
| hindsight | 8888 (API), 9999 (Control Plane on `::1`), 9998 (IPv4 Control Plane compatibility proxy), 5433 (PostgreSQL) — loopback; 9443/9444 Tailscale Serve HTTPS | uv-locked venv `hindsight-api` + npm-locked Control Plane + Caddy compatibility proxy + nixpkgs postgresql_17+pgvector | Always-on + 6-hourly backup + monthly restore test | Named per-user instances (`services.hindsight.instances.bryan`). Locks in `hindsight-env/` (uv.lock + package-lock.json); service start applies them exactly (`uv sync --locked`, `npm ci`). Studio's `upgrade-system` refreshes coordinated Hindsight locks and takes a pre-upgrade backup before rebuilding; `update-system` applies committed versions only. Hindsight's packaged next-intl middleware requires the literal `localhost` server hostname for internal locale rewrites; on macOS that binds `::1`. The `9998` proxy provides Tailscale Serve an IPv4 loopback target, dials `::1`, and keeps internal rewrites on plain HTTP. Secrets read at exec time from `~/.secrets/hindsight-<name>/` — never in plists/derivations. Backups age-encrypted to declared public recipient; tiered retention 48h/14d/4w + pre-upgrade via `hindsight-<name>-backup-now pre-upgrade`. PostgreSQL major upgrades are explicit migrations, never routine rebuilds. |
| hermes | 443, 9119, 9120 (Studio Tailscale only) | Nix client package + isolated per-user managed runtimes + Caddy proxies | Always-on on Studio; Bryan updates nightly, Traci weekly | Studio runs Bryan's gateway/dashboard plus per-user headless remote backends. Nix owns launchd, permissions, ports, and Tailscale while each server runtime updates independently with a hard-gated state backup and clean-checkout check. Bryan's dashboard remains bound to its Tailscale IP so Hermes keeps its authentication gate enabled; Tailscale Serve and Caddy add HTTPS on port 443. Traci's backend remains loopback-only behind its own authenticated proxy. MBP is a native client; Linux clients are managed outside this repository. |
| dashy | 8088 (Studio Tailscale), 8089 (loopback status adapter) | Nixpkgs `dashy-ui` static build + Caddy + Python status adapter | Always-on | Private service homepage. Configuration is compiled into the immutable static build; mutable UI editing and upstream proxy endpoints are absent. Status dots read only the allowlisted Hermes watchdog state. |

## Where Enabled

Services are enabled per-host in `modules/hosts/{host}.nix`:
```nix
imports = [ ... syncthing ... ];
services.syncthing.enable = true;
```

Only **studio** enables the full stack (ollama, open-webui, monitoring, smb-mount, icloud-backup).
All darwin hosts enable **syncthing**.
No host enables **obsidian-headless** or **vault-git-backup** any more (retired 2026-09-16; the personal second brain is Apple Notes and `/Users/bryan/second-brain` is a frozen archive). Nothing may automatically commit, pull, push, or sync that archive.

## Alerting (monitoring module)

Pipeline: **Prometheus (rule eval) → Alertmanager (dedup/group/route) → smtp2go → email**

Host must set `services.monitoring.alertEmail = "you@example.com"` and must
have the SMTP password on disk:

```
~/.secrets/grafana-smtp-password   # smtp2go API password (reused by
                                   # Alertmanager via smtp_auth_password_file)
```

Alert rules live in Prometheus (`monitoring.nix`) as declarative PromQL under
version control. Alertmanager handles delivery via a simple declarative
receiver config.

Grafana is kept as pure visualization — it gets Alertmanager added as a
datasource so you can view alert state in its UI, but notifications go
through Alertmanager directly, not through Grafana. This avoids a real
incompatibility in Grafana 12.4's embedded Alertmanager: its `/api/v2/alerts`
endpoint expects a non-spec wrapped payload (`{alerts: [...]}`) while
Prometheus's client sends the canonical bare array per AM v2, causing 400
"cannot unmarshal array into PostableAlerts" on every real alert. Real
Alertmanager speaks canonical AM v2.

## Scheduling Patterns

Three launchd scheduling modes used (match existing when adding):

| Mode | Config | Used By |
|------|--------|---------|
| Always-on daemon | `RunAtLoad=true` + `KeepAlive=true` | ollama, open-webui, syncthing, monitoring |
| Event-driven one-shot | `RunAtLoad=true` + `KeepAlive=false` + `WatchPaths` | smb-mount |
| Calendar-scheduled | `StartCalendarInterval` only | icloud-backup, vault-git-backup |

## Obsidian Vault Sync and Backup (retired; rollback reference)

Retired 2026-09-16: neither module is enabled on any host. The description below is how they worked and what re-enabling would require. Obsidian Headless Sync and Git snapshots are separate layers:

- `obsidian-headless` provides live bidirectional synchronization through the existing Obsidian remote vault.
- `vault-git-backup` creates a nightly Studio-only Git snapshot and normally pushes it to `origin/main`.
- Desktop Obsidian may open the vault, but its core Sync plugin must be disabled before Headless Sync starts. Phone Sync remains enabled. Each Headless client must exclude the `core-plugin` config category so `core-plugins.json` remains device-specific; `core-plugin-data` may remain enabled. Automatic commit, pull, and push actions from the Obsidian Git plugin must also be disabled so Studio remains the only automated Git writer.
- Never pass account credentials, E2E passwords, or tokens on command lines. `ob login` and `ob sync-setup` prompt interactively and keep their state under `~/.obsidian-headless/`.

The Headless launcher waits five minutes between setup checks instead of exiting repeatedly before `ob login` and `ob sync-setup` are complete. It also refuses to start while `sync-status --json` reports `core-plugin` config syncing, preventing a phone's enabled desktop-plugin state from crossing onto a Headless Mac. Logs are `/tmp/obsidian-headless.log` and `/tmp/obsidian-headless.error.log`.

The Git backup exits successfully when there is nothing to commit. Before staging, it takes an atomic lock, rejects in-progress Git operations and a nonempty index, fetches, and requires local `HEAD` to equal `origin/main`. It validates the proposed snapshot through a temporary index, rechecks branch, HEAD, index, and tree state immediately before committing, runs `git diff --cached --check`, creates an unsigned unattended commit with `second-brain: automated vault snapshot YYYY-MM-DD`, and performs a normal push. Nix supplies Git LFS for the machine's global pre-push hook; push authentication still uses the existing machine-local SSH credentials. It never pulls, merges, rebases, amends, resets, resolves conflicts, or force-pushes. A failed push deliberately leaves the local commit for manual reconciliation. Logs are `/tmp/vault-git-backup.log` and `/tmp/vault-git-backup.error.log`.

## Anti-Patterns

- **NEVER** use arbitrary names for `system.activationScripts.{foo}.text` — see footgun above. Use `extraActivation.text = lib.mkAfter …` instead — **except** for work that must follow a Homebrew upgrade, which belongs in `postActivation` because the `homebrew` phase is second to last.
- **NEVER** add a module-local restart block for a brew-backed launchd agent. `base/homebrew.nix` re-pins them all in `postActivation`; a per-service block runs in the wrong phase and duplicates the shared work.
- **NEVER** use `launchctl kickstart` to pick up a replaced binary. It restarts a job without re-registering it, so it cannot refresh launchd's cdhash pin. Use `bootout` + `bootstrap`.
- **NEVER** point a boot-time system LaunchDaemon directly at `/nix/store`. The Nix volume may not be mounted when launchd first spawns it, causing `EX_CONFIG` and a persistent penalty-box state. Start through `/bin/sh` and wait for the real executable before `exec` (see `hermes.nix`).
- **NEVER** use Nix store paths for ProgramArguments for Homebrew packages — use `/opt/homebrew/bin/` (exception: tools not in Homebrew, like blackbox_exporter, use nixpkgs derivations).
- **ALWAYS** include both darwin and nixos aspects (even if nixos is a stub).
- User services log to `/tmp/{name}.log` and `/tmp/{name}.error.log` so alloy can discover them. Root LaunchDaemons must instead use a root-owned, non-world-writable directory under `/var/log` to prevent predictable-path symlink attacks.
- **NEVER** run desktop Obsidian Sync and `obsidian-headless` against the same local vault concurrently (rollback only; both retired).
- **NEVER** enable `vault-git-backup` on an Obsidian replica; if ever re-enabled, Studio is the sole Git writer for `second-brain`.
- **NEVER** re-enable either module for `/Users/bryan/second-brain` without reopening the 2026-09-16 Apple Notes decision; the archive is frozen.
- **NEVER** rely on `pip install --user` for background services. The transitive dep tree is un-pinned and can rot invisibly. Use `uv tool install` (isolated venv + lockfile) or a Nix-packaged equivalent.
- NixOS stubs marked `# TODO` are intentional — use native NixOS modules when implementing.
- open-webui depends implicitly on ollama via `ollamaUrl` default — no hard dependency declared.
