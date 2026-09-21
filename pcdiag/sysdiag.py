"""System overview (Linux counterpart of PCDiagLite)."""
import json
import os
import platform
import re
from typing import List

from . import distro
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

    s = Section("System")
    osname = read("/etc/os-release").split("\n")[0]
    s.data = {"kernel": platform.release(), "machine": platform.machine(), "os": osname,
              "uptime_s": read("/proc/uptime").split(" ")[0]}
    d = distro.detect()
    s.data["distro"] = d.label
    s.add(PASS, f"{platform.node()} · {platform.release()}", f"{d.label} (Paketfamilie: {d.family})")
    if not is_root():
        s.add(ATTENTION, "Nicht als root gestartet",
              "dmesg/SMART/DMI sind ohne root eingeschränkt (sudo verwenden).")
    secs.append(s)

    s = Section("CPU & RAM")
    cpu = [l.split(":", 1)[1].strip() for l in read("/proc/cpuinfo").splitlines()
           if l.startswith("model name")]
    s.add(PASS, f"{cpu[0] if cpu else 'CPU unbekannt'} ({len(cpu)} Threads)")
    mi = parse_meminfo(read("/proc/meminfo"))
    if mi:
        tot, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        s.add(PASS, f"RAM {tot // 1024} MiB gesamt, {avail // 1024} MiB verfügbar")
        if tot and avail / tot < 0.10:
            s.add(ATTENTION, "Weniger als 10 % RAM verfügbar")
        sw = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
        if mi.get("SwapTotal") and sw / mi["SwapTotal"] > 0.5:
            s.add(ATTENTION, "Swap zu über 50 % belegt")
    secs.append(s)

    s = Section("Datenträger")
    rc, out = run(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL"])
    if rc == 0:
        try:
            for d in json.loads(out).get("blockdevices", []):
                if d.get("type") == "disk":
                    s.add(PASS, f"/dev/{d['name']} {d.get('size', '?')} {d.get('model') or ''}".strip())
        except ValueError:
            pass
    st = os.statvfs("/")
    free = st.f_bavail / st.f_blocks if st.f_blocks else 1
    s.add(ATTENTION if free < 0.10 else PASS, f"Root-Dateisystem {free * 100:.0f} % frei")
    rc, out = run(["findmnt", "-rno", "OPTIONS", "/"])
    if rc == 0 and "ro" in out.strip().split(","):
        s.add(FAIL, "Root-Dateisystem ist read-only gemountet", "Hinweis auf Dateisystemfehler.")
    secs.append(s)

    s = Section("Dienste & Kernel-Log")
    if have("systemctl"):
        rc, out = run(["systemctl", "--failed", "--no-legend", "--plain"])
        failed = [l for l in out.splitlines() if l.strip()]
        s.add(ATTENTION if failed else PASS, f"{len(failed)} fehlgeschlagene systemd-Units",
              "; ".join(failed[:8]))
    else:
        s.add(UNSUPPORTED, "systemctl nicht verfügbar")
    if have("journalctl"):
        rc, out = run(["journalctl", "-k", "-b", "-p", "warning", "--no-pager", "-q"], timeout=30)
    else:
        rc, out = run(["dmesg", "--level=err,warn"], timeout=30)
    bad = classify_kernel_lines(out.splitlines()) if rc == 0 else []
    if rc != 0:
        s.add(UNSUPPORTED, "Kernel-Log nicht lesbar", out.strip()[:200])
    elif bad:
        s.add(ATTENTION, f"{len(bad)} auffällige Kernel-Meldungen (dieser Boot)", "\n".join(bad[:10]))
    else:
        s.add(PASS, "Keine auffälligen Kernel-Meldungen (dieser Boot)")
    s.raw = "\n".join(bad[:200])
    secs.append(s)

    s = Section("GPU & PCI")
    rc, out = run(["lspci", "-nn"]) if have("lspci") else (127, "")
    if rc == 0:
        for l in out.splitlines():
            if re.search(r"VGA|3D controller|Display", l):
                s.add(PASS, l.split(" ", 1)[1][:140])
    else:
        s.add(UNSUPPORTED, "lspci nicht verfügbar", "pciutils installieren")
    secs.append(s)
    return secs
