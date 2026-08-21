"""Where files live, as one definition per convention."""

from pathlib import Path

# Unraid's own path for a VM's EFI variables.
NVRAM_DIR = "/etc/libvirt/qemu/nvram"

# The disk file inside <domains>/<VM name>/, as the VM manager names it.
DISK_NAME = "vdisk1.qcow2"

VM_ICON_NAME = "Hassio_2.png"

# Container side of the template's "VM Icons" mount; the host side is Unraid's
# /usr/local/emhttp/plugins/dynamix.vm.manager/templates/images.
ICONS_DIR = "/icons"

# Unraid's VM Manager settings, written by its Settings -> VM Manager page. The
# mount is optional, so readers treat an absent file as "could not check".
DOMAIN_CFG_PATH = "/boot/config/domain.cfg"

USER_SHARE_ROOT = "/mnt/user"


def nvram_name(uuid: str) -> str:
    return f"{uuid}_VARS-pure-efi.fd"


def nvram_path(uuid: str, nvram_dir: str = NVRAM_DIR) -> str:
    return str(Path(nvram_dir) / nvram_name(uuid))


def disk_path(domains_root: str, vm_name: str) -> str:
    """The VM's disk under a domains share, from whichever side you are looking."""
    return str(Path(domains_root) / vm_name / DISK_NAME)
