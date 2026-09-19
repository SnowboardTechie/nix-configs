# Base module: Homebrew configuration
# Provides Homebrew infrastructure and darwin-only packages.
# Feature modules (zsh.nix, git.nix, editors.nix, cli-tools.nix) contribute
# additional brews/casks via their darwin aspects.
# Note: Homebrew is darwin-only, no nixos aspect needed
{ ... }:
{
  flake.modules.darwin.homebrew = { config, lib, ... }: {
    homebrew = {
      enable = true;

      onActivation = {
        autoUpdate = true;
        cleanup = "zap";
        upgrade = true;
      };

      # Common Homebrew taps
      taps = [
      ];

      # Darwin-only packages (no NixOS equivalent)
      brews = [
        "ca-certificates"
      ];

      # Common Homebrew casks (GUI applications)
      casks = [
        "font-meslo-lg-nerd-font"
        "obsidian"
        "yaak"
      ];
    };

    # === Re-pin launchd agents to their upgraded Homebrew binaries ===
    #
    # `onActivation.upgrade` above replaces brew binaries in place. The plists
    # for agents pointing at those binaries don't change, so nix-darwin's
    # userLaunchd phase leaves the jobs alone -- and launchd pins each job to
    # the cdhash of the binary present when the job was bootstrapped. Homebrew
    # kegs are ad-hoc signed (`Signature=adhoc`, TeamIdentifier not set), so
    # every upgrade mints a fresh cdhash, the pin goes stale, and the job dies
    # with `EX_CONFIG` (78) / `job state = spawn failed` on its next respawn --
    # permanently, since KeepAlive just retries the same rejected exec.
    #
    # `launchctl kickstart` cannot fix this: it restarts a job without
    # re-registering it. Only bootout + bootstrap re-reads the plist and takes
    # a fresh cdhash.
    #
    # This lives in postActivation because nix-darwin composes a fixed phase
    # order in which `homebrew` is second to last; anything earlier (notably
    # `extraActivation`) runs before the upgrade it is meant to react to. See
    # modules/services/AGENTS.md for the full phase list.
    #
    # Ollama lost ~70 minutes to exactly this on 2026-09-19.
    system.activationScripts.postActivation.text = lib.mkAfter (
      let
        homeDir = "/Users/${config.system.primaryUser}";

        isBrewBacked = agent:
          let args = agent.serviceConfig.ProgramArguments or null;
          in args != null && lib.any (lib.hasPrefix "/opt/homebrew") args;

        brewAgents = lib.attrNames
          (lib.filterAttrs (_name: isBrewBacked) config.launchd.user.agents);
      in
      lib.optionalString (brewAgents != [ ]) ''
        # === re-pin brew-backed launchd agents ===
        brew_agent_uid=$(/usr/bin/id -u ${config.system.primaryUser})
        for brew_agent in ${lib.escapeShellArgs brewAgents}; do
          brew_agent_label="org.nixos.$brew_agent"
          brew_agent_plist="${homeDir}/Library/LaunchAgents/$brew_agent_label.plist"
          [ -f "$brew_agent_plist" ] || continue

          echo "Re-pinning $brew_agent_label to its current Homebrew binary..."
          /bin/launchctl bootout "gui/$brew_agent_uid/$brew_agent_label" >/dev/null 2>&1 || true

          # bootout is asynchronous; bootstrapping while the old job is still
          # leaving the domain fails with EBUSY. Wait for it to actually go.
          for _ in 1 2 3 4 5 6 7 8 9 10; do
            /bin/launchctl print "gui/$brew_agent_uid/$brew_agent_label" >/dev/null 2>&1 || break
            /bin/sleep 0.2
          done

          if ! /bin/launchctl bootstrap "gui/$brew_agent_uid" "$brew_agent_plist"; then
            echo "  WARNING: could not bootstrap $brew_agent_label" >&2
          fi
        done
      ''
    );
  };
}
