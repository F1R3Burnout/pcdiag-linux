"""Distribution detection and dependency installation.

Family is derived from /etc/os-release ID and ID_LIKE, so derivatives (CachyOS,
Manjaro, Linux Mint, Pop!_OS, Nobara ...) resolve to their parent's package manager.
"""
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

from .common import have, is_root, run

FAMILIES = {
    "arch":   {"ids": {"arch", "cachyos", "manjaro", "endeavouros", "garuda", "artix", "arcolinux"},
               "install": ["pacman", "-S", "--needed", "--noconfirm"], "refresh": None},
    "debian": {"ids": {"debian", "ubuntu", "linuxmint", "pop", "raspbian", "zorin", "elementary", "kali", "neon"},
               "install": ["apt-get", "install", "-y"], "refresh": ["apt-get", "update"]},
    "rhel":   {"ids": {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ol"},
               "install": ["dnf", "install", "-y"], "refresh": None},
    "suse":   {"ids": {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles", "sled"},
               "install": ["zypper", "--non-interactive", "install"], "refresh": ["zypper", "--non-interactive", "refresh"]},
}

# command we need  ->  package providing it (identical name on all supported families unless overridden)
CMD_PACKAGES: Dict[str, str] = {
    "smartctl": "smartmontools",
    "memtester": "memtester",
    "lspci": "pciutils",
    "ethtool": "ethtool",
    "iw": "iw",
    "aplay": "alsa-utils",
    "vulkaninfo": "vulkan-tools",
}
PKG_OVERRIDES: Dict[str, Dict[str, str]] = {}   # {family: {generic_pkg: family_pkg}}

TOOL_NEEDS = {
    "sysdiag": ["lspci"],
    "netdiag": ["ethtool", "iw"],
    "displaydiag": ["aplay"],
    "stability": ["smartctl", "memtester", "lspci", "vulkaninfo"],
}

VULKAN_DRIVER_HINT = {
    "arch":   "AMD: vulkan-radeon · Intel: vulkan-intel · NVIDIA: nvidia-utils",
    "debian": "AMD/Intel: mesa-vulkan-drivers · NVIDIA: nvidia-driver (Mint: Treiberverwaltung)",
    "rhel":   "AMD/Intel: mesa-vulkan-drivers · NVIDIA: akmod-nvidia (RPM Fusion)",
    "suse":   "AMD/Intel: Mesa-vulkan-drivers · NVIDIA: nvidia-compute-utils",
}


@dataclass
class Distro:
    id: str = "unknown"
    name: str = "Linux"
    version: str = ""
    family: str = "unknown"
    immutable: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} {self.version}".strip() + (f" [{self.family}]" if self.family != "unknown" else "")


def parse_os_release(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            try:
                parts = shlex.split(v)
            except ValueError:
                parts = [v.strip('"')]
            out[k.strip()] = parts[0] if parts else ""
    return out


def classify(info: Dict[str, str]) -> Distro:
    ids = [info.get("ID", "").lower()] + info.get("ID_LIKE", "").lower().split()
    family = "unknown"
    for cand in ids:
        for fam, spec in FAMILIES.items():
            if cand in spec["ids"]:
                family = fam
                break
        if family != "unknown":
            break
    if family == "unknown":   # fall back to whichever package manager exists
        for fam, spec in FAMILIES.items():
            if have(spec["install"][0]):
                family = fam
                break
    d = Distro(info.get("ID", "unknown"), info.get("PRETTY_NAME") or info.get("NAME", "Linux"),
               "", family)
    if info.get("PRETTY_NAME"):
        d.name, d.version = info["PRETTY_NAME"], ""
    d.immutable = os.path.exists("/run/ostree-booted") or d.id in ("steamos", "bazzite") \
        or info.get("VARIANT_ID", "") in ("silverblue", "kinoite", "coreos", "sericea", "onyx")
    return d


def detect() -> Distro:
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(p, errors="replace") as f:
                return classify(parse_os_release(f.read()))
        except OSError:
            continue
    return classify({})


def missing_packages(distro: Distro, tool: Optional[str] = None,
                     present=have) -> List[str]:
    cmds = TOOL_NEEDS.get(tool, list(CMD_PACKAGES)) if tool else list(CMD_PACKAGES)
    pk = []
    for c in cmds:
        if not present(c):
            name = PKG_OVERRIDES.get(distro.family, {}).get(CMD_PACKAGES[c], CMD_PACKAGES[c])
            if name not in pk:
                pk.append(name)
    return pk


def install_commands(distro: Distro, packages: List[str]) -> List[List[str]]:
    spec = FAMILIES.get(distro.family)
    if not spec or not packages:
        return []
    cmds = []
    if spec["refresh"]:
        cmds.append(spec["refresh"])
    cmds.append(spec["install"] + packages)
    return cmds


def ensure_dependencies(tool: str, assume_yes: bool = False, allow_install: bool = True,
                        log=print, ask=input) -> List[str]:
    """Offer to install missing helper programs. Returns the packages still missing."""
    d = detect()
    pk = missing_packages(d, tool)
    if not pk:
        return []
    if not allow_install or d.family == "unknown":
        log(f"Optionale Pakete fehlen ({' '.join(pk)}) - Installation übersprungen.")
        return pk
    if d.immutable:
        log(f"{d.label} ist ein unveränderliches System - bitte manuell installieren: {' '.join(pk)}")
        return pk
    cmds = install_commands(d, pk)
    prefix = [] if is_root() else (["sudo"] if have("sudo") else None)
    if prefix is None:
        log("Weder root noch sudo verfügbar - Pakete bitte manuell installieren: " + " ".join(pk))
        return pk
    log(f"Erkannt: {d.label}. Fehlende optionale Pakete: {' '.join(pk)}")
    if not assume_yes:
        if not sys.stdin.isatty():
            log("Keine interaktive Eingabe - mit --yes automatisch installieren oder manuell: "
                + " ".join(prefix + cmds[-1]))
            return pk
        if ask("Jetzt installieren? [J/n] ").strip().lower() not in ("", "j", "y", "ja", "yes"):
            return pk
    for c in cmds:
        rc, out = run(prefix + c, timeout=900)
        if rc != 0:
            log(f"Befehl fehlgeschlagen: {' '.join(c)}\n{out.strip()[-300:]}")
            if c is cmds[-1]:
                break
    still = missing_packages(d, tool)
    log("Installation abgeschlossen." if not still else f"Weiterhin fehlend: {' '.join(still)}")
    return still
