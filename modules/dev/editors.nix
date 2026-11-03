# Development module: Editor configuration
# Provides vim and neovim for both darwin and nixos.
{ inputs, ... }:
{
  flake.modules.darwin.editors = { pkgs, ... }: {
    environment.systemPackages = with pkgs; [
      neovim
      nil          # Nix language server (LSP for nvim)
      tree-sitter  # required by nvim-treesitter (main branch) to build parsers
      vim
    ];
  };

  flake.modules.nixos.editors = { pkgs, ... }: {
    environment.systemPackages = with pkgs; [
      neovim
      nil          # Nix language server (LSP for nvim)
      tree-sitter  # required by nvim-treesitter (main branch) to build parsers
      vim
    ];
  };
}
