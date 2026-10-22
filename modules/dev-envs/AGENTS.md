# Dev Environments Module

Cross-platform `devShells` for VA projects. Each env is a `perSystem` flake output.

## Pattern

```nix
{ inputs, ... }: {
  perSystem = { pkgs, ... }: {
    devShells.{project} = let
      nodeLib = import ./_nodeLib.nix { inherit pkgs; };
      nodejs = nodeLib.nodejs{version};
    in pkgs.mkShell {
      buildInputs = [ nodejs ... ] ++ nodeLib.commonBuildTools ++ nodeLib.browserTestingDeps;
      shellHook = ''
        ${nodeLib.nodeEnvSetup}
        ${nodeLib.yarnClassicSetup}   # or yarnBerrySetup
      '';
    };
  };
}
```

## Shared Library: `_nodeLib.nix`

Underscore prefix = internal, not a flake output. Provides:

| Export | Purpose |
|--------|---------|
| `nodejs14` | Node 14.15.0 — x64 binary, Rosetta on Apple Silicon |
| `nodejs22` | Node 22.22.0 — native arm64 on Apple Silicon |
| `commonBuildTools` | python3, gcc, gnumake, pkg-config, bun, curl.dev |
| `browserTestingDeps` | Linux-only X11/GTK libs for Cypress/Playwright/Puppeteer |
| `nodeEnvSetup` | NODE_OPTIONS, PATH, npm hardening |
| `yarnClassicSetup` | YARN_IGNORE_SCRIPTS=true |
| `yarnBerrySetup` | YARN_ENABLE_SCRIPTS=false |
| `yarnInstallWithScripts` | Wrapper that temporarily enables scripts for install |

## Environments

| Shell | Node | Package Manager | Browser Tests |
|-------|------|-----------------|---------------|
| vets-website | 22.22.0 | Yarn 1.x | Cypress |
| next-build | 24 (nixpkgs) | Yarn 3.x | Playwright |
| component-library | 22 (nixpkgs) | Yarn 4.x | Puppeteer |
| content-build | 14.15.0 | Yarn 1.x | Cypress |
| simpler-grants | 20 (nixpkgs) | pnpm (corepack) | — |

## Gotchas

- Node 14.15.0 has **no native arm64 build** — runs via Rosetta 2 on Apple Silicon
- npm lifecycle scripts **disabled by default** (`npm_config_ignore_scripts=true`) for supply chain security
- Re-enable selectively with `npm rebuild <package>` or use `yarn-install-with-scripts`
- `browserTestingDeps` only applies on Linux (`lib.optionals stdenv.isLinux`)
- simpler-grants uses corepack for pnpm — has nix-store limitation workaround
- When changing Node versions, update both `_nodeLib.nix` and README "Development Environments" section
