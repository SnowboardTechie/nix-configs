# PLACEHOLDER hardware configuration for iNix (2017 iMac Pro, Intel Xeon W + AMD Vega)
#
# This file is temporary and will be replaced by the real output of
# `nixos-generate-config` from the machine itself.
#
# On the iNix machine, run:
#   sudo nixos-generate-config
# and copy `/etc/nixos/hardware-configuration.nix` here.
#
# Or from another machine:
#   scp inix:/etc/nixos/hardware-configuration.nix hardware-configs/inix.nix
#
# After replacing this file, also update `boot.resumeDevice` in
# `modules/hosts/inix.nix` with the real swap UUID.

{ config, lib, modulesPath, ... }:

{
  imports =
    [
      (modulesPath + "/installer/scan/not-detected.nix")
    ];

  boot.initrd.availableKernelModules = [ "nvme" "xhci_pci" "ahci" "usbhid" "usb_storage" "sd_mod" ];
  boot.initrd.kernelModules = [ "amdgpu" ];
  boot.kernelModules = [ "kvm-intel" ];
  boot.extraModulePackages = [ ];

  fileSystems."/" =
    {
      device = "/dev/disk/by-uuid/00000000-0000-0000-0000-000000000001"; # REPLACE: root filesystem UUID
      fsType = "ext4";
    };

  fileSystems."/boot" =
    {
      device = "/dev/disk/by-uuid/00000000-0000-0000-0000-000000000002"; # REPLACE: boot partition UUID
      fsType = "vfat";
      options = [ "fmask=0077" "dmask=0077" ];
    };

  swapDevices =
    [{
      device = "/dev/disk/by-uuid/00000000-0000-0000-0000-000000000003"; # REPLACE: swap partition UUID
    }];

  # Enables DHCP on each ethernet and wireless interface. In case of scripted networking
  # (the default) this is the recommended approach. When using systemd-networkd it's
  # still possible to use this option, but it's recommended to use it in conjunction
  # with explicit per-interface declarations with `networking.interfaces.<interface>.useDHCP`.
  networking.useDHCP = lib.mkDefault true;
  # networking.interfaces.eno1.useDHCP = lib.mkDefault true;
  # networking.interfaces.wlp12s0.useDHCP = lib.mkDefault true;

  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
  hardware.cpu.intel.updateMicrocode = lib.mkDefault config.hardware.enableRedistributableFirmware;
}
