{
  description = "Bryan's System Configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    # Unstable packages for overlays
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Package-only pin. The fleet nixpkgs revision predates obsidian-headless,
    # while advancing all packages introduces unrelated service regressions.
    obsidian-headless-nixpkgs.url = "github:NixOS/nixpkgs/39f82096d8d8dd504daa48311015f4664bd38418";

    nix-darwin = {
      url = "github:LnL7/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };

    import-tree.url = "github:vic/import-tree";

    googleworkspace-cli = {
      url = "github:googleworkspace/cli";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    worktrunk = {
      url = "github:max-sixty/worktrunk";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Zen Browser (Firefox fork with vertical tabs)
    zen-browser = {
      url = "github:0xc000022070/zen-browser-flake/beta";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Hermes Agent (Nous Research) — LLM agent w/ NixOS module (services.hermes-agent)
    # ponytail: no `inputs.nixpkgs.follows` here — it builds Python deps via uv2nix
    # against its own pinned nixpkgs; forcing follows tends to break the build.
    hermes-agent.url = "github:NousResearch/hermes-agent";
  };

  outputs = inputs: inputs.flake-parts.lib.mkFlake { inherit inputs; } {
    imports = [
      ./modules/_options.nix
      (inputs.import-tree ./modules)
    ];

    systems = [ "aarch64-darwin" "x86_64-linux" ];

    flake = {
      # Export overlays for use in system configurations
      overlays = import ./overlays { inherit inputs; };

      # macOS configurations using nix-darwin
      darwinConfigurations = {
        mbp = inputs.nix-darwin.lib.darwinSystem {
          system = "aarch64-darwin";
          specialArgs = { inherit inputs; };
          modules = [ inputs.self.modules.darwin.mbp ];
        };
        a6mbp = inputs.nix-darwin.lib.darwinSystem {
          system = "aarch64-darwin";
          specialArgs = { inherit inputs; };
          modules = [ inputs.self.modules.darwin.a6mbp ];
        };
        studio = inputs.nix-darwin.lib.darwinSystem {
          system = "aarch64-darwin";
          specialArgs = { inherit inputs; };
          modules = [ inputs.self.modules.darwin.studio ];
        };
      };

    };
  };
}
