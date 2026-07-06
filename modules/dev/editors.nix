# Development module: Editor configuration
# Provides vim, neovim, and Zed (with the nil Nix LSP) for both darwin and nixos.
# Darwin installs Zed via Homebrew cask; NixOS via the zed-editor package.
{ inputs, ... }:
{
  flake.modules.darwin.editors = { pkgs, ... }: {
    environment.systemPackages = with pkgs; [
      neovim
      nil          # Nix language server (LSP for Zed / nvim)
      vim
    ];
    homebrew.casks = [
      "zed"
    ];
  };

  flake.modules.nixos.editors = { pkgs, ... }: {
    environment.systemPackages = with pkgs; [
      neovim
      zed-editor
      nil          # Nix language server (LSP for Zed / nvim)
      vim
    ];
  };
}
