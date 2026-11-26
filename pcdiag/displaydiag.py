"""Display/HDMI diagnostics via DRM sysfs (Linux counterpart of HDMI-Diagnose)."""
import glob
import json
import os
import re
from typing import List, Optional

from . import events
from .common import ATTENTION, PASS, SKIPPED, UNSUPPORTED, Section, have, read, run


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
    preferred = f"{w}x{h}@{hz}" if clock and w and h and hz else ""
    ext_count = data[126]
    ext_ok = len(data) >= (1 + ext_count) * 128
    hdr = set()
    for i in range(1, ext_count + 1):
        block = data[i * 128:(i + 1) * 128]
        if len(block) != 128 or sum(block) % 256 != 0:
            ext_ok = False
            continue
        if block[0] != 0x02 or not 4 <= block[2] <= 127:
            continue
        pos, end = 4, block[2]
        while pos < end:
            kind, size = block[pos] >> 5, block[pos] & 0x1f
            if pos + size >= end:
                break
            if kind == 7 and size >= 2 and block[pos + 1] == 6:
                eotf = block[pos + 2]
                hdr.update(name for bit, name in ((4, "HDR10/PQ"), (8, "HLG")) if eotf & bit)
            pos += size + 1
    return {"valid": True, "vendor": vendor, "name": name, "preferred": preferred,
            "extensions": ext_count, "extensions_valid": ext_ok, "hdr_formats": sorted(hdr)}


def parse_kscreen(data: dict) -> dict:
    out = {}
    for entry in data.get("outputs", []):
        name = entry.get("name")
        if not name or not entry.get("connected"):
            continue
        current = entry.get("currentModeId")
        mode = next((m for m in entry.get("modes", []) if str(m["id"]) == str(current)), {})
        row = {"enabled": bool(entry.get("enabled"))}
        if row["enabled"] and mode:
            size, rate = mode.get("size", {}), mode.get("refreshRate")
            if size.get("width") and size.get("height") and isinstance(rate, (int, float)):
                row["active_mode"] = f"{size['width']}x{size['height']}@{rate:.2f}"
        for key in ("hdr", "hdrEnabled", "highDynamicRange"):
            if isinstance(entry.get(key), bool):
                row["hdr_active"] = entry[key]
                break
        out[name] = row
    return out


def parse_xrandr(text: str) -> dict:
    result, name = {}, None
    for line in text.splitlines():
        m = re.match(r"^(\S+) (connected|disconnected)\b", line)
        if m:
            name = m[1] if m[2] == "connected" else None
            if name:
                result[name] = {"enabled": False}
        elif name:
            m = re.match(r"^\s+(\d+x\d+)\s+.*?([\d.]+)\*", line)
            if m:
                result[name] = {"enabled": True, "active_mode": f"{m[1]}@{m[2]}"}
    return result


def compare_state(previous: dict, current: dict) -> Section:
    s = Section("Vergleich mit vorherigem Display-Report", expanded=True)
    for name in sorted(previous.keys() | current.keys()):
        old, new = previous.get(name), current.get(name)
        if old is None or new is None:
            s.add(ATTENTION, f"{name}: {'neu erfasst' if old is None else 'nicht mehr erfasst'}")
            continue
        for field in ("connected", "enabled", "active_mode", "hdr_active", "hdr_formats"):
            if field in old and field in new and old[field] != new[field]:
                s.add(ATTENTION, f"{name}: {field} geändert", f"{old[field]} → {new[field]}",
                      "Vergleich der DRM-/Sitzungsmesswerte beider Reports")
    if not s.findings:
        s.add(PASS, "Keine Änderung in den gemeinsam verfügbaren Messwerten")
    return s


