# Desktop module: Bryan's personal macOS applications
# Shared by the personal MBP and Mac Studio daily desktop.
{ ... }:
{
  flake.modules.darwin.personal-desktop = { ... }: {
    homebrew = {
      brews = [
        "libfido2" # FIDO2/U2F tools
        "mas" # Mac App Store CLI for declarative applications
        "ykman" # YubiKey Manager CLI
      ];

      casks = [
        "finicky" # Browser/URL router — github.com/johnste/finicky
        "keymapp" # ZSA keyboard configuration and firmware
        "logi-options+" # Logitech device configuration
        "monal"
        "rectangle-pro"
        "slack"
        "superwhisper"
        "vivaldi"
        "yubico-authenticator"
        "zoom"
      ];

      # Keep the purchased, perpetual Final Cut Pro app available separately
      # from the Creator Studio subscription build.
      masApps = {
        "Final Cut Pro" = 424389933;
      };
    };
  };
}
