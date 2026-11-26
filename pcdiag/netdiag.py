"""Read-only network diagnostics with physical LAN and VPN kept separate."""
import ipaddress
import json
import os
import re
import shutil
import socket
import time
from typing import List, Optional

from . import events
from .common import ATTENTION, PASS, SKIPPED, UNSUPPORTED, Section, have, read, run

VPN_KINDS = {"tun", "tap", "wireguard", "xfrm", "vti", "vti6", "gre", "gretap", "ip6gre"}
VPN_NAME = re.compile(r"^(?:tun|tap|wg|vpn|ppp)(?:\d|[-_])|^(?:tailscale|zerotier|zt|mullvad|proton|warp|nordlynx)", re.I)
LINK_PROBLEM = re.compile(r"carrier lost|link (?:is )?down|deauthenticat|reason ['\"]carrier-lost|"
                          r"(?:dns|dhcp).*?(?:fail|timed out|timeout (?:expired|occurred))|"
                          r"state change: activated -> (?:failed|deactivating|disconnected)", re.I)


def parse_default_gateway(route_text: str) -> Optional[str]:
    m = re.search(r"^default via (\S+)", route_text, re.M)
    return m.group(1) if m else None


def parse_ping_loss(text: str) -> Optional[float]:
    m = re.search(r"([\d.]+)% packet loss", text)
    return float(m.group(1)) if m else None


def parse_ping_avg(text: str) -> Optional[float]:
    m = re.search(r"= [\d.]+/([\d.]+)/", text)
    return float(m.group(1)) if m else None


def _ip(*args) -> tuple:
    rc, out = run(["ip", "-j", *args], timeout=5, stdout_only=True)
    if rc == 0:
        try:
            value = json.loads(out)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value, ""
        except ValueError:
            pass
    return [], out.strip()[:240] or f"ip: Exit {rc}"


def is_vpn(link: dict) -> bool:
    name = link.get("ifname", "")
    kind = (link.get("linkinfo") or {}).get("info_kind")
    return kind in VPN_KINDS or bool(VPN_NAME.search(name))


def _addresses(link: dict) -> str:
    return ", ".join(f"{r['local']}/{r['prefixlen']}" for r in link.get("addr_info", [])
                     if r.get("scope") in ("global", "link") and r.get("local") and "prefixlen" in r)


def _is_up(link: dict) -> bool:
    return link.get("operstate") == "UP" or (
        link.get("operstate") == "UNKNOWN" and "UP" in link.get("flags", []) and bool(_addresses(link)))


def _route_text(r: dict) -> str:
    return " · ".join(str(x) for x in (r.get("dst"), r.get("gateway"), r.get("dev"),
                                       f"Metrik {r['metric']}" if "metric" in r else "") if x)


def _ping(s: Section, host: str, label: str, n: int = 3) -> None:
    cmd = ["ping", "-n", "-c", str(n), "-W", "2", host]
    rc, out = run(cmd, timeout=n * 3 + 3)
    loss, avg = parse_ping_loss(out), parse_ping_avg(out)
    if loss is None:
        s.add(UNSUPPORTED, f"{label}: kein ICMP-Messwert", out[:220], " ".join(cmd))
    else:
        s.data[host] = {"loss_percent": loss, **({"avg_ms": avg} if avg is not None else {})}
        s.add(PASS if loss == 0 else ATTENTION,
              f"{label}: {'erreichbar' if loss == 0 else f'{loss:g} % Paketverlust'}",
              f"Ø {avg:g} ms" if avg is not None else "ICMP kann durch eine Firewall blockiert sein.",
              " ".join(cmd))


def _dns(s: Section, name: str) -> list:
    started = time.monotonic()
    cmd = ["getent", "ahosts", name]
    rc, out = run(cmd, timeout=5)
    found = []
    if rc == 0:
        for line in out.splitlines():
            cols = line.split()
            if not cols:
                continue
            try:
                addr = str(ipaddress.ip_address(cols[0]))
            except ValueError:
                continue
            if addr not in found:
                found.append(addr)
    if found:
        ms = round((time.monotonic() - started) * 1000, 1)
        s.data[name] = {"addresses": found, "elapsed_ms": ms}
        s.add(PASS, f"{name}: aufgelöst", f"{ms:g} ms · {', '.join(found[:8])}", " ".join(cmd))
    else:
        status = UNSUPPORTED if rc == 127 else ATTENTION
        s.add(status, f"{name}: nicht aufgelöst", out.strip()[:220] or f"getent: Exit {rc}", " ".join(cmd))
    return found


