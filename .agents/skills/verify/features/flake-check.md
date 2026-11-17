# The flake's own check

`nix flake check` is the check this repository's `AGENTS.md` names: it evaluates every output for this machine's
system, checks the NixOS configuration and the overlays, and evaluates each dev shell's derivation. The flake defines no
`checks` output of its own, so this is the whole of it.

## Sub-features

- `devshells`: the eight `devShells.aarch64-darwin.*` evaluate (vets-website, next-build, component-library and the
  rest in `modules/dev-envs/`).
- `outputs`: the NixOS configuration, the overlays and the flake's schema check.

## How to get to it (user POV)

- `nix flake check` from the repository root, as `AGENTS.md`'s Build/Lint/Test section says.
- `nix develop '.#<shell>'` for the dev shells, which first evaluates the shell this checks.

## Driving it with nix-verify

Preconditions:

- `$V doctor` passes.

- **Check.** Run `$V run flake-check`. The step `nix flake check` is `ok`, about 15 s.
- **Proof.** `01-flake-check.err` lists `checking derivation devShells.aarch64-darwin.<shell>` for each shell, and
  ends with the warning that `x86_64-linux` was omitted.

## Gotchas

- `--all-systems` fails on main (2026-11-16): the `x86_64-linux` dev shells pull `gtk+-2.24.33`, which nixpkgs marks
  broken. It is not a journey until that is fixed.
- The check evaluates the dev shells; it does not build them. `unknown flake output 'modules'` is a known warning.
