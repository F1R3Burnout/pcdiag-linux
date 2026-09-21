"""Network diagnostics (Linux counterpart of NetzwerkDiagnose)."""
import re
import socket
from typing import List, Optional

from .common import ATTENTION, FAIL, PASS, UNSUPPORTED, Section, have, read, run


def parse_default_gateway(route_text: str) -> Optional[str]:
    m = re.search(r"^default via (\S+)", route_text, re.M)
    return m.group(1) if m else None


def parse_ping_loss(text: str) -> Optional[float]:
    m = re.search(r"([\d.]+)% packet loss", text)
    return float(m.group(1)) if m else None


def parse_ping_avg(text: str) -> Optional[float]:
    m = re.search(r"= [\d.]+/([\d.]+)/", text)
    return float(m.group(1)) if m else None


def _ping(host: str, n: int = 5):
    rc, out = run(["ping", "-c", str(n), "-W", "2", host], timeout=n * 3 + 5)
    return parse_ping_loss(out), parse_ping_avg(out), out


def collect() -> List[Section]:
    secs: List[Section] = []
    s = Section("Schnittstellen")
    rc, out = run(["ip", "-brief", "addr"])
    s.raw = out
    if rc != 0:
        s.add(UNSUPPORTED, "iproute2 nicht verfügbar")
    else:
        up = [l for l in out.splitlines() if " UP " in l and not l.startswith("lo")]
        s.add(PASS if up else FAIL, f"{len(up)} aktive Schnittstelle(n)", "; ".join(up[:5]))
    secs.append(s)

    s = Section("Routing & Gateway")
    rc, rt = run(["ip", "route"])
    gw = parse_default_gateway(rt)
    if not gw:
        s.add(FAIL, "Kein Default-Gateway")
    else:
        loss, avg, raw = _ping(gw)
        s.raw = raw
        if loss is None:
            s.add(UNSUPPORTED, "ping nicht auswertbar", raw[:120])
        elif loss > 0:
            s.add(ATTENTION if loss < 50 else FAIL, f"Gateway {gw}: {loss:.0f} % Paketverlust")
        else:
            s.add(PASS, f"Gateway {gw} erreichbar", f"Ø {avg} ms" if avg is not None else "")
    secs.append(s)

    s = Section("Internet & DNS")
    loss, avg, _ = _ping("1.1.1.1")
    if loss is None:
        s.add(UNSUPPORTED, "Ping zu 1.1.1.1 nicht auswertbar")
    else:
        s.add(PASS if loss == 0 else ATTENTION if loss < 50 else FAIL,
              f"1.1.1.1: {loss:.0f} % Verlust", f"Ø {avg} ms" if avg is not None else "")
    try:
        socket.setdefaulttimeout(4)
        socket.gethostbyname("github.com")
        s.add(PASS, "DNS-Auflösung funktioniert")
    except OSError as e:
        s.add(FAIL if loss == 0 else ATTENTION, "DNS-Auflösung fehlgeschlagen", str(e))
    s.add(PASS, "resolv.conf", read("/etc/resolv.conf").replace("\n", " ")[:160])
    secs.append(s)

    s = Section("Verbindungsqualität")
    for line in run(["ip", "-brief", "link"])[1].splitlines():
        if not line.split():
            continue
        ifc = line.split()[0].split("@")[0]
        if ifc == "lo":
            continue
        if have("ethtool") and not ifc.startswith(("wl", "docker", "veth", "br", "virbr")):
            rc, out = run(["ethtool", ifc])
            m = re.search(r"Speed: (\S+)", out)
            if m and "Unknown" not in m.group(1):
                sp = m.group(1)
                s.add(ATTENTION if sp in ("10Mb/s", "100Mb/s") else PASS, f"{ifc}: Link {sp}")
        if have("iw") and ifc.startswith("wl"):
            rc, out = run(["iw", "dev", ifc, "link"])
            m = re.search(r"signal: (-?\d+) dBm", out)
            if m:
                dbm = int(m.group(1))
                s.add(ATTENTION if dbm < -75 else PASS, f"{ifc}: WLAN-Signal {dbm} dBm")
    if not s.findings:
        s.add(UNSUPPORTED, "Keine Linkdaten (ethtool/iw fehlen oder keine physische Schnittstelle)")
    secs.append(s)
    return secs
