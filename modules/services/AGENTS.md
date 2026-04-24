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

## Services

| Service | Port(s) | Binary | Scheduling | Notes |
|---------|---------|--------|------------|-------|
| ollama | 11434 | `/opt/homebrew/bin/ollama` | Always-on | Flash attention, q8_0 KV cache. Auto-restarts on rebuild to pick up brew upgrades. |
| open-webui | 8080 | uv tool venv `~/.local/bin/open-webui` | Always-on + daily updater | Installed via `uv tool install`. Updater has an import probe — refuses to kickstart a broken install. |
| monitoring | 9090, 9093, 9100, 3000, 3100, 12345, 9115 | prometheus, alertmanager, node_exporter, grafana, loki, alloy, blackbox_exporter | Always-on | Binds 0.0.0.0; 7 agents; SMTP via smtp2go; alertmanager + blackbox_exporter via nixpkgs derivations; alloy replaced promtail (EOL March 2026). Alerts: Prometheus → Alertmanager → email. |
| syncthing | 8384, 22000 | `/opt/homebrew/bin/syncthing` | Always-on | NixOS uses native module directly |
| smb-mount | — | mount_smbfs | Event-driven (WatchPaths) | Soft mount, no polling |
| icloud-backup | — | /usr/bin/rsync | Calendar (2:00 AM) | Excludes .stversions/.syncthing* |

## Where Enabled

Services are enabled per-host in `modules/hosts/{host}.nix`:
```nix
imports = [ ... syncthing ... ];
services.syncthing.enable = true;
```

Only **studio** enables the full stack (ollama, open-webui, monitoring, smb-mount, icloud-backup).
All darwin hosts enable **syncthing**.

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
| Calendar-scheduled | `StartCalendarInterval` only | icloud-backup |

## Anti-Patterns

- **NEVER** use arbitrary names for `system.activationScripts.{foo}.text` — see footgun above. Use `extraActivation.text = lib.mkAfter …` instead.
- **NEVER** use Nix store paths for ProgramArguments for Homebrew packages — use `/opt/homebrew/bin/` (exception: tools not in Homebrew, like blackbox_exporter, use nixpkgs derivations).
- **ALWAYS** include both darwin and nixos aspects (even if nixos is a stub).
- **ALWAYS** log to `/tmp/{name}.log` and `/tmp/{name}.error.log` — alloy discovers these by glob and tags them with `service_name` extracted from the filename.
- **NEVER** rely on `pip install --user` for background services. The transitive dep tree is un-pinned and can rot invisibly. Use `uv tool install` (isolated venv + lockfile) or a Nix-packaged equivalent.
- NixOS stubs marked `# TODO` are intentional — use native NixOS modules when implementing.
- open-webui depends implicitly on ollama via `ollamaUrl` default — no hard dependency declared.
