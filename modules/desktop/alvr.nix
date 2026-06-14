# ALVR (Air Light VR)
#
# Streams VR games from this PC to a standalone headset (e.g. Meta Quest) over
# Wi-Fi. On Linux, ALVR runs as a SteamVR driver, so it requires Steam/SteamVR —
# provided by the `gaming` module (programs.steam). nixpkgs' upstream
# `programs.alvr` module installs the alvr package (the `alvr_dashboard` GUI) and
# opens the streaming ports.
#
# First-run setup (manual, one-time): launch SteamVR once so it initialises, then
# open ALVR Dashboard (`alvr_dashboard`), follow the setup wizard, install the
# ALVR client APK on the headset, and `Trust` the device to begin streaming.
#
# Linux-only: ALVR has no macOS build, so there is no darwin aspect.
{ inputs, ... }:
{
  flake.modules.nixos.alvr = { ... }: {
    programs.alvr = {
      enable = true;
      # Open ALVR's discovery/streaming ports (TCP + UDP 9943-9944) so the
      # headset can reach the streamer over the LAN.
      openFirewall = true;
    };
  };
}
