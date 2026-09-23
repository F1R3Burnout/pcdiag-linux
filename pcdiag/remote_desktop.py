"""xrdp + PolicyKit diagnostics.

xrdp sessions are not registered as an "active" seat by systemd-logind, so
PolicyKit's built-in NetworkManager policy - which silently allows the active
local user - falls back to requiring admin authentication for routine actions
(reconnecting a saved network, etc.), even for the machine's own owner. This
shows up as a repeating "Authentication Required" dialog inside the remote
session. The fix is a PolicyKit rule that explicitly trusts the desktop user
for NetworkManager actions regardless of session state.
"""
import getpass
import glob
import os
import subprocess
from typing import List, Optional

from .common import ATTENTION, PASS, UNSUPPORTED, Section, have, is_root, read, run

RULE_PATH = "/etc/polkit-1/rules.d/49-nopasswd-networkmanager.rules"


def xrdp_installed(present=have) -> bool:
    return present("xrdp-sesman") or present("xrdp")


def target_username() -> str:
    """The desktop user to grant the rule to - not 'root', even when this
    process itself runs under sudo (the normal way this tool is started)."""
    return os.environ.get("SUDO_USER") or getpass.getuser()


def polkit_rule_text(username: str) -> str:
    return (
        "polkit.addRule(function(action, subject) {\n"
        '    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 &&\n'
        f'        subject.user == "{username}") {{\n'
        "        return polkit.Result.YES;\n"
        "    }\n"
        "});\n"
    )


def rule_grants_networkmanager(text: str, username: str) -> bool:
    """Pure check: does this polkit rule file's content already grant
    passwordless NetworkManager access covering `username`?
    Heuristic, not a JS/pkla parser - good enough to avoid a duplicate rule."""
    if "NetworkManager" not in text:
        return False
    allows = ("polkit.Result.YES" in text or "ResultActive=yes" in text
             or "ResultAny=yes" in text or "ResultInactive=yes" in text)
    if not allows:
        return False
    if username in text or "wheel" in text or "netdev" in text or "subject.isInGroup" in text:
        return True
    # a blanket rule with no user/group qualifier at all still covers everyone
    return "subject.user ==" not in text and "Identity=" not in text


def has_nopasswd_networkmanager_rule(username: str, rule_files: Optional[List[str]] = None) -> bool:
    files = rule_files if rule_files is not None else (
        sorted(glob.glob("/etc/polkit-1/rules.d/*.rules"))
        + sorted(glob.glob("/etc/polkit-1/localauthority/50-local.d/*.pkla"))
    )
    return any(rule_grants_networkmanager(read(f), username) for f in files)


def check(username: Optional[str] = None) -> Section:
    s = Section("Remote Desktop & PolicyKit")
    if not xrdp_installed():
        s.add(UNSUPPORTED, "xrdp nicht installiert - Prüfung übersprungen")
        return s
    user = username or target_username()
    if has_nopasswd_networkmanager_rule(user):
        s.add(PASS, "PolicyKit-Regel für passwortlose NetworkManager-Aktionen gefunden")
        return s
    s.add(ATTENTION,
          "xrdp ist installiert, aber keine PolicyKit-Regel für NetworkManager gefunden",
          "xrdp-Sitzungen gelten bei systemd-logind nicht als 'aktiv': PolicyKit fragt deshalb bei "
          "jeder NetworkManager-Aktion (z. B. WLAN verbinden) erneut nach dem Passwort - auch für "
          f"den Besitzer des Rechners ({user}). Beheben: sudo python3 -m pcdiag sysdiag "
          f"--fix-xrdp-polkit (schreibt {RULE_PATH})")
    return s


def apply_fix(username: Optional[str] = None, assume_yes: bool = False, ask=input, log=print) -> bool:
    """Writes the PolicyKit rule for `username`. Needs root (directly or via sudo);
    asks for confirmation unless assume_yes. Returns True on success."""
    user = username or target_username()
    if has_nopasswd_networkmanager_rule(user):
        log("PolicyKit-Regel ist bereits vorhanden - nichts zu tun.")
        return True
    log(f"Das legt {RULE_PATH} an und erlaubt Benutzer '{user}' alle NetworkManager-Aktionen "
        "ohne Passwortabfrage (auch in xrdp-Sitzungen).")
    if not assume_yes:
        if ask("Jetzt anlegen? [J/n] ").strip().lower() not in ("", "j", "y", "ja", "yes"):
            return False
    content = polkit_rule_text(user)
    try:
        if is_root():
            with open(RULE_PATH, "w") as f:
                f.write(content)
            os.chmod(RULE_PATH, 0o644)
        elif have("sudo"):
            p = subprocess.run(["sudo", "tee", RULE_PATH], input=content, text=True,
                               capture_output=True, timeout=30)
            if p.returncode != 0:
                log(f"Schreiben fehlgeschlagen: {p.stderr.strip()[-300:]}")
                return False
        else:
            log("Weder root noch sudo verfügbar - Regel manuell anlegen.")
            return False
    except OSError as e:
        log(f"Schreiben fehlgeschlagen: {e}")
        return False
    rc, out = run((["sudo"] if not is_root() else []) + ["systemctl", "restart", "polkit"], timeout=30)
    if rc != 0:
        log(f"Regel geschrieben. Neustart von polkit fehlgeschlagen (meist unkritisch, wirkt beim "
            f"nächsten Login): {out.strip()[-200:]}")
    else:
        log("Regel geschrieben und polkit neu gestartet.")
    return True
