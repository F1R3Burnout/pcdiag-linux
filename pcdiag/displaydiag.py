"""Display/HDMI diagnostics via DRM sysfs (Linux counterpart of HDMI-Diagnose)."""
import glob
import os
import re
from typing import List, Optional

from .common import ATTENTION, PASS, UNSUPPORTED, Section, have, read, run


def parse_edid(data: bytes) -> Optional[dict]:
    """Minimal EDID base-block parser (vendor, name, preferred timing)."""
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    if sum(data[:128]) % 256 != 0:
        return {"valid": False}
    m = (data[8] << 8) | data[9]
    vendor = "".join(chr(((m >> s) & 0x1F) + 64) for s in (10, 5, 0))
    name = ""
    for off in (54, 72, 90, 108):
        blk = data[off:off + 18]
        if blk[:3] == b"\x00\x00\x00" and blk[3] == 0xFC:
            name = blk[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
    b = data[54:72]
    w = b[2] | ((b[4] & 0xF0) << 4)
    h = b[5] | ((b[7] & 0xF0) << 4)
    clock = (b[0] | (b[1] << 8)) / 100.0
    ht = w + (b[3] | ((b[4] & 0x0F) << 8))
    vt = h + (b[6] | ((b[7] & 0x0F) << 8))
    hz = round(clock * 1e6 / (ht * vt), 1) if ht and vt else 0
    return {"valid": True, "vendor": vendor, "name": name, "preferred": f"{w}x{h}@{hz}",
            "extensions": data[126]}


def collect() -> List[Section]:
    secs: List[Section] = []
    s = Section("Anschlüsse (DRM)")
    conns = sorted(glob.glob("/sys/class/drm/card*-*"))
    if not conns:
        s.add(UNSUPPORTED, "Keine DRM-Anschlüsse gefunden", "Headless-System oder Treiber fehlt.")
    for c in conns:
        name = os.path.basename(c).split("-", 1)[1]
        if read(f"{c}/status", "unknown") != "connected":
            continue
        modes = read(f"{c}/modes").splitlines()
        info = f"{name}: aktiv" + (f", Modus {modes[0]}" if modes else "")
        try:
            with open(f"{c}/edid", "rb") as f:
                e = parse_edid(f.read())
        except OSError:
            e = None
        if e and e.get("valid"):
            s.add(PASS, f"{info} · {e['vendor']} {e['name']} (bevorzugt {e['preferred']})")
            if modes and e["preferred"].split("@")[0] not in modes:
                s.add(ATTENTION, f"{name}: bevorzugter Modus {e['preferred']} nicht in Modusliste")
        elif e is not None:
            s.add(ATTENTION, info, "EDID-Prüfsumme ungültig (Kabel/Adapter/Monitor prüfen)")
        else:
            s.add(ATTENTION, info, "EDID nicht lesbar oder leer")
    if conns and not s.findings:
        s.add(PASS, "Keine Anzeige verbunden")
    secs.append(s)

    s = Section("Sitzung & Treiber")
    s.add(PASS, f"Session-Typ: {os.environ.get('XDG_SESSION_TYPE', 'unbekannt')}")
    if have("xrandr") and os.environ.get("DISPLAY"):
        s.raw = run(["xrandr", "--query"])[1]
    drivers = sorted({os.path.basename(os.readlink(p))
                      for p in glob.glob("/sys/class/drm/card?/device/driver") if os.path.islink(p)})
    s.add(PASS, "GPU-Treiber", ", ".join(drivers) or "nicht ermittelt")
    secs.append(s)

    s = Section("Kernel-Meldungen zur Anzeige")
    rc, out = (run(["journalctl", "-k", "-b", "--no-pager", "-q"], timeout=30) if have("journalctl")
               else run(["dmesg"], timeout=30))
    if rc != 0:
        s.add(UNSUPPORTED, "Kernel-Log nicht lesbar (root?)")
    else:
        pat = re.compile(r"(drm|hdmi|edid|displayport|dp link|amdgpu|i915|nouveau|nvidia).*"
                         r"(error|fail|timeout|link training|flip_done|underrun)", re.I)
        bad = [l for l in out.splitlines() if pat.search(l)]
        s.add(ATTENTION if bad else PASS, f"{len(bad)} Display-Fehlermeldungen", "\n".join(bad[:8]))
        s.raw = "\n".join(bad[:200])
    secs.append(s)

    s = Section("HDMI/DP-Audio")
    if have("aplay"):
        hd = [l for l in run(["aplay", "-l"])[1].splitlines() if re.search(r"HDMI|DisplayPort", l, re.I)]
        s.add(PASS if hd else UNSUPPORTED, f"{len(hd)} HDMI/DP-Audiogeräte", "\n".join(hd[:6]))
    else:
        s.add(UNSUPPORTED, "aplay (alsa-utils) nicht installiert")
    secs.append(s)
    return secs
