#!/usr/bin/env bash
# PC-Diagnose Linux bootstrap:
#   curl -fsSL https://raw.githubusercontent.com/F1R3Burnout/pcdiag-linux/main/install.sh | bash -s -- [pcdiag args]
set -euo pipefail

REPO="F1R3Burnout/pcdiag-linux"
BRANCH="${PCDIAG_BRANCH:-main}"
DEST="${XDG_CACHE_HOME:-$HOME/.cache}/pcdiag/src"

command -v python3 >/dev/null || { echo "python3 wird benötigt (Arch/CachyOS: sudo pacman -S python)" >&2; exit 1; }

echo "[pcdiag] Lade $REPO ($BRANCH) ..."
rm -rf "$DEST"
mkdir -p "$DEST"
URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
# Timeouts + IPv4 fallback: a stalled connection must fail loudly instead of hanging silently.
if command -v curl >/dev/null; then
  curl -fL --progress-bar --connect-timeout 10 --max-time 120 --retry 2 "$URL" -o "$DEST/src.tar.gz" \
    || curl -fL --progress-bar -4 --connect-timeout 10 --max-time 120 "$URL" -o "$DEST/src.tar.gz"
elif command -v wget >/dev/null; then
  wget -q --show-progress -T 20 -O "$DEST/src.tar.gz" "$URL"
else
  echo "curl oder wget wird benötigt" >&2; exit 1
fi
tar -xzf "$DEST/src.tar.gz" -C "$DEST" --strip-components=1
rm -f "$DEST/src.tar.gz"
echo "[pcdiag] Download fertig."

cd "$DEST"
# When piped from curl, stdin is the script pipe: attach the menu to the terminal instead.
if [ ! -t 0 ] && (: </dev/tty) 2>/dev/null; then exec </dev/tty; fi

if [ "$(id -u)" -ne 0 ] && [ -z "${PCDIAG_NO_SUDO:-}" ] && command -v sudo >/dev/null; then
  echo "[pcdiag] Starte mit sudo für vollen Zugriff (SMART, Kernel-Log, Roh-Lesetest)."
  echo "[pcdiag] Gleich kommt die Passwortabfrage. Ohne root: PCDIAG_NO_SUDO=1 setzen."
  exec sudo -E python3 -m pcdiag "$@"
fi
exec python3 -m pcdiag "$@"
