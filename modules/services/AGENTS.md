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
| syncthing | 8384, 22000 | `/opt/homebrew/bin/syncthing` | Always-on | NixOS uses native module directly |
| smb-mount | — | mount_smbfs | Event-driven (WatchPaths) | Soft mount, no polling |
| icloud-backup | — | /usr/bin/rsync | Calendar (2:00 AM) | Excludes .stversions/.syncthing* |
| obsidian-headless | — | nixpkgs `obsidian-headless` (`ob`) | Always-on after interactive setup | **Retired 2026-09-16 (no host enables it).** Synchronized `/Users/bryan/second-brain` on Studio and MBP until the personal second brain moved to Apple Notes. Module kept as the rollback path. |
| vault-git-backup | — | nixpkgs Git | Calendar (3:00 AM) | **Retired 2026-09-16 (no host enables it).** Studio-only snapshot of `/Users/bryan/second-brain` to `origin/main`; `scripts/test_vault_git_backup.py` still guards the script for rollback. |
| hermes | 443, 9119, 9120 (Studio Tailscale only) | Nix client package + isolated per-user managed runtimes + Caddy proxies | Always-on on Studio; Bryan updates nightly, Traci weekly | Studio runs Bryan's gateway/dashboard plus per-user headless remote backends. Nix owns launchd, permissions, ports, and Tailscale while each server runtime updates independently with a hard-gated state backup and source-drift check (only the exact reviewed classic status repair and #170 matrix-send patch are permitted, see [Hermes local runtime patches](#hermes-local-runtime-patches); shallow checkouts are deepened first). Updaters announce failures and refusals as well as success, and headless updates restart that user's gateway daemon too. Bryan's dashboard remains bound to its Tailscale IP so Hermes keeps its authentication gate enabled; Tailscale Serve and Caddy add HTTPS on port 443. Traci's backend remains loopback-only behind its own authenticated proxy. MBP and Gnarbox are clients; Gnarbox's CLI/Desktop packages come from the NixOS aspect of this module. |
| dashy | 8088 (Studio Tailscale), 8089 (loopback status adapter) | Nixpkgs `dashy-ui` static build + Caddy + Python status adapter | Always-on | Private service homepage. Configuration is compiled into the immutable static build; mutable UI editing and upstream proxy endpoints are absent. Status dots read only the allowlisted Hermes watchdog state. |
| summit-point-gallery | 4321 (loopback only) | Nixpkgs `caddy` serving a staged static build | Always-on | Unlisted concept preview for the Summit Point website (repo `bryan/summit-point-site`). Exactly one hostname, `summitpoint.thompson.codes`, reaches the origin through the existing remotely managed Cloudflare Tunnel; Caddy answers any other Host header with a 404, and the tunnel holds no path rules. Serves the `current` symlink under `~/Library/Application Support/Thompson Codes/Summit Point Gallery/`, which always points at an immutable `releases/<sha>/` directory — never a working tree's `dist/`. Publish and roll back with the site repo's `scripts/publish-review.sh` (`--rollback <sha>` is an atomic symlink change, no rebuild, no activation). Every response carries `X-Robots-Tag: noindex, nofollow, noarchive`, `Cache-Control: no-store`, `nosniff`, `no-referrer`, a restrictive `Permissions-Policy`, and a static-site CSP that refuses framing. Loopback-only and deliberately NOT registered with the macOS firewall. Astro's dev server owns 4322 and must never be tunneled (enforced by a module assertion). |
| pi-durable | 8790 (loopback page); when configured: 8888 (tinyproxy on the sandbox network's gateway) and one Tailscale Serve HTTPS port | Homebrew `node` running `~/.local/share/pi-durable/sgg-deployment/dist/workspaces/sgg/src/main.js run`; nixpkgs `tinyproxy`; `/sbin/pfctl` (root, anchor `com.apple/100.pi-durable-sgg`) | Always-on when enabled; pf loader at activation and at boot | **Enabled on Studio after commissioning (2026-10-16).** Dedicated clean deployment checkout is declared; prepare its verified build before Bryan's `update-system` activation. Until then the installed agent retains the original source checkout path. Secrets stay in 0600 files, never in the store/plist (`EnvironmentVariables` is `HOME` and `PATH` only). Existing proxy, pf confinement, TailNet route and version-bound gates are unchanged. Apple `container` 1.5.0 remains deliberately pinned. See [deployment separation](../../docs/pi-durable-workspace-separation.md) and [commissioning inputs](#pi-durable-commissioning-inputs). |
| omlx | 8000 (loopback only) | User-managed venv `<stateDir>/runtime/venv/bin/omlx`, built from a pinned source revision (never the Homebrew tap or the macOS app) | On demand: always-on agent when enabled, no model loaded at startup | **Imported on Studio, disabled (2026-10-16).** Trial runtime for MLX models larger than RAM (MoE expert offload from SSD) beside Ollama. Nix owns one foreground `omlx serve` agent (`org.nixos.omlx`) and nothing else: no install, download, firewall rule, Tailscale route, MCP config or credential. `omlx-preflight.sh` exits 78 with one line on stderr unless the runtime is installed and `settings.json` was commissioned with an API key and `cors_origins = []` (oMLX's defaults let any web page in the operator's browser claim the admin key), no model is pinned, and no model directory carries code (mlx-vlm imports `config.json` `model_file` whatever `trust_remote_code` says). `KeepAlive.Crashed` restarts only crashes, so a refusal does not loop. Hugging Face is offline for the process (`HF_HUB_OFFLINE=1`) with `HF_HOME` under the state directory, and `--no-hf-cache` keeps `~/.cache/huggingface` out of discovery. The API key lives only in oMLX's 0600 `settings.json`, written by the operator; per-model settings (expert offload, MTP) are oMLX's own state, set through its admin API. Review, install route, commissioning and rollback: [`docs/omlx-glm53-trial.md`](../../docs/omlx-glm53-trial.md). Tests: `python3 scripts/test_omlx_module.py`. |

## Where Enabled

Services are enabled per-host in `modules/hosts/{host}.nix`:
```nix
imports = [ ... syncthing ... ];
services.syncthing.enable = true;
```

Only **studio** enables the full stack (ollama, open-webui, monitoring, smb-mount, icloud-backup).
Only **studio** enables **summit-point-gallery** (loopback-only concept preview behind the existing Cloudflare Tunnel).
**studio** enables **pi-durable** for commissioning (2026-10-16): proxy on the `pi-durable-sgg` internal network (`192.168.128.1`, `192.168.128.0/24`, `fd85:1c07:74fb:2dc7::/64`), pf confining that network to the proxy, page on tailnet HTTPS 8790. The service starts paused; its own gates decide what runs.
**studio** imports **omlx** with explicit trial settings and leaves it disabled until the reviewed runtime and checkpoint are approved, installed and verified. No other host imports it.
Hindsight is retired: no service module or host enables it. Preserve its inactive data and encrypted backups; do not recreate third-party memory integrations. Agents use harness-native memory/history and established vaults.
All darwin hosts enable **syncthing**.
No host enables **obsidian-headless** or **vault-git-backup** any more (retired 2026-09-16; the personal second brain is Apple Notes and `/Users/bryan/second-brain` is a frozen archive). Nothing may automatically commit, pull, push, or sync that archive.

## Hermes local runtime patches

The Hermes updater (`scripts/hermes-update-preflight.py`, called twice by the wrapper in `hermes.nix`) admits a dirty managed checkout only when every change is one of two reviewed patches. Either may be present without the other. Anything else (another file, a staged change, a mode change, an extra hunk or byte, an extra untracked file) refuses the update before backup or downtime. The guide is [`docs/hermes-auto-updates.md`](../../docs/hermes-auto-updates.md). Tests: `python3 scripts/test_hermes_updater.py`.

| Patch | Recorded in | Pinned by |
|-------|-------------|-----------|
| Classic `/status` session-id correction (one line, `hermes_cli/cli_session_mixin.py`) | `dotfiles/hermes/runtime-patches/cli-status-session.*` | `corrected_status_source`: the AST-located one-line rewrite of HEAD's file |
| `hermes send` to Matrix through the running gateway ([durable-pi-workspace#170](https://git.snowboardtechie.com/bryan/durable-pi-workspace/issues/170)); modifies `gateway/run_bootstrap.py`, `tools/send_message_senders.py`, adds untracked `gateway/matrix_send_forward.py` and `tests/gateway/test_matrix_send_forward.py` | `scripts/hermes-runtime-patches/matrix-send-gateway.{patch,json}`, cut from hermes-agent `bf0203a794..2be71e5f54` | `MATRIX_PATCH_SHA256` on the patch file. The four files must equal HEAD plus that patch, byte for byte, and the new files must not be executable |

**How the #170 patch is carried.** Nix copies the patch into the store and passes it to both preflight calls as `--matrix-patch`. The preflight refuses to run at all if that file's SHA-256 is not the pinned one, so editing the patch means changing the constant and the manifest together, under review. Before downtime, `plan` applies the patch to the update target in a scratch Git index and predicts Hermes's own stash restore (`stash push --include-untracked`, checkout, `stash apply`). Each modified file's three-way merge must come out clean and equal to the target plus the patch. The new files must be absent from the target. Hermes restores both patches from one stash and parks *all* local changes on any conflict, so a failed prediction for either patch refuses the whole update. `verify` then requires the checkout to be exactly the target plus each planned patch. A configuration that discards local changes is refused while either patch is present.

**First application to the live checkout** (coordinator; it changes the live runtime, so Bryan approves it):

1. Deploy this updater with `update-system` from clean pushed main *first*, so the nightly job can never meet the patch with the old one-patch preflight. The old preflight refuses rather than drops it, but every night's update would then refuse.
2. In `~/.hermes/hermes-agent`, at `base_commit` (`bf0203a794`, detached, with only the `/status` patch dirty): `git apply --check <patch>`, then `git apply <patch>`, then compare each file with `files[].patched_sha256` in the manifest.
3. Confirm admission without updating: run the installed wrapper's `plan` command by hand (`<venv python> <store>/hermes-update-preflight.py plan --root ~/.hermes/hermes-agent --matrix-patch <store>/…-matrix-send-gateway.patch --config ~/.hermes/config.yaml`, copied from the wrapper). It fetches `origin/main` and changes nothing else. On stderr it should print `preserve matrix-send patch: True`. Stdout stays empty while the checkout is already current.
4. Restart the gateway at once. Bryan's is the Hermes-owned LaunchAgent: `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway`. A headless instance's is `sudo launchctl kickstart -k system/ai.hermes.gateway-<name>`. Until it restarts, the running gateway has no `matrix-send` verb, and every Matrix `hermes send` (Pi notices included) exits 1 with "restart the gateway".
5. The next update needs nothing extra. The wrapper's backup, stash restore, `verify` and gateway restart carry the patch.

**Retirement.**

- *Upstream ships the identical change* (the target already contains HEAD plus the patch, including both new files byte for byte): the restore absorbs the patch, the updated tree is clean, and `verify` accepts it. Then remove the patch files, the constants and the tests in a reviewed change.
- *Upstream ships an equivalent but different fix, or changes the patched lines:* the patch no longer applies, and every update refuses with "the required matrix-send patch cannot be cleanly preserved on this target". Nothing changes and the fix is not lost. Review the upstream fix. If it covers #170, reverse the local patch in the live checkout (`git apply -R <patch>`; the untracked files go with it), then remove the patch from this repo, and the next update proceeds. If it doesn't, cut and review a new patch for the new base and replace the pinned one.
- To drop the patch without an upstream replacement, run `git apply -R <patch>` in the live checkout and restart the gateway. `hermes send` returns to its standalone Matrix connection, which reintroduces the #170 key desynchronization.

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
- Logs: `/tmp/pi-durable-sgg.log`, `/tmp/pi-durable-sgg.error.log`, `/tmp/pi-durable-proxy.log`, `/tmp/pi-durable-proxy.error.log`, and the root pf loader's `/var/log/pi-durable-pf.log`.
- pf state (root): `sudo pfctl -a com.apple/100.pi-durable-sgg -sr` shows the loaded rules, `sudo pfctl -s info` whether pf is enabled, `sudo pfctl -s References` the enable references.
- Rollback: pause the service first (OPERATIONS §4), then set `enable = false` and run `update-system`. Removing a Serve route is not automatic: `tailscale serve --https=<port> off`. Neither is the pf anchor, which stays loaded until a restart: `sudo pfctl -a com.apple/100.pi-durable-sgg -F all`, then `sudo pfctl -X $(cat /var/run/pi-durable-pf.token)`. The state directory and evidence are left untouched.
- Tests: `python3 scripts/test_pi_durable_module.py`. It evaluates Studio as configured (nothing installed, started, exposed or routed) and enabled (contract command, no secrets in the environment). It checks the accepted and refused proxy addresses, clients and domains, including embedded newlines. It runs the Serve guard against Studio's live route shape and synthetic Funnel, stale and conflicting states, and it runs the generated tinyproxy configuration on loopback. It checks Studio's pf anchor text exactly, has this Mac's `/sbin/pfctl -n` parse and expand it (no root needed), checks the boot daemon and the `ipv6Subnet` refusals, and runs the loader against a stand-in `pfctl`: one enable reference across reruns, a stale token replaced, and failure on refused rules, a missing token, no `com.apple/*` hook or rules that differ.

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
