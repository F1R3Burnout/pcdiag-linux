"""System overview (Linux counterpart of PCDiagLite)."""
import json
import os
import platform
import re
from typing import List

from . import distro, events, remote_desktop, stability
from .common import (ATTENTION, FAIL, PASS, UNSUPPORTED, Section, have, is_root,
                     read, run)

KERNEL_ERR = re.compile(
    r"(machine check|mce:|hardware error|edac|aer:|pcie bus error|i/o error|"
    r"ata\d+.*(error|failed)|nvme.*(timeout|reset|error)|blk_update_request|"
    r"oom-killer|out of memory|segfault|call trace|gpu hang|amdgpu.*(error|timeout|ring)|"
    r"nvrm: xid|i915.*(error|hang)|thermal.*(throttl|critical)|mmc.*error)", re.I)


def parse_meminfo(text: str) -> dict:
    d = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            d[m.group(1)] = int(m.group(2))
    return d


def classify_kernel_lines(lines: List[str]) -> List[str]:
    return [l for l in lines if KERNEL_ERR.search(l)]


def collect() -> List[Section]:
    secs: List[Section] = []

    s = Section("System & Hardware auf einen Blick", expanded=True)
    d = distro.detect()
    uptime = read("/proc/uptime").split(" ")[0]
    s.data = {"kernel": platform.release(), "machine": platform.machine(),
              "distro": d.label, "uptime_s": uptime}
    s.add(PASS, f"{platform.node()} · {d.label}",
          f"Kernel {platform.release()} · {platform.machine()}", "/etc/os-release; uname")
    if not is_root():
        s.add(ATTENTION, "Nicht als root gestartet",
              "dmesg/SMART/DMI sind ohne root eingeschränkt (sudo verwenden).")
    cpu = [l.split(":", 1)[1].strip() for l in read("/proc/cpuinfo").splitlines()
           if l.startswith("model name")]
    if cpu:
        s.add(PASS, f"{cpu[0]} ({len(cpu)} logische Threads)", source="/proc/cpuinfo")
    mi = parse_meminfo(read("/proc/meminfo"))
    if mi:
        tot, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        s.add(PASS, f"RAM {tot / (1024 * 1024):.1f} GiB gesamt, {avail / (1024 * 1024):.1f} GiB verfügbar",
              source="/proc/meminfo: MemTotal, MemAvailable")
        if tot and avail / tot < 0.10:
            s.add(ATTENTION, "Weniger als 10 % RAM verfügbar", source="/proc/meminfo: MemAvailable")
        sw = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
        if mi.get("SwapTotal") and sw / mi["SwapTotal"] > 0.5:
            s.add(ATTENTION, "Swap zu über 50 % belegt", source="/proc/meminfo: SwapTotal, SwapFree")
    board = " ".join(filter(None, (read("/sys/class/dmi/id/board_vendor"), read("/sys/class/dmi/id/board_name"))))
    firmware = read("/sys/class/dmi/id/bios_version")
    if board or firmware:
        s.add(PASS, board or "Firmware", f"BIOS/UEFI {firmware}" if firmware else "",
              "/sys/class/dmi/id/{board_vendor,board_name,bios_version}")
    rc, out = run(["lspci", "-nnk"])
    if rc == 0:
        gpus = re.findall(r"^\S+ (?:VGA compatible controller|3D controller|Display controller)"
                          r"(?: \[[0-9a-fA-F]{4}\])?: .+$", out, re.M)
        for gpu in gpus:
            s.add(PASS, gpu, source="lspci -nnk")
    else:
        s.add(UNSUPPORTED, "PCI-Grafikadapter nicht ermittelt", out[:200], "lspci -nnk")
    secs.extend([s, events.restart_context(), events.hardware_context(), events.application_crashes()])

    s = Section("Datenträger")
    rc, out = run(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL"])
    if rc == 0:
        try:
            for d in json.loads(out).get("blockdevices", []):
                if d.get("type") == "disk":
                    s.add(PASS, f"/dev/{d['name']} {d.get('size', '?')} {d.get('model') or ''}".strip(),
                          source="lsblk -J -o NAME,SIZE,TYPE,MODEL")
        except ValueError:
            s.add(UNSUPPORTED, "lsblk-Ausgabe nicht lesbar")
    else:
        s.add(UNSUPPORTED, "Datenträger nicht ermittelt", out[:200], "lsblk -J")
    st = os.statvfs("/")
    free = st.f_bavail / st.f_blocks if st.f_blocks else 1
    s.add(ATTENTION if free < 0.10 else PASS, f"Root-Dateisystem {free * 100:.0f} % frei",
          source="statvfs /")
    rc, out = run(["findmnt", "-rno", "OPTIONS", "/"])
    if rc == 0 and "ro" in out.strip().split(","):
        s.add(ATTENTION, "Root-Dateisystem ist read-only gemountet",
              "Kann beabsichtigt sein; bei einem beschreibbaren Image die Kernel-Meldungen prüfen.",
              "findmnt -rno OPTIONS /")
    secs.append(s)
    if is_root() and have("smartctl"):
        secs.extend(stability.smart_test())
    else:
        s = Section("Datenträgergesundheit (SMART)")
        s.add(UNSUPPORTED, "SMART nicht abgefragt",
              "Benötigt smartctl und Lesezugriff auf die Geräte (in der Regel sudo).")
        secs.append(s)

    s = Section("Systemdienste")
    if have("systemctl"):
        rc, out = run(["systemctl", "--failed", "--no-legend", "--plain"])
        if rc == 0:
            failed = [l for l in out.splitlines() if l.strip()]
            s.add(ATTENTION if failed else PASS, f"{len(failed)} fehlgeschlagene systemd-Units",
                  "\n".join(failed[:12]), "systemctl --failed --no-legend --plain")
        else:
            s.add(UNSUPPORTED, "systemd-Dienstestatus nicht verfügbar", out[:200], "systemctl --failed")
    else:
        s.add(UNSUPPORTED, "systemctl nicht verfügbar")
    secs.append(s)

    secs.append(remote_desktop.check())

    return secs
