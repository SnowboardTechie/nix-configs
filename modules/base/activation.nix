# Base module: Activation scripts
# Provides shared activation scripts for Alacritty config
{ inputs, ... }:
{
  flake.modules.darwin.activation = { pkgs, config, ... }: {
    system.activationScripts.extraActivation.text = ''
      # Run as the primary user for user-specific setup
      /usr/bin/su - ${config.system.primaryUser} -c '
        # --- Alacritty platform config ---
        ALACRITTY_DIR="$HOME/.config/alacritty"
        ALACRITTY_TOML="$ALACRITTY_DIR/alacritty.toml"
        if [ -d "$ALACRITTY_DIR" ] && [ ! -L "$ALACRITTY_TOML" ]; then
          echo "Setting up Alacritty config symlink..."
          ln -sf alacritty-macos.toml "$ALACRITTY_TOML"
          echo "  Linked alacritty.toml -> alacritty-macos.toml"
        fi
      '
    '';
  };

  flake.modules.nixos.activation = { pkgs, config, ... }: {
    system.activationScripts.userSetup = ''
      # Run as bryan for user-specific setup (runuser is in util-linux, /bin/sh always exists)
      ${pkgs.util-linux}/bin/runuser -u bryan -- /bin/sh -c '
        # --- Alacritty platform config ---
        ALACRITTY_DIR="$HOME/.config/alacritty"
        ALACRITTY_TOML="$ALACRITTY_DIR/alacritty.toml"
        if [ -d "$ALACRITTY_DIR" ] && [ ! -L "$ALACRITTY_TOML" ]; then
          echo "Setting up Alacritty config symlink..."
          ln -sf alacritty-linux.toml "$ALACRITTY_TOML"
          echo "  Linked alacritty.toml -> alacritty-linux.toml"
        fi
      '
    '';
  };
}
