# Base module: Activation scripts
# Provides shared activation scripts for Alacritty config and oh-my-opencode setup
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

        # --- oh-my-opencode plugin ---
        if ! grep -q "oh-my-opencode" ~/.config/opencode/opencode.json 2>/dev/null; then
          echo "Installing oh-my-opencode plugin..."
          ${pkgs.bun}/bin/bunx oh-my-opencode install --no-tui --claude=no --copilot=no --gemini=no || true
          echo "oh-my-opencode installed!"
          echo "Next steps:"
          echo "  1. Authenticate with providers: opencode auth login"
          echo "  2. Start OpenCode: opencode"
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

        # --- oh-my-opencode plugin ---
        if ! grep -q "oh-my-opencode" ~/.config/opencode/opencode.json 2>/dev/null; then
          echo "Installing oh-my-opencode plugin..."
          ${pkgs.bun}/bin/bunx oh-my-opencode install --no-tui --claude=no --copilot=no --gemini=no || true
          echo "oh-my-opencode installed!"
        fi
      '
    '';
  };
}
