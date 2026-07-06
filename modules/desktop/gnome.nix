{ inputs, ... }:
{
  flake.modules.nixos.gnome = { pkgs, lib, ... }: {
    # Enable the X11 windowing system
    services.xserver.enable = true;

    # Enable the GNOME Desktop Environment
    services.displayManager.gdm = {
      enable = true;
      # Disable automatic suspend in GDM (prevents auto-suspend when idle)
      autoSuspend = false;
    };
    services.desktopManager.gnome = {
      enable = true;
      # Configure GNOME power settings to prevent user-level overrides
      extraGSettingsOverrides = ''
        [org.gnome.settings-daemon.plugins.power]
        sleep-inactive-ac-type='nothing'
        sleep-inactive-battery-type='nothing'
        sleep-inactive-ac-timeout=0
        sleep-inactive-battery-timeout=0

        [org.gnome.desktop.screensaver]
        lock-enabled=false
        idle-activation-enabled=false

        [org.gnome.desktop.session]
        idle-delay=0

        [org.gnome.shell]
        enabled-extensions=['hidetopbar@mathieu.bidon.ca']

        [org.gnome.desktop.interface]
        cursor-theme='Adwaita'
        cursor-size=24
      '';
    };

    # Cursor theme for XWayland / Electron apps that don't read GNOME gsettings.
    # Without this, apps fall back to a blank cursor rendered as a white/transparent
    # square when the configured theme can't be resolved (e.g. a stray 'breeze_cursors'
    # left over from a Plasma session).
    environment.sessionVariables = {
      XCURSOR_THEME = "Adwaita";
      XCURSOR_SIZE = "24";
    };

    # Configure keymap in X11
    services.xserver.xkb = {
      layout = "us";
      variant = "";
    };
  };
}
