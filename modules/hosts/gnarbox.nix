# Gnarbox NixOS desktop. Hardware UUIDs match the fresh 26.05 installation.
{ inputs, ... }:
{
  flake.modules.nixos.gnarbox =
    { outputs, config, pkgs, lib, ... }:
    {
      imports =
        (with inputs.self.modules.nixos; [
          fonts
          nix-settings
          zsh
          editors
          git
          cli-tools
          rust
          activation
          gnome
          gaming
          audio
          alvr
          hermes
        ])
        ++ [ ../../hardware-configs/gnarbox.nix ];

      programs.nix-ld.enable = true;

      nixpkgs = {
        overlays = [ outputs.overlays.unstable outputs.overlays.zen-browser ];
        hostPlatform = "x86_64-linux";
        config.allowUnfree = true;
      };

      boot = {
        loader = {
          systemd-boot.enable = true;
          systemd-boot.configurationLimit = 10;
          efi.canTouchEfiVariables = true;
        };
        kernelPackages = pkgs.linuxPackages_latest;
        resumeDevice = "/dev/disk/by-uuid/79097585-b795-447b-bd1a-c488b8e77f96";

         # ROG Azoth 2.4 GHz dongle (USB 0b05:1a83, full-speed on xHCI 0e:00.0)
         # drops out periodically, and the whole root hub loses power on resume
         # ("usb1: root hub lost power or was reset" / "xHC error in resume"
         # after a deep S3 suspend). Keep USB ports/xHCI out of runtime PM so a
         # quiescent port can't be pulled and fail to re-enumerate. Mitigation,
         # not a cure: residual 2.4 GHz dropouts are RF -- use a USB 2.0 port
         # away from Wi-Fi/BT, or pair the keyboard via Bluetooth instead.
        kernelParams = [ "usbcore.autosuspend=0" ];
      };

      networking = {
        hostName = "gnarbox";
        networkmanager.enable = true;
      };

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

      users.users.bryan = {
        isNormalUser = true;
        description = "Bryan";
        extraGroups = [ "networkmanager" "wheel" "plugdev" ];
        shell = pkgs.zsh;
      };

      hardware.keyboard.zsa.enable = true;
      programs = {
        firefox.enable = true;
        gnupg.agent = {
          enable = true;
          enableSSHSupport = true;
          pinentryPackage = pkgs.pinentry-gnome3;
        };
      };

      services = {
        openssh = {
          enable = true;
          settings.PasswordAuthentication = false;
        };
        pcscd.enable = true;
        printing.enable = true;
        tailscale = {
          enable = true;
          openFirewall = true;
        };
        upower.enable = lib.mkForce false;
        logind.settings.Login = {
          HandlePowerKey = "hibernate";
          HandleSuspendKey = "ignore";
          HandleLidSwitch = "ignore";
          IdleAction = "ignore";
          IdleActionSec = 0;
        };
        # Client only: Studio owns the gateway and authenticated remote backend.
        hermes = {
          enable = true;
          desktop.enable = true;
        };
      };

      environment.systemPackages = with pkgs; [
        pinentry-gnome3
        vlc
        keymapp
        obsidian
        zen-browser
        gnomeExtensions.hide-top-bar
        yaak
        libfido2
        yubioath-flutter
        yubikey-manager
        discord
      ];

      system.stateVersion = "26.05";
    };
}
