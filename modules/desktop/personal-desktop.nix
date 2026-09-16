# Desktop module: Bryan's personal macOS applications
# Shared by the personal MBP and Mac Studio daily desktop.
{ ... }:
{
  flake.modules.darwin.personal-desktop = { ... }: {
    homebrew = {
      brews = [
        "libfido2" # FIDO2/U2F tools
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
    };
  };
}