def collect(compare: str = "") -> List[Section]:
    secs: List[Section] = []
    session = os.environ.get("XDG_SESSION_TYPE", "")
    drm = {}
    session_data, backend = {}, ""
    if os.environ.get("WAYLAND_DISPLAY") and have("kscreen-doctor"):
        rc, out = run(["kscreen-doctor", "-j"], timeout=5, stdout_only=True)
        if rc == 0:
            try:
                session_data = parse_kscreen(json.loads(out))
                backend = "kscreen-doctor -j"
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
    elif os.environ.get("DISPLAY") and session != "wayland" and have("xrandr"):
        rc, out = run(["xrandr", "--current"], timeout=5)
        if rc == 0:
            session_data, backend = parse_xrandr(out), "xrandr --current"
    s = Section("Anschlüsse & Monitor (DRM / EDID)", expanded=True)
    conns = sorted(glob.glob("/sys/class/drm/card*-*"))
    if not conns:
        s.add(UNSUPPORTED, "Keine DRM-Anschlüsse gefunden", "Headless-System oder Treiber fehlt.")
    for c in conns:
        key = os.path.basename(c)
        name = key.split("-", 1)[1]
        status = read(f"{c}/status", "unknown")
        row = drm[key] = {"status": status}
        if status != "unknown":
            row["connected"] = status == "connected"
        if status != "connected":
            continue
        modes = read(f"{c}/modes").splitlines()
        row["advertised_modes"] = modes[:40]
        selected = session_data.get(name)
        if not selected and backend.startswith("xrandr"):
            m = re.fullmatch(r"HDMI-A-(\d+)", name)
            if m:
                selected = session_data.get(f"HDMI-{m[1]}")
        if selected and sum(p.rsplit("/", 1)[-1].split("-", 1)[1] == name for p in conns) == 1:
            row.update(selected)
            if row.get("active_mode"):
                mode_res, rate = row["active_mode"].split("@", 1)
                s.add(PASS, f"{name}: aktiv mit {mode_res} bei {rate} Hz", source=backend)
        info = f"{name}: verbunden"
        try:
            with open(f"{c}/edid", "rb") as f:
                e = parse_edid(f.read())
        except OSError:
            e = None
        if e and e.get("valid"):
            desc = [f"EDID bevorzugt {e['preferred']}"] if e["preferred"] else []
            if e["hdr_formats"]:
                desc.append("EDID nennt " + ", ".join(e["hdr_formats"]) + " (Fähigkeit, nicht Aktivstatus)")
            row.update({k: e[k] for k in ("vendor", "name", "preferred") if e[k]})
            if e["extensions_valid"]:
                row["hdr_formats"] = e["hdr_formats"]
            s.add(PASS, f"{info} · {e['vendor']} {e['name']}".strip(),
                  " · ".join(desc), f"{c}/edid")
            if not e["extensions_valid"]:
                s.add(ATTENTION, f"{name}: EDID-Erweiterungen unvollständig oder fehlerhaft",
                      source=f"{c}/edid")
        elif e is not None:
            s.add(ATTENTION, info, "EDID-Prüfsumme ungültig (Kabel/Adapter/Monitor prüfen)", f"{c}/edid")
        else:
            s.add(ATTENTION, info, "EDID nicht lesbar oder leer", f"{c}/edid")
    if conns and not s.findings:
        known = all(row.get("status") != "unknown" for row in drm.values())
        s.add(SKIPPED if known else UNSUPPORTED,
              "Keine Anzeige verbunden" if known else "Anschlussstatus nicht vollständig lesbar",
              source="/sys/class/drm/*/status")
    s.data["connectors"] = drm
    secs.append(s)

    if compare:
        try:
            with open(compare, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("tool") != "displaydiag":
                raise ValueError("Kein Display-Report")
            previous = next((x.get("data", {}).get("connectors") for x in old.get("sections", [])
                             if "connectors" in x.get("data", {})), None)
            if not isinstance(previous, dict) or not all(isinstance(v, dict) for v in previous.values()):
                raise ValueError("Report enthält keine lesbaren Anschlussdaten")
        except (OSError, ValueError, TypeError, AttributeError) as e:
            s = Section("Vergleich mit vorherigem Display-Report")
            s.add(UNSUPPORTED, "Vergleich nicht möglich", str(e), compare)
            secs.append(s)
        else:
            secs.append(compare_state(previous, drm))

    s = Section("Aktiver Modus, HDR & Treiber")
    if not session_data:
        s.add(UNSUPPORTED, "Aktiver Modus und HDR-Status nicht aus der Sitzung lesbar",
              "Für KDE-Wayland bzw. X11 in der grafischen Sitzung als angemeldeter Nutzer starten.")
    else:
        s.data["session_outputs"] = session_data
        for name, row in session_data.items():
            if "hdr_active" in row:
                s.add(PASS, f"{name}: HDR {'aktiv' if row['hdr_active'] else 'aus'}", source=backend)
        if not any("hdr_active" in row for row in session_data.values()):
            s.add(UNSUPPORTED, "Sitzung meldet keinen tatsächlichen HDR-Status", source=backend)
    drivers = sorted({os.path.basename(os.path.realpath(p))
                      for p in glob.glob("/sys/class/drm/card[0-9]*/device/driver") if os.path.islink(p)})
    if drivers:
        s.add(PASS, "GPU-Treiber", ", ".join(drivers), "/sys/class/drm/card*/device/driver")
    secs.append(s)

    s = Section("Kernel-Meldungen zur Anzeige")
    rows, error = events.query("--system", "-k", "-b", "0", "-p", "warning", limit=400)
    if error or not rows:
        s.add(UNSUPPORTED, "Keine lesbaren Kernel-Warnungen zur Prüfung", error,
              "journalctl --system -k -b 0 -p warning")
    else:
        pat = re.compile(r"(drm|hdmi|edid|displayport|dp link|amdgpu|i915|nouveau|nvidia).*"
                         r"(error|fail|timeout|link training|flip_done|underrun)", re.I)
        bad = [r for r in rows if pat.search(r["MESSAGE"])]
        if not bad:
            s.add(PASS, "Kein bekanntes Display-Fehlermuster in den gelesenen Warnungen",
                  source="journalctl --system -k -b 0 -p warning")
        for r in bad[-12:]:
            s.add(ATTENTION, r["MESSAGE"][:350], source=events.source(r))
    secs.append(s)

    s = Section("HDMI/DP-Audio")
    if have("aplay"):
        rc, out = run(["aplay", "-l"], timeout=5)
        hd = [l for l in out.splitlines() if re.search(r"HDMI|DisplayPort", l, re.I)] if rc == 0 else []
        s.add(PASS if hd else UNSUPPORTED, f"{len(hd)} HDMI/DP-Audioausgänge erkannt",
              "\n".join(hd[:6]) or out[:180], "aplay -l (keine Wiedergabeprüfung)")
    else:
        s.add(UNSUPPORTED, "aplay (alsa-utils) nicht installiert")
    secs.append(s)
    return secs