def _tcp(s: Section, name: str, addresses: list) -> None:
    if not addresses:
        s.add(SKIPPED, f"{name}:443", "Keine IP aus der DNS-Prüfung.")
        return
    for address in addresses[:3]:
        af = socket.AF_INET6 if ":" in address else socket.AF_INET
        started = time.monotonic()
        try:
            with socket.socket(af, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                sock.connect((address, 443))
            ms = (time.monotonic() - started) * 1000
            s.add(PASS, f"{name}:443 erreichbar", f"{address} · {ms:.1f} ms", "TCP connect (kein TLS-Test)")
            return
        except OSError as err:
            reason = str(err)
    s.add(ATTENTION, f"{name}:443 nicht erreichbar", reason, "TCP connect, bis zu drei DNS-Adressen")


def parse_speedtest(data: dict) -> dict:
    server = data["server"]
    official = isinstance(data["download"], dict)
    latency = data["ping"]["latency"] if official else data["ping"]
    factor = 8 / 1e6 if official else 1 / 1e6
    def throughput(key):
        val = data[key]["bandwidth"] if official else data[key]
        return round(float(val) * factor, 2)
    return {"download_mbps": throughput("download"), "upload_mbps": throughput("upload"),
            "ping_ms": round(float(latency), 2),
            "server": " · ".join(str(server[k]) for k in ("name", "location", "country", "host", "id")
                                if server.get(k)) or "nicht gemeldet"}


def _speedtest() -> Section:
    s = Section("Internet-Speedtest", expanded=True)
    if have("speedtest-cli"):
        cmd = [shutil.which("speedtest-cli"), "--json"]
    elif have("speedtest"):
        cmd = [shutil.which("speedtest"), "--format=json"]
    else:
        s.add(UNSUPPORTED, "Kein speedtest-Client vorhanden",
              "Optional den Client selbst installieren. Das Tool lädt ihn nicht herunter.")
        return s
    rc, out = run(cmd, timeout=100, stdout_only=True)
    try:
        if rc:
            raise ValueError(f"Exit {rc}: {out.strip()[:230]}")
        s.data = parse_speedtest(json.loads(out))
    except (ValueError, KeyError, TypeError) as err:
        s.add(UNSUPPORTED, "Speedtest ohne vollständigen Messwert", str(err),
              " ".join(cmd))
    else:
        s.add(PASS, f"{s.data['download_mbps']:g} Mbit/s Download · {s.data['upload_mbps']:g} Mbit/s Upload"
              f" · {s.data['ping_ms']:g} ms Ping", s.data["server"], " ".join(cmd))
    return s


def collect(lan_only: bool = False, speedtest: bool = False) -> List[Section]:
    links, link_error = _ip("-d", "address", "show")
    links = [r for r in links if r.get("ifname") and r["ifname"] != "lo"]
    vpn = {r["ifname"] for r in links if is_vpn(r)}
    main = [r for r in links if r["ifname"] not in vpn]
    adapters = Section("Netzwerkadapter (ohne VPN)", expanded=True)
    if link_error:
        adapters.add(UNSUPPORTED, "Adapterdaten fehlen", link_error, "ip -j -d address show")
    for r in main:
        name = r["ifname"]
        detail = _addresses(r)
        state = r.get("operstate", "unbekannt")
        adapters.add(PASS if _is_up(r) else SKIPPED, f"{name} · {state}", detail, "ip -j -d address show")
    if links and not main:
        adapters.add(ATTENTION, "Nur Tunneladapter gefunden")
    elif not main and not link_error:
        adapters.add(ATTENTION, "Keine Netzwerkschnittstelle außer Loopback gefunden")

    routing = Section("Routing (IPv4 / IPv6, ohne VPN)")
    pings = Section("Ping & Paketverlust", expanded=True)
    dns = Section("DNS-Diagnose (Systemresolver)", expanded=True)
    tcp = Section("TCP-Erreichbarkeit")
    tunnels = Section("VPN · Adapter, IP-Konfiguration & Routing")
    for r in links:
        if r["ifname"] in vpn:
            has_address = any(a.get("scope") == "global" for a in r.get("addr_info", []))
            tunnels.add(PASS if _is_up(r) and has_address else SKIPPED,
                        f"{r['ifname']} · {r.get('operstate', '?')}",
                        _addresses(r), "ip -j -d address show")

    for family in ("-4", "-6"):
        routes, error = _ip(family, "route", "show", "table", "all")
        source = f"ip -j {family} route show table all"
        if error:
            routing.add(UNSUPPORTED, f"Routenabfrage {family} fehlgeschlagen", error, source)
            continue
        main_routes = [r for r in routes if r.get("dev") not in vpn
                       and r.get("table") not in ("local", 255) and r.get("dst")]
        routing.data[family] = [_route_text(r) for r in main_routes[:90]]
        vpn_routes = list(dict.fromkeys(_route_text(r) for r in routes if r.get("dev") in vpn
                                       and r.get("table") not in ("local", 255) and r.get("dst")))
        if vpn_routes:
            tunnels.data[family] = vpn_routes[:100]
        defaults = [r for r in main_routes if r.get("dst") == "default"]
        for r in defaults:
            routing.add(PASS, f"IPv{family[1]}-Standardroute", _route_text(r), source)
        for r in defaults[:2]:
            gateway = r.get("gateway")
            if gateway:
                target = gateway + "%" + r["dev"] if gateway.startswith("fe80:") and r.get("dev") else gateway
                _ping(pings, target, f"Gateway {gateway}")
    if not routing.findings:
        routing.add(ATTENTION, "Keine Standardroute außerhalb der erkannten Tunnel")
    if not tunnels.findings:
        tunnels.add(SKIPPED, "Keine bekannten Tunneladapter gefunden",
                    "Policy-basiertes IPsec ohne eigenes Interface ist damit nicht ausgeschlossen.")
    else:
        for family, rows in tunnels.data.items():
            tunnels.add(PASS, f"IPv{family[1]}: {len(rows)} Tunnelroute(n) erfasst",
                        "\n".join(rows[:8]) + "\nTunnelverbindung wurde nicht unabhängig geprüft.",
                        f"ip -j {family} route show table all")
        rules = {}
        for family in ("-4", "-6"):
            rc, out = run(["ip", family, "rule", "show"], timeout=5)
            if rc == 0 and out.strip():
                rules[family] = out.splitlines()[:60]
        if rules:
            tunnels.data["policy_rules_all_devices"] = rules

    resolvers = [l.strip() for l in read("/etc/resolv.conf").splitlines() if l.startswith("nameserver ")]
    if resolvers:
        dns.data["resolv.conf"] = resolvers
        dns.add(PASS, "Konfigurierte Resolver", ", ".join(resolvers), "/etc/resolv.conf")
    if lan_only:
        pings.add(SKIPPED, "Internet-Ping nicht ausgeführt", "--lan-only")
        dns.add(SKIPPED, "Keine externen DNS-Namen abgefragt", "--lan-only")
        tcp.add(SKIPPED, "Keine externen TCP-Ziele geprüft", "--lan-only")
    else:
        _ping(pings, "1.1.1.1", "Internet IPv4 (1.1.1.1)")
        for name in ("github.com", "www.debian.org"):
            addresses = _dns(dns, name)
            _tcp(tcp, name, addresses)

    quality = Section("Linkqualität & WLAN")
    for r in main:
        if not _is_up(r):
            continue
        name = r["ifname"]
        if name.startswith("wl") or os.path.isdir(f"/sys/class/net/{name}/wireless"):
            rc, out = run(["iw", "dev", name, "link"], timeout=5)
            match = re.search(r"signal:\s*(-?\d+) dBm", out)
            if rc == 0 and match:
                dbm = int(match.group(1))
                freq = re.search(r"freq:\s*(\d+)", out)
                bitrate = re.findall(r"((?:rx|tx) bitrate:.*)", out)
                info_rc, info = run(["iw", "dev", name, "info"], timeout=5)
                channel = re.search(r"\bchannel (\d+)", info) if not info_rc else None
                measure = ([f"Kanal {channel[1]}"] if channel else []) + ([f"{freq[1]} MHz"] if freq else []) + bitrate
                quality.add(ATTENTION if dbm < -75 else PASS, f"{name}: WLAN-Signal {dbm} dBm",
                            " · ".join(measure), f"iw dev {name} link; iw dev {name} info")
        elif os.path.exists(f"/sys/class/net/{name}/device"):
            sp = read(f"/sys/class/net/{name}/speed")
            duplex = read(f"/sys/class/net/{name}/duplex")
            if sp.isdigit() and int(sp) > 0:
                quality.add(PASS, f"{name}: {sp} Mbit/s", duplex,
                            f"/sys/class/net/{name}/{{speed,duplex}}")
        errors = {k: read(f"/sys/class/net/{name}/statistics/{k}") for k in
                  ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")}
        if any(v.isdigit() for v in errors.values()):
            quality.data[name] = {k: int(v) for k, v in errors.items() if v.isdigit()}
    if not quality.findings:
        quality.add(UNSUPPORTED, "Keine Link- oder WLAN-Messwerte verfügbar")

    logs = Section("Netzwerk-Ereignisprotokoll")
    rows, error = events.query("--system", "-b", "0", "-u", "NetworkManager.service",
                               "-u", "systemd-networkd.service", "-u", "systemd-resolved.service", limit=150)
    issues = [r for r in rows if LINK_PROBLEM.search(r["MESSAGE"])]
    if error or not rows:
        logs.add(UNSUPPORTED, "Keine zugänglichen Netzwerkdienst-Ereignisse",
                 error, "journalctl -u NetworkManager -u systemd-networkd -u systemd-resolved")
    elif not issues:
        logs.add(PASS, f"Keine bekannten Link-/DNS-Muster in {len(rows)} Dienstmeldungen")
    else:
        for r in issues[-8:]:
            message = r["MESSAGE"][:350]
            if any(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", message) for name in vpn):
                tunnels.add(ATTENTION, message, source=events.source(r))
            else:
                logs.add(ATTENTION, message, source=events.source(r))
        if not logs.findings:
            logs.add(PASS, "Keine dieser Meldungen betraf einen Nicht-VPN-Adapter")
    if speedtest and not lan_only:
        speed = _speedtest()
    else:
        speed = Section("Übersprungene Tests")
        speed.add(SKIPPED, "Internet-Speedtest", "Nur auf Wunsch mit --speedtest (nicht mit --lan-only).")
    main_sections = [adapters, pings, dns]
    if speedtest and not lan_only:
        main_sections.append(speed)
    main_sections.extend([routing, tcp, quality, logs, tunnels])
    if not speedtest or lan_only:
        main_sections.append(speed)
    return main_sections
