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

      # === Broadcom WiFi (BCM4364 B2, codename "ekans") ===
      # The iMac Pro 2017 needs Apple-specific firmware not included in linux-firmware.
      # Firmware source: t2linux project (pre-extracted from macOS Big Sur).
      # See: https://github.com/t2linux/nixos-overlay
      boot.blacklistedKernelModules = [ "b43" "bcma" ];

      # Broadcom association fix — firmware is slow to complete WPA2 handshake.
      # MAC randomization and power saving cause association timeouts.
      networking.networkmanager.wifi.scanRandMacAddress = false;
      networking.networkmanager.wifi.powersave = false;

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

      # Apple BCM4364 B2 WiFi firmware — not in linux-firmware, must be sourced separately.
      # Uses ekans variant from Big Sur (iMac Pro 2017 codename).
      # Source: https://d0.ee/apple/ (same as t2linux/nixos-overlay)
      hardware.firmware = [
        (pkgs.stdenvNoCC.mkDerivation {
          pname = "apple-bcm4364-firmware";
          version = "big-sur-1620854225";

          src = pkgs.fetchurl {
            url = "https://d0.ee/apple/big-sur-wifi-fw-1620854225.tar.xz";
            sha256 = "sha256-YMeC8q+eccGkOUYR5hnNolKStpmcr6E4RVLdaHOiN2w=";
          };

          dontBuild = true;

          # Tarball extracts to ./firmware/ — Nix unpack cd's into it
          installPhase = ''
            mkdir -p $out/lib/firmware/brcm
            local fw=C-4364__s-B2
            local dst=$out/lib/firmware/brcm

            # iMac Pro 2017 "ekans" firmware + regulatory (BCM4364 B2)
            for base in brcmfmac4364-pcie brcmfmac4364b2-pcie; do
              cp $fw/ekans.trx       "$dst/$base.bin"
              cp $fw/ekans-X3.clmb   "$dst/$base.clm_blob"
            done

            # NVRAM — two board type variants exist (V-u=0x081d, V-m=0x07bf).
            # Provide BOTH under Apple platform naming so the kernel OTP reader finds the right one.
            # Also provide every fallback name the kernel tries.
            local nvram_u="$fw/P-ekans_M-HRPN_V-u__m-7.5.txt"
            local nvram_m="$fw/P-ekans_M-HRPN_V-m__m-7.5.txt"

            for base in brcmfmac4364-pcie brcmfmac4364b2-pcie; do
              # Apple OTP platform names (kernel tries these first)
              cp "$nvram_u" "$dst/$base.apple,ekans-HRPN-u.txt"
              cp "$nvram_m" "$dst/$base.apple,ekans-HRPN-m.txt"
              # DMI/SMBIOS model name fallback
              cp "$nvram_u" "$dst/$base.Apple Inc.-iMacPro1,1.txt"
              # Generic fallback (no suffix)
              cp "$nvram_u" "$dst/$base.txt"
            done
          '';

          meta.license = lib.licenses.unfree;
        })
      ];

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

        # Diagnostics
        pciutils
      ];

      # === System State ===

      system.stateVersion = "25.05";
    };
}
