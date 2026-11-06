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
| ollama | 11434 | `/opt/homebrew/bin/ollama` | Always-on | Flash attention, q8_0 KV cache. Re-pinned after brew upgrades by the shared `postActivation` block in `base/homebrew.nix`, not by a module-local restart. Studio forwards the loopback listener tailnet-only through Tailscale Serve. `OLLAMA_ORIGINS` stays at Ollama's localhost-only default: its clients call the API server-side, and `*` let any web page drive the unauthenticated API. |
| open-webui | 8080 | uv tool venv `~/.local/bin/open-webui` | Always-on + daily updater | Installed via `uv tool install`. Updater has an import probe — refuses to kickstart a broken install. Binds `host` (default 127.0.0.1, passed as `serve --host` because `serve` ignores `HOST`); the Cloudflare Tunnel connector on the same host is its only client, so a loopback bind gets no firewall rule. |
| monitoring | 9090, 9093, 9100, 33000 (Studio Grafana; module default 3000), 3100, 9095, 12345, 9115, UDP 1514 | prometheus, alertmanager, node_exporter, grafana, loki, alloy, blackbox_exporter | Always-on | Every listener binds 127.0.0.1 except Alloy's Unraid syslog receiver (UDP 0.0.0.0:1514, the only firewall rule). Loki and Alertmanager are unauthenticated and blackbox's `/probe` fetches any URL, so they are never published. With `tailscaleServe` (Studio), Grafana, Prometheus and the Alloy UI are published tailnet-only over HTTPS on their own port numbers; `tailscaleServe.hostName` sets Grafana's root URL, Prometheus's external URL and (in `hosts/studio.nix`) the Dashy links. 7 agents; SMTP via smtp2go; alertmanager + blackbox_exporter via nixpkgs derivations; alloy replaced promtail (EOL March 2026). Grafana's reusable default is 3000, but `hosts/studio.nix` overrides `services.monitoring.grafana.port = 33000` to keep port 3000 free for development servers; the launchd `GF_SERVER_HTTP_PORT`, the Prometheus scrape target, and `services.dashy.grafanaBaseUrl` all derive from that one option. Alerts: Prometheus → Alertmanager → email, plus a token-authenticated webhook when `alertWebhook` is set (Studio: Hermes → encrypted Matrix room). Listener exposure (with Open WebUI and Ollama) is tested by `python3 scripts/test_studio_listener_exposure.py`. |
| fseventsd-watchdog | — | `scripts/fseventsd-watchdog.sh` (`/bin/sh`, `ps`, `pgrep`, `kill`) from a root LaunchDaemon (`org.nixos.fseventsd-watchdog`) | `StartInterval` (60 s) | **Studio, from 2026-11-02**, after fseventsd grew to 12 GB and a full core over 32 days of uptime and froze Claude Code (every new file watch waited on it). Each check writes fseventsd's memory and CPU to `/var/db/fseventsd-watchdog/metrics/fseventsd.prom` for node_exporter's textfile collector, and restarts fseventsd (SIGTERM, SIGKILL after 10 s; launchd relaunches it) once it stays over 2 GiB or 90% of a core for 15 checks in a row, at most once an hour (`cooldownSeconds`). Root because only root can signal fseventsd. State in `/var/db/fseventsd-watchdog/state` (0600); log `/var/log/fseventsd-watchdog.log`, one line per event. With the monitoring module it adds, through `extraAlertRules`, `extraDashboards` and `nodeExporter.textfileDirectories`: `FseventsdHighMemory` (> 1 GiB for 10m), `FseventsdHighCpu` (> 80% for 10m), `FseventsdRestartedByWatchdog` (every restart, with its reason), `FseventsdDown` (critical) and `FseventsdWatchdogSilent`, all delivered by email and to the Studio Alerts Matrix room, plus the "fseventsd" Grafana dashboard (uid `fseventsd`; its threshold lines follow the options). It cannot tell which app floods fseventsd; that needs root `fs_usage`. Rollback: `enable = false` and `update-system`; `/var/db/fseventsd-watchdog` stays. Tests: `python3 scripts/test_fseventsd_watchdog.py` (the script against stand-in `pgrep`/`ps`/`kill` and a fake clock, never the real fseventsd; Studio's daemon, node_exporter and Grafana wiring; `promtool` checks of the metrics and `promtool test rules` for each alert). |
| syncthing | 8384, 22000 | `/opt/homebrew/bin/syncthing` | Always-on | NixOS uses native module directly |
| smb-mount | — | mount_smbfs | Event-driven (WatchPaths) | Soft mount, no polling |
| icloud-backup | — | /usr/bin/rsync | Calendar (2:00 AM) | Excludes .stversions/.syncthing* |
| hermes-nas-backup | — | `scripts/hermes-nas-backup.sh` (`/usr/bin/sqlite3`, nixpkgs `zstd`) | Calendar (3:00 AM, before the 04:00 Hermes update) | **Studio, from 2026-11-02.** Copies Bryan's `~/.hermes` state to `/Volumes/Bryan/Backups/hermes/<UTC stamp>/`: `state.db.zst` (SQLite online backup, `quick_check` must say `ok`), `files.tar.zst` (memories, `cron/jobs.json`, `skills/` with symlinks kept as links, and `config.yaml.redacted`, whose secret/token/password/API-key values are replaced) and `MANIFEST.json` (checksums, session and message counts). Never `.env` or `auth.json`. Built and checked on local disk (`~/.local/state/hermes-nas-backup`), copied to `.partial-<stamp>`, read back and checksummed, then renamed into place. Refuses unless `mountPoint` is in the mount table as smbfs and sits on a different device from its parent, so an unmounted share is a failure, not a local "backup". Keeps the newest 7; retention runs only after a publish and deletes only stamp-named directories. Failures, and the first success after one, go to Matrix through `hermes send`; the last result is in `~/.local/state/hermes-nas-backup/last-run.json`. Nix doesn't mount this share. Restore: `zstd -d state.db.zst`, then put it in place with Hermes stopped. Tests: `python3 scripts/test_hermes_nas_backup.py`. |
| obsidian-headless | — | nixpkgs `obsidian-headless` (`ob`) | Always-on after interactive setup | **Retired 2026-09-16 (no host enables it).** Synchronized `/Users/bryan/second-brain` on Studio and MBP until the personal second brain moved to Apple Notes. Module kept as the rollback path. |
| vault-git-backup | — | nixpkgs Git | Calendar (3:00 AM) | **Retired 2026-09-16 (no host enables it).** Studio-only snapshot of `/Users/bryan/second-brain` to `origin/main`; `scripts/test_vault_git_backup.py` still guards the script for rollback. |
| hermes | 443, 9119, 9120 (Studio Tailscale only) | Nix client package + isolated per-user managed runtimes + Caddy proxies | Always-on on Studio; Bryan updates nightly, Traci weekly | Studio runs Bryan's gateway/dashboard plus per-user headless remote backends. Nix owns launchd, permissions, ports, and Tailscale while each server runtime updates independently with a hard-gated state backup and source-drift check (only the exact reviewed classic status repair, Matrix-through-gateway patch (#191's) and Rex program-status patch are permitted, see [Hermes local runtime patches](#hermes-local-runtime-patches); shallow checkouts are deepened first). Updaters announce failures and refusals as well as success, and headless updates restart that user's gateway daemon too. Bryan's dashboard remains bound to its Tailscale IP so Hermes keeps its authentication gate enabled; Tailscale Serve and Caddy add HTTPS on port 443. Traci's backend remains loopback-only behind its own authenticated proxy. MBP and Gnarbox are clients; Gnarbox's CLI/Desktop packages come from the NixOS aspect of this module. |
| dashy | 8088 (loopback portal; Studio publishes it tailnet-only over HTTPS on the same port through Tailscale Serve via `tailscaleHttpsPort`), 8089 (loopback status adapter) | Nixpkgs `dashy-ui` static build + Caddy + Python status adapter | Always-on | Private service homepage at `https://<tailnet name>:8088` on Studio (plain HTTP on its tailnet address until 2026-11-03). Configuration is compiled into the immutable static build; mutable UI editing and upstream proxy endpoints are absent. `tailscaleHttpsPort` requires `host = "127.0.0.1"`; a loopback portal gets no firewall rule. `piDurableLinks` adds a Pi Durable section, one link per workspace page, whose URLs Studio derives from `services.pi-durable`'s Serve ports; a new workspace is one entry there plus a watchdog probe of the same name. Status dots read only the allowlisted Hermes watchdog state (`~/.hermes/scripts/check-studio-services.py`), keyed by probe name, and turn red only once the watchdog has alerted (3 failed checks in a row); below that a dot stays green and its tooltip counts the failures. Dashy checks every tile at once on page load, so the adapter's listen backlog is 128: Python 3.14's default of 5 reset most of the burst and drew random red dots. Tests: `python3 scripts/test_studio_listener_exposure.py` and `scripts/test_dashy_status_api.py`. |
| artifact-shelf | 8092 (loopback Caddy; Studio publishes it tailnet-only over HTTPS on the same port through Tailscale Serve via `tailscaleHttpsPort`) | Nixpkgs `caddy` (`file_server`) + the stdlib-only `artifact-shelf.py` CLI on Nix's `python3Minimal`, in `environment.systemPackages` | Always-on server; index agent on `RunAtLoad`, `WatchPaths` (`items/`, `meta/`) and `StartInterval` (600 s) | **Studio, from 2026-11-06.** One permanent page for the visual artifacts Hermes prepares, each at `<baseUrl>/<slug>/` (slugs `YYYY-MM-DD-name`). Root `~/Library/Application Support/Artifact Shelf/`: `items/` served (copied directories, or symlinks with `--link`, which is refused for sources under `~/.hermes/cache/scratch` or the system temp dirs), `meta/<slug>.json` and `retired/` not served; the CLI creates them as the user. CLI: `artifact-shelf publish DIR --slug --title --project --description [--link] [--entry] [--replace]`, `touch`, `retire` (moves the item, never following a link), `list [--json]` (status `ok`, `missing`, or `unsafe` for a linked item whose tree has since gained a symlink that leaves it, is broken or loops), `url`, `index`. The index page shows only `ok` items (entry file present; linked trees re-walked on each rebuild, though Caddy would still serve an unsafe path asked for directly, so `publish` refuses such links up front), newest first, with Recent and a collapsed Older section (`olderAfterDays`, 30), project chips, and `<meta name="generator" content="artifact-shelf">` for the Hermes watchdog probe of the same name; it is rewritten (temp file + rename) only when its content changes, so the agent's own write doesn't retrigger `WatchPaths`. Every response carries `X-Robots-Tag: noindex, nofollow, noarchive`, `nosniff`, `no-referrer` and `Cache-Control: no-cache`; no CSP, since artifacts carry inline scripts; dotfiles are hidden; no directory browsing. Host must be loopback (assertion); no firewall rule; never Funnel. Dashy shows it through `dailyLinks`. Tests: `python3 scripts/test_artifact_shelf.py` and `python3 scripts/test_studio_listener_exposure.py`. |
| summit-point-gallery | 4321 (loopback only) | Nixpkgs `caddy` serving a staged static build | Always-on | Unlisted concept preview for the Summit Point website (repo `bryan/summit-point-site`). Exactly one hostname, `summitpoint.thompson.codes`, reaches the origin through the existing remotely managed Cloudflare Tunnel; Caddy answers any other Host header with a 404, and the tunnel holds no path rules. Serves the `current` symlink under `~/Library/Application Support/Thompson Codes/Summit Point Gallery/`, which always points at an immutable `releases/<sha>/` directory — never a working tree's `dist/`. Publish and roll back with the site repo's `scripts/publish-review.sh` (`--rollback <sha>` is an atomic symlink change, no rebuild, no activation). Every response carries `X-Robots-Tag: noindex, nofollow, noarchive`, `Cache-Control: no-store`, `nosniff`, `no-referrer`, a restrictive `Permissions-Policy`, and a static-site CSP that refuses framing. Loopback-only and deliberately NOT registered with the macOS firewall. Astro's dev server owns 4322 and must never be tunneled (enforced by a module assertion). |
| pi-durable | 8790 (loopback page); with `sampler.enable`: 8791 (the sampler's loopback page); when configured: 8888 (tinyproxy on the sandbox network's gateway); with `modelProxy.enable`: 8892 (the model proxy on the same gateway); with `githubProxy.enable`: 8893 (the GitHub write proxy's push and signing listener on the same gateway, once its config has a `push` block); and one Tailscale Serve HTTPS port per page | Homebrew `node` running `~/.local/share/pi-durable/sgg-deployment/dist/workspaces/sgg/src/main.js run` and, with `sampler.enable`, `.../dist/workspaces/sampler/src/main.js run --config ~/.config/pi-durable/sampler.json`; with `modelProxy.enable`, nixpkgs `nodejs-slim_24` running `.../dist/workspaces/proxies/src/model-main.js run --config ~/.config/pi-durable/model-proxy.json`; with `githubProxy.enable`, the same Node running `.../dist/workspaces/proxies/src/github-main.js run --config ~/.config/pi-durable/github-proxy.json`, with nixpkgs `git` and `openssh` on its `PATH`; nixpkgs `tinyproxy`; `/sbin/pfctl` (root, anchor `com.apple/100.pi-durable-sgg`) | Always-on when enabled; pf loader at activation and at boot | **Enabled on Studio after commissioning (2026-10-16).** Dedicated clean deployment checkout is declared; prepare its verified build before Bryan's `update-system` activation. Until then the installed agent retains the original source checkout path. Secrets stay in 0600 files, never in the store/plist (`EnvironmentVariables` is `HOME` and `PATH` only). Existing proxy, pf confinement, TailNet route and version-bound gates are unchanged. Apple `container` 1.5.0 remains deliberately pinned. `sampler.enable` (independent of `enable`) adds the shared Claude usage sampler (#236): agent `org.nixos.pi-durable-sampler` from the same deployment checkout, `HOME` and `PATH` only, and its page routed through the same Serve guard. `modelProxy.enable` (requires `enable` and `proxy.listenAddress`) adds the model-provider proxy (durable-pi-workspace#268): agent `org.nixos.pi-durable-model-proxy` on Nix's Node, `HOME` only, a firewall exception for that store path, and its port in the pf anchor. `githubProxy.enable` (requires `enable` and `proxy.listenAddress`) adds the GitHub write proxy (durable-pi-workspace#268): agent `org.nixos.pi-durable-github-proxy` on the same Node, `HOME` and a `PATH` of Nix's git and openssh only, the same firewall exception, and its port in the pf anchor. See [deployment separation](../../docs/pi-durable-workspace-separation.md) and [commissioning inputs](#pi-durable-commissioning-inputs). |
| omlx | 8000 (loopback only) | User-managed venv `<stateDir>/runtime/venv/bin/omlx`, built from a pinned source revision (never the Homebrew tap or the macOS app) | On demand: always-on agent when enabled, no model loaded at startup | **Imported on Studio, disabled (2026-10-16).** Trial runtime for MLX models larger than RAM (MoE expert offload from SSD) beside Ollama. Nix owns one foreground `omlx serve` agent (`org.nixos.omlx`) and nothing else: no install, download, firewall rule, Tailscale route, MCP config or credential. `omlx-preflight.sh` exits 78 with one line on stderr unless the runtime is installed and `settings.json` was commissioned with an API key and `cors_origins = []` (oMLX's defaults let any web page in the operator's browser claim the admin key), no model is pinned, and no model directory carries code (mlx-vlm imports `config.json` `model_file` whatever `trust_remote_code` says). `KeepAlive.Crashed` restarts only crashes, so a refusal does not loop. Hugging Face is offline for the process (`HF_HUB_OFFLINE=1`) with `HF_HOME` under the state directory, and `--no-hf-cache` keeps `~/.cache/huggingface` out of discovery. The API key lives only in oMLX's 0600 `settings.json`, written by the operator; per-model settings (expert offload, MTP) are oMLX's own state, set through its admin API. Review, install route, commissioning and rollback: [`docs/omlx-glm53-trial.md`](../../docs/omlx-glm53-trial.md). Tests: `python3 scripts/test_omlx_module.py`. |

## Where Enabled

Services are enabled per-host in `modules/hosts/{host}.nix`:
```nix
imports = [ ... syncthing ... ];
services.syncthing.enable = true;
```

Only **studio** enables the full stack (ollama, open-webui, monitoring, smb-mount, icloud-backup).
Only **studio** enables **artifact-shelf** (2026-11-06; tailnet HTTPS 8092, linked from Dashy).
Only **studio** enables **summit-point-gallery** (loopback-only concept preview behind the existing Cloudflare Tunnel).
Only **studio** enables **fseventsd-watchdog** (2026-11-02). It works without the monitoring module too (metrics and restarts, no alerts or dashboard), so another Mac can enable it alone.
**studio** enables **pi-durable** for commissioning (2026-10-16): proxy on the `pi-durable-sgg` internal network (`192.168.128.1`, `192.168.128.0/24`, `fd85:1c07:74fb:2dc7::/64`), pf confining that network to the proxy, page on tailnet HTTPS 8790. The service starts paused; its own gates decide what runs. It also enables the **pi-durable sampler** (#236): loopback page 8791, routed on tailnet HTTPS 8791. And the **model proxy** (durable-pi-workspace#268) on the gateway at 8892, and the **GitHub write proxy** (the same issue) with its push and signing listener on the gateway at 8893.
**studio** imports **omlx** with explicit trial settings and leaves it disabled until the reviewed runtime and checkpoint are approved, installed and verified. No other host imports it.
Hindsight is retired: no service module or host enables it. Preserve its inactive data and encrypted backups; do not recreate third-party memory integrations. Agents use harness-native memory/history and established vaults.
All darwin hosts enable **syncthing**.
No host enables **obsidian-headless** or **vault-git-backup** any more (retired 2026-09-16; the personal second brain is Apple Notes and `/Users/bryan/second-brain` is a frozen archive). Nothing may automatically commit, pull, push, or sync that archive.

## Hermes local runtime patches

The Hermes updater (`scripts/hermes-update-preflight.py`, called twice by the wrapper in `hermes.nix`) admits a dirty managed checkout only when every change is one of three reviewed patches. Each may be present without the others. Anything else (another file, a staged change, a mode change, an extra hunk or byte, an extra untracked file) refuses the update before backup or downtime. **A new local edit to the live Hermes checkout must be added here (pin, preflight, tests) in the same change, or every nightly update refuses from then on**; the Rex patch went unregistered from 2026-10-31 to 2026-11-02 and blocked two runs. The guide is [`docs/hermes-auto-updates.md`](../../docs/hermes-auto-updates.md). Tests: `python3 scripts/test_hermes_updater.py`.

| Patch | Recorded in | Pinned by |
|-------|-------------|-----------|
| Classic `/status` session-id correction (one line, `hermes_cli/cli_session_mixin.py`) | `dotfiles/hermes/runtime-patches/cli-status-session.*` | `corrected_status_source`: the AST-located one-line rewrite of HEAD's file |
| `hermes send` to Matrix through the running gateway ([durable-pi-workspace#170](https://git.snowboardtechie.com/bryan/durable-pi-workspace/issues/170)), plus `hermes send --react`/`--redact` through it ([durable-pi-workspace#191](https://git.snowboardtechie.com/bryan/durable-pi-workspace/issues/191)); modifies `gateway/run_bootstrap.py`, `hermes_cli/send_cmd.py`, `tools/send_message_senders.py`, adds untracked `gateway/matrix_send_forward.py` and `tests/gateway/test_matrix_send_forward.py` | `scripts/hermes-runtime-patches/matrix-send-gateway.{patch,json}`: reviewed as hermes-agent `bf0203a794..9bdfc74c29`, recut onto v0.22.6 (`f0d94882e8`) on 2026-11-02 (only `run_bootstrap.py`'s hunk context changed) | `MATRIX_PATCH_SHA256` on the patch file. The five files must equal HEAD plus that patch, byte for byte, and the new files must not be executable |
| Rex program-status report: the classic CLI tells Rex (OSC 7501) when a turn is working, blocked on a prompt, done or failed; modifies `hermes_cli/cli_chat_turn_mixin.py` and `hermes_cli/terminal_notify.py` | `scripts/hermes-runtime-patches/rex-program-status.{patch,json}`, the same bytes as `dotfiles/hermes/runtime-patches/rex-program-status.patch` (whose test covers its behaviour); change both together | `REX_PATCH_SHA256` on the patch file. Both files must equal HEAD plus that patch, byte for byte |

**How the pinned patches are carried.** Nix copies the Matrix and Rex patch files into the store and passes them to both preflight calls as `--matrix-patch` and `--rex-patch`. The preflight refuses to run at all if either file's SHA-256 is not the pinned one, so editing a patch means changing the constant and the manifest together, under review. `MATRIX_PATCH_FILES` can hold a superseded Matrix patch beside the current one while a live checkout moves between them (`plan` then records which by its SHA-256 and `verify` requires that same one); today it holds only the current one. Before downtime, `plan` applies each present patch to the update target in a scratch Git index and predicts Hermes's own stash restore (`stash push --include-untracked`, checkout, `stash apply`). Each modified file's three-way merge must come out clean and equal to the target plus the patch, and the Matrix patch's new files must be absent from the target. Hermes restores every patch from one stash and parks *all* local changes on any conflict, so a failed prediction for any patch refuses the whole update. `verify` then requires the checkout to be exactly the target plus each planned patch. A configuration that discards local changes is refused while any patch is present.

**A manual `hermes update` is not guarded.** Run by hand, Hermes updates first and finds conflicts afterwards: on 2026-11-02 v0.22.6 changed the Matrix patch's `run_bootstrap.py` lines, the restore conflicted, and all three patches were parked in a stash (`hermes update` prints its ref) while the gateway restarted without them. Recovery, in `~/.hermes/hermes-agent` (coordinator; it changes the live runtime, so Bryan approves it):

1. Cut the conflicting patch afresh for the new HEAD in a scratch clone (`git clone --shared`), apply the others there, run the Matrix patch's own `tests/gateway/test_matrix_send_forward.py`, and land the new pin through review and `update-system` like any patch change.
2. In the live checkout, remove leftover untracked patch files only after matching them against the manifest's `patched_sha256`, then `git apply` each patch (`--check` first) and compare every file with its manifest `patched_sha256`.
3. Confirm admission without updating: run the installed wrapper's `plan` command by hand (`<venv python> <store>/hermes-update-preflight.py plan --root ~/.hermes/hermes-agent --matrix-patch <store>/…-matrix-send-gateway.patch --rex-patch <store>/…-rex-program-status.patch --config ~/.hermes/config.yaml`, copied from the wrapper). It fetches `origin/main` and changes nothing else. Stderr should report each present patch as preserved; stdout stays empty while the checkout is already current.
4. Restart the gateway so it registers the Matrix verbs. Until then every Matrix `hermes send` exits 1 with "restart the gateway". Rex reporting reaches CLI sessions started after the patch.
5. Keep the parked stash until the run after next has passed; it is the only copy of the pre-update tree.

**First application of a pinned patch to the live checkout** (coordinator; Bryan approves): deploy the updater that pins it with `update-system` from clean pushed main *first*, so the nightly job never meets the patch with an older preflight (which refuses rather than drops it). Then apply it as in recovery steps 2 to 4. Bryan's gateway is the Hermes-owned LaunchAgent: `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway`. A headless instance's is `sudo launchctl kickstart -k system/ai.hermes.gateway-<name>`. The next update needs nothing extra: the wrapper's backup, stash restore, `verify` and gateway restart carry every patch.

**Retirement.**

- *Upstream ships the identical Matrix change* (the target already contains HEAD plus the patch, including both new files byte for byte): the restore absorbs the patch, the updated tree is clean, and `verify` accepts it. Then remove the patch files, the constants and the tests in a reviewed change.
- *Upstream ships an equivalent but different fix, or changes the patched lines:* the patch no longer applies, and every update refuses with "the required matrix-send patch cannot be cleanly preserved on this target". Nothing changes and the fix is not lost. Review the upstream fix. If it covers #170, reverse the local patch in the live checkout (`git apply -R <patch>`; the untracked files go with it), then remove the patch from this repo, and the next update proceeds. If it doesn't, cut and review a new patch for the new base and replace the pinned one.
- To drop the patch without an upstream replacement, run `git apply -R <patch>` in the live checkout and restart the gateway. `hermes send` returns to its standalone Matrix connection, which reintroduces the #170 key desynchronization, and `--react`/`--redact` are gone (Pi Durable's rating mirror then stays pending).
- The Rex patch retires the same way: when upstream ships the identical change the restore absorbs it; when upstream changes its lines, updates refuse with "the required Rex program-status patch cannot be cleanly preserved on this target" until it is recut or dropped. Dropping it (`git apply -R <patch>`) only stops Hermes's Rex tab status; nothing else depends on it.

## Pi Durable commissioning inputs

`services.pi-durable` declares the service in `~/.local/share/pi-durable/sgg-deployment`. Until submission-path reconciliation lands, the canonical `~/code/durable-pi-workspace` still hosts the submit tools and must remain clean pushed main with a verified build; human/agent edits happen only in linked worktrees. That repository's `workspaces/sgg/OPERATIONS.md` owns the live gates. See the [migration/rollback procedure](../../docs/pi-durable-workspace-separation.md). On Studio the module was enabled for commissioning on 2026-10-16, with the inputs below set from observation:

1. **The decision:** `services.pi-durable.enable = true` in `hosts/studio.nix`, made after the security and prerequisite review and applied with the standard `update-system` from clean main. Enabling alone runs the paused agent, which serves its page on loopback. It does not start the proxy, open the firewall or add a Serve route.
2. **The application:** a dedicated clean pushed-main checkout at `~/.local/share/pi-durable/sgg-deployment`, built and verified before activation, never a temporary worktree. Existing locked dependencies may be copied from the source checkout; installing dependencies remains separately governed. Never rebuild beneath a running service. Configuration remains `~/.config/pi-durable/sgg.json`; state and the 0600 secrets/deploy key/`known_hosts` remain unchanged. Moving the launch path does not itself migrate evidence or rewrite case history.
3. **Apple `container` (installed, pinned):** 1.5.0 from Apple's signed, notarized installer (Developer ID "Apple Inc. - Containerization"), approved 2026-10-16 and started once interactively. This module never installs it: nixpkgs pins 1.1.0, and Homebrew would float it at every activation (`onActivation.upgrade`), under a sandbox gate that attests one version. Upgrades are deliberate: install the new signed package, re-run the sandbox gate, record it (automation: durable-pi-workspace issue 14). The config's `sandbox.cli` names `/usr/local/bin/container`.
4. **Proxy:** `proxy.listenAddress` is the internal container network's gateway IPv4 address, and `proxy.allowedClients` is that network's subnet. Both come from the network created in OPERATIONS §3.3. Until both are set there is no tinyproxy agent and no firewall registration. The listen address must be one canonical IPv4 address, not `0.0.0.0`. Each client must be a canonical IPv4 address or a CIDR range with a `/1` to `/32` prefix. Octets run 0 to 255 with no leading zeros. Whitespace, newlines and any other token are refused, so no value can widen access or add a tinyproxy directive. This checks configuration syntax, not the VM's network isolation. `proxy.allowedDomains` defaults to the contract's starting allowlist. Only exact lowercase hostnames are accepted, and every other destination and every CONNECT port other than 443 is refused. The config's `sandbox.proxy` is then `http://<listenAddress>:8888`. The allowlist is unverified until the live sandbox gate passes, and a running proxy does not prove the VM cannot bypass it.
5. **Sandbox isolation (pf):** `proxy.ipv6Subnet` is the same network's IPv6 prefix (`container network inspect <name>`: `ipv6Subnet`), required with the proxy: lowercase hex with a `/1` to `/128` prefix, nothing that could end a pf rule. Measured 2026-10-16 without it, a VM on the internal network reached every Studio service bound to all interfaces on the gateway, both LAN addresses and the bridge's IPv6 link-local and ULA addresses, and the tailnet-only services on Studio's Tailscale address (Hermes dashboard 9119, Dashy 8088). IP forwarding is on and the application firewall does not filter vmnet bridges. The anchor `com.apple/100.pi-durable-sgg` passes TCP from `allowedClients` to `listenAddress:port` and refuses (`block return`) everything else from `allowedClients` and `ipv6Subnet` on every interface but `lo0`, plus TCP and UDP from IPv6 link-local on `bridge100` to `bridge115`.
   - It matches addresses, not the bridge: vmnet names the bridge when the first VM attaches (`bridge100` or `bridge101`, by what else runs), and macOS has no interface groups. So the link-local rule covers every vmnet bridge, the default network's too. The residual: a VM that gains root can send one-way packets from a forged source address; nothing it reaches can answer it.
   - `lo0` is exempt so Studio's own connections from the gateway address work (the live gate's host-side controls use them).
   - It sits under `com.apple/`, the one anchor point the stock `/etc/pf.conf` evaluates, so neither that file nor Apple's anchors are touched. `pi-durable-pf.sh` loads it, holds its own pf enable reference (token in `/var/run/pi-durable-pf.token`; vmnet's NAT takes and drops its own) and fails unless pf is enabled, the main ruleset evaluates `com.apple/*` and the anchor holds exactly the generated rules. It runs in `extraActivation` (a failure aborts activation) and from `org.nixos.pi-durable-pf` at boot, retried until it succeeds, because pf rules do not survive a restart and nix-darwin does not re-run activation at boot.
   - Recreating the network changes both subnets: update `allowedClients` and `ipv6Subnet` with it and re-run the sandbox gate.
6. **Page route:** `page.port` must equal the config's `page.port` (8790; confirm it is free). `page.tailscaleHttpsPort` is a free tailnet HTTPS port. On 2026-10-16 Serve already held 443, 9120 and 10443 (HTTPS; 10443 is not declared in Nix) and 11434 (TCP). `pi-durable-page-route.sh` reads the live Serve state at activation. It routes only a port that is entirely free, or one that already serves exactly this page: an HTTPS listener with one `/` proxy to it. It refuses Funnel (public) exposure of the port on any hostname. It also refuses a TCP forward, a plain HTTP listener, stale or extra handlers, and a foreground Serve session on the port. Other ports and routes are untouched. Whether Serve forwards the Host header the page accepts is part of the page gate.

Operating notes:

- The agent is `org.nixos.pi-durable-sgg`, so `AGENT=gui/$(id -u)/org.nixos.pi-durable-sgg`. The `org.nixos.` label is what lets `base/homebrew.nix` re-pin it after a Homebrew `node` upgrade. That re-pin restarts it on every activation, which the service handles: SIGTERM stops it in order, and a review in flight reruns.
- The sampler (application runbook `workspaces/sampler/OPERATIONS.md`) is `org.nixos.pi-durable-sampler`, running `main.js run --config ~/.config/pi-durable/sampler.json` from the deployment checkout, `RunAtLoad` and `KeepAlive`. `sampler.port` must equal `page.port` in `sampler.json` (8791) and `sampler.tailscaleHttpsPort` is a free tailnet HTTPS port (8791; on 2026-11-01 Serve held 443, 8790, 9090, 9120, 10443, 11434, 12345 and 33000). Assertions refuse a sampler port equal to the SGG page's loopback or Serve port. `sampler.json` must exist before activation: without it `run` prints `<file>: does not exist` and exits 1, and `KeepAlive` respawns it at launchd's throttle (about every 10 s), each attempt adding that line to the error log. The `org.nixos.` label and Homebrew `node` mean the re-pin above covers it too. Rollback: `sampler.enable = false` and `update-system`, then `tailscale serve --https=8791 off`; its ledgers stay.
- The model proxy (application runbook `workspaces/proxies/OPERATIONS.md`) is `org.nixos.pi-durable-model-proxy`, running nixpkgs `nodejs-slim_24` (a store path, not Homebrew's `node`, so it is not re-pinned and Homebrew's node never gets a firewall exception) on `model-main.js run --config ~/.config/pi-durable/model-proxy.json` from the deployment checkout, `RunAtLoad` and `KeepAlive`, `EnvironmentVariables` `HOME` only (it runs no other program). It listens on `proxy.listenAddress` at `modelProxy.port` (8892), which must equal `listen.port` in `model-proxy.json`; until the gateway exists the bind fails and it retries in-process. Without `model-proxy.json`, or before the deployment build carries `model-main.js`, it exits 1 and `KeepAlive` respawns it at launchd's throttle. Activation adds and unblocks that Node binary in the application firewall, as tinyproxy's, and the pf anchor passes `allowedClients` to `listenAddress:modelProxy.port` before its blocks. Assertions refuse it without `enable` and `proxy.listenAddress`, and on tinyproxy's or a page's port. Rollback: `modelProxy.enable = false` and `update-system`; the anchor drops the port on that reload.
- The GitHub write proxy (the same runbook, §7) is `org.nixos.pi-durable-github-proxy`, running the same Node on `github-main.js run --config ~/.config/pi-durable/github-proxy.json` from the deployment checkout, `RunAtLoad` and `KeepAlive`. Its API route is a Unix socket in its state directory. With a `push` block in `github-proxy.json` it also listens on `proxy.listenAddress` at `githubProxy.port` (8893), which must equal `push.listen.port`, for case VMs' pushes and `/sign`, retrying until the gateway exists; without the block it opens no port. `EnvironmentVariables` is `HOME` and a `PATH` of only nixpkgs `git` and `openssh` `bin` (`git`, `git http-backend`, `git verify-commit`, `ssh-keygen`), so nothing on it finds Homebrew's `node`. The Node firewall exception is applied while either proxy is enabled, and the pf anchor passes `allowedClients` to `listenAddress:githubProxy.port` before its blocks; with it disabled the anchor is unchanged. Assertions refuse it without `enable` and `proxy.listenAddress`, and on tinyproxy's, the model proxy's or a page's port. Rollback: `githubProxy.enable = false` and `update-system`.
- Logs: `/tmp/pi-durable-sgg.log`, `/tmp/pi-durable-sgg.error.log`, `/tmp/pi-durable-sampler.log`, `/tmp/pi-durable-sampler.error.log`, `/tmp/pi-durable-proxy.log`, `/tmp/pi-durable-proxy.error.log`, `/tmp/pi-durable-model-proxy.log`, `/tmp/pi-durable-model-proxy.error.log`, `/tmp/pi-durable-github-proxy.log`, `/tmp/pi-durable-github-proxy.error.log`, and the root pf loader's `/var/log/pi-durable-pf.log`.
- pf state (root): `sudo pfctl -a com.apple/100.pi-durable-sgg -sr` shows the loaded rules, `sudo pfctl -s info` whether pf is enabled, `sudo pfctl -s References` the enable references.
- Rollback: pause the service first (OPERATIONS §4), then set `enable = false` and run `update-system`. Removing a Serve route is not automatic: `tailscale serve --https=<port> off`. Neither is the pf anchor, which stays loaded until a restart: `sudo pfctl -a com.apple/100.pi-durable-sgg -F all`, then `sudo pfctl -X $(cat /var/run/pi-durable-pf.token)`. The state directory and evidence are left untouched.
- Tests: `python3 scripts/test_pi_durable_module.py`. It evaluates Studio as configured (nothing installed, started, exposed or routed) and enabled (contract command, no secrets in the environment), and the sampler's agent, its route on 8791 and its port assertions, and the model proxy's agent on Nix's Node, its firewall exception, its pf port and its assertions, and the write proxy's agent, `PATH`, gateway port in pf, the shared firewall exception and its assertions. It checks the accepted and refused proxy addresses, clients and domains, including embedded newlines. It runs the Serve guard against Studio's live route shape and synthetic Funnel, stale and conflicting states, and it runs the generated tinyproxy configuration on loopback. It checks Studio's pf anchor text exactly, has this Mac's `/sbin/pfctl -n` parse and expand it (no root needed), checks the boot daemon and the `ipv6Subnet` refusals, and runs the loader against a stand-in `pfctl`: one enable reference across reruns, a stale token replaced, and failure on refused rules, a missing token, no `com.apple/*` hook or rules that differ.

## Alerting (monitoring module)

Pipeline: **Prometheus (rule eval) → Alertmanager (dedup/group/route) → smtp2go → email**

Host must set `services.monitoring.alertEmail = "you@example.com"` and must
have the SMTP password on disk:

```
~/.secrets/grafana-smtp-password   # smtp2go API password (reused by
                                   # Alertmanager via smtp_auth_password_file)
```

### Webhook / Matrix (optional, alongside email)

`services.monitoring.alertWebhook = { url; tokenHeader; tokenFile; }` adds a
webhook integration to the same Alertmanager receiver, so every notification
(firing and resolved, plus the 4h repeats) goes to both email and the webhook,
as Alertmanager's standard JSON payload. Alertmanager retries each integration
independently: a webhook outage never delays email. It requires `alertEmail`.
Alertmanager reads the token from `tokenFile` and sends it in `tokenHeader`
(trailing newline stripped); it never enters the store or the plist.

**Studio: Hermes → encrypted Matrix room.** Studio points the webhook at
Hermes's `studio-alerts` route, `http://127.0.0.1:8644/webhooks/studio-alerts`,
with the token in `X-Gitlab-Token` from
`~/.secrets/hermes-alertmanager-webhook-token`. The route is deliver-only (the
rendered template is the message; no agent or LLM run) and posts through the
running gateway's Matrix adapter, so messages are end-to-end encrypted, into the
"Studio Alerts" room (`!BY2yuFPzB-GFM6e1MCD33kHk-YH2c1H3Ga-R59PCTUg`) as
`@hermes`. Hermes created the room, the route, the token and its config backup;
the route lives in `~/.hermes/config.yaml`, not in this repo, and changes to it
go through Hermes. Its template reads `status`, `commonLabels` (alertname,
severity, instance) and `commonAnnotations` (summary, description), which hold
the alert's own values because notifications group by alertname + instance.

**Failure signal:** Prometheus scrapes Alertmanager, and `AlertDeliveryFailed`
fires per integration when Alertmanager gives up on a notification after its
retries. Each channel reports the other: a webhook failure (Hermes down, or
answering 502 because the Matrix send failed) arrives by email, and an email
failure through the webhook. Matrix alerts depend on the Hermes gateway, which
restarts during its nightly 04:00 update; Alertmanager keeps retrying a
notification for up to the 5m group interval, which covers a normal restart.

**Rollback:** unset `alertWebhook` and run `update-system`. The Hermes route and
room are Hermes's to remove.

Tests: `python3 scripts/test_monitoring_alert_webhook.py`. It checks the
receiver and scrape wiring with and without `alertWebhook`, the refusals
(no email, malformed URL, header or token path), that the token stays out of
the store and plist, `amtool`/`promtool` acceptance of the generated configs,
and `promtool test rules` cases for the failure rule. It also runs the real
Alertmanager with Studio's generated webhook integration against a stand-in
endpoint on loopback: the token arrives without its file's trailing newline,
the payload carries the fields Hermes's template reads, and a 502 makes
Alertmanager retry.

Alert rules live in Prometheus (`monitoring.nix`) as declarative PromQL under
version control. Alertmanager handles delivery via a simple declarative
receiver config.

**Feature modules extend monitoring** without editing `monitoring.nix`:
`services.monitoring.extraAlertRules` (appended to the service-health group and
delivered like every other rule), `services.monitoring.extraDashboards`
(`name.json` → file, provisioned beside the built-in dashboards; built-in names
are refused) and `services.monitoring.nodeExporter.textfileDirectories` (each
becomes a `--collector.textfile.directory`). A module that may land on a host
without monitoring sets them under `lib.optionalAttrs (options.services ?
monitoring)`, as `fseventsd-watchdog.nix` does. Note that `HighCpuUsage`
averages all cores, so one process pinning a single core (about 4% of Studio)
never trips it; a per-process signal needs its own exporter and rule.

Grafana is kept as pure visualization — it gets Alertmanager added as a
datasource so you can view alert state in its UI, but notifications go
through Alertmanager directly, not through Grafana. This avoids a real
incompatibility in Grafana 12.4's embedded Alertmanager: its `/api/v2/alerts`
endpoint expects a non-spec wrapped payload (`{alerts: [...]}`) while
Prometheus's client sends the canonical bare array per AM v2, causing 400
"cannot unmarshal array into PostableAlerts" on every real alert. Real
Alertmanager speaks canonical AM v2.

## Scheduling Patterns

Four launchd scheduling modes used (match existing when adding):

| Mode | Config | Used By |
|------|--------|---------|
| Always-on daemon | `RunAtLoad=true` + `KeepAlive=true` | ollama, open-webui, syncthing, monitoring |
| Event-driven one-shot | `RunAtLoad=true` + `KeepAlive=false` + `WatchPaths` | smb-mount |
| Calendar-scheduled | `StartCalendarInterval` only | icloud-backup, vault-git-backup |
| Crash-only restart | `RunAtLoad=true` + `KeepAlive.Crashed=true` | omlx (a refused preflight or configuration error stays down instead of looping) |

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
