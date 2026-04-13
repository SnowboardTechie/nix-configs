# Host configuration: inix (NixOS Desktop)
#
# Features: fonts, nix-settings, zsh, editors, git, cli-tools, activation
# Desktop: gnome, audio
# Hardware: 2017 iMac Pro (Intel Xeon W, AMD Vega, Broadcom WiFi, 5K display)
{ inputs, ... }:
{
  flake.modules.nixos.inix =
    { outputs
    , config
    , pkgs
    , lib
    , meta
    , ...
    }: {
      imports = (with inputs.self.modules.nixos; [
        # Base features
        fonts
        nix-settings
        zsh
        editors
        git
        cli-tools
        activation
        # Desktop features
        gnome
        audio
      ]) ++ [
        # Hardware configuration (placeholder — replace with real nixos-generate-config output)
        ../../hardware-configs/inix.nix
      ];

      # === Nixpkgs Configuration ===

      nixpkgs = {
        hostPlatform = "x86_64-linux";
        config.allowUnfree = true;
      };

      # === Boot Configuration ===

      boot = {
        loader = {
          systemd-boot.enable = true;
          efi.canTouchEfiVariables = true;
        };
        # REPLACE: update with actual swap partition UUID from 'blkid' on iNix
        resumeDevice = "/dev/disk/by-uuid/00000000-0000-0000-0000-000000000003";
      };

      # === Networking ===

      networking = {
        hostName = meta.hostname;
        networkmanager.enable = true;
      };

      # Broadcom WiFi — 2017 iMac Pro uses Broadcom BCM43xx family
      # b43 open-source driver is recommended. If WiFi doesn't work, check your chip with
      # 'lspci | grep -i network' and see: https://nixos.wiki/wiki/Broadcom_WiFi
      networking.enableB43Firmware = true;
      boot.blacklistedKernelModules = [ "brcmfmac" "brcmsmac" "bcma" ];

      # === Localization ===

      time.timeZone = "America/Los_Angeles";

      i18n = {
        defaultLocale = "en_US.UTF-8";
        extraLocaleSettings = {
          LC_ADDRESS = "en_US.UTF-8";
          LC_IDENTIFICATION = "en_US.UTF-8";
          LC_MEASUREMENT = "en_US.UTF-8";
          LC_MONETARY = "en_US.UTF-8";
          LC_NAME = "en_US.UTF-8";
          LC_NUMERIC = "en_US.UTF-8";
          LC_PAPER = "en_US.UTF-8";
          LC_TELEPHONE = "en_US.UTF-8";
          LC_TIME = "en_US.UTF-8";
        };
      };

      # === User Configuration ===

      users.users.bryan = {
        isNormalUser = true;
        description = "Bryan";
        extraGroups = [ "networkmanager" "wheel" ];
        shell = pkgs.zsh;
      };

      # === Security ===

      security.sudo.wheelNeedsPassword = false;

      # === Hardware / Firmware ===

      # AMD Radeon Pro Vega GPU support
      hardware.graphics.enable = true;

      # Broadcom WiFi + Bluetooth firmware (also includes FaceTime HD camera firmware)
      hardware.enableAllFirmware = true;
      hardware.bluetooth.enable = true;

      # === Programs ===

      programs = {
        firefox.enable = true;
        gnupg.agent = {
          enable = true;
          enableSSHSupport = true;
          pinentryPackage = pkgs.pinentry-gnome3;
        };
      };

      # === GNOME Power Settings ===
      # gnome.nix disables all sleep/lock globally (designed for gnarbox, an always-on streaming box).
      # iNix is a garage machine — restore sensible idle behavior.
      services.desktopManager.gnome.extraGSettingsOverrides = lib.mkAfter ''
        [org.gnome.settings-daemon.plugins.power]
        sleep-inactive-ac-type='suspend'
        sleep-inactive-ac-timeout=1800

        [org.gnome.desktop.screensaver]
        lock-enabled=true
        idle-activation-enabled=true

        [org.gnome.desktop.session]
        idle-delay=uint32 300
      '';

      # === Services ===

      services.logind = {
        settings.Login = {
          HandlePowerKey = "hibernate";
        };
      };

      # === Syncthing ===
      # Uses NixOS native services.syncthing module

      services.syncthing = {
        enable = true;
        user = "bryan";
        group = "users";
        dataDir = "/home/bryan";
        configDir = "/home/bryan/.config/syncthing";
        guiAddress = "127.0.0.1:8384";
        openDefaultPorts = true;
        # Manage devices/folders via Web UI, not declaratively
        overrideDevices = false;
        overrideFolders = false;
      };

      # === Host-specific System Packages ===

      environment.systemPackages = with pkgs; [
        # GPG (GNOME-specific pinentry)
        pinentry-gnome3

        # Media
        vlc

        # Notes and project plans
        obsidian
      ];

      # === System State ===

      system.stateVersion = "25.05";
    };
}
