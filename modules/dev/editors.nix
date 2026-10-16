# Development module: Editor configuration
# Provides vim, neovim, Zed, and Cursor for both darwin and nixos.
# Darwin installs GUI editors via Homebrew casks; NixOS via Nix packages.
{ inputs, ... }:
{
  flake.modules.darwin.editors = { pkgs, ... }: {
    environment.systemPackages = with pkgs; [
      neovim
      nil          # Nix language server (LSP for Zed / nvim)
      tree-sitter  # required by nvim-treesitter (main branch) to build parsers
      vim
    ];
    homebrew.casks = [
      "cursor"
      "zed"
    ];
  };

  flake.modules.nixos.editors = { pkgs, ... }: {
    environment.systemPackages = with pkgs; [
      code-cursor
      neovim
      zed-editor
      nil          # Nix language server (LSP for Zed / nvim)
      tree-sitter  # required by nvim-treesitter (main branch) to build parsers
      vim
    ];
  };
}
