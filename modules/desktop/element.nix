# Shared Matrix desktop client for the Macs: Element Desktop, mainly for bot alerts.
# Native rather than a web app because it keeps running (and notifying) after its
# window closes; Element's web build has no background push.
{ ... }:
{
  flake.modules.darwin.element = { ... }: {
    homebrew.casks = [ "element" ];
  };
}
