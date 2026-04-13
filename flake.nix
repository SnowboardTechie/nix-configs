{
  description = "Bryan's System Configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    # Unstable packages for overlays
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";

    nix-darwin = {
      url = "github:LnL7/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };

    import-tree.url = "github:vic/import-tree";

    worktrunk = {
      url = "github:max-sixty/worktrunk";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Zen Browser (Firefox fork with vertical tabs)
    zen-browser = {
      url = "github:0xc000022070/zen-browser-flake/beta";
      inputs.nixpkgs.follows = "nixpkgs";
    };
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

      # NixOS configurations
      nixosConfigurations = {
        gnarbox = inputs.nixpkgs.lib.nixosSystem {
          specialArgs = {
            inherit inputs;
            outputs = inputs.self;
            meta = { hostname = "gnarbox"; };
          };
          modules = [ inputs.self.modules.nixos.gnarbox ];
        };
        inix = inputs.nixpkgs.lib.nixosSystem {
          specialArgs = {
            inherit inputs;
            outputs = inputs.self;
            meta = { hostname = "inix"; };
          };
          modules = [ inputs.self.modules.nixos.inix ];
        };
      };
    };
  };
}
