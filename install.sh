#!/usr/bin/env bash
# PC-Diagnose Linux bootstrap:  curl -fsSL https://raw.githubusercontent.com/F1R3Burnout/pcdiag-linux/main/install.sh | bash -s -- [pcdiag args]
set -euo pipefail

REPO="F1R3Burnout/pcdiag-linux"
BRANCH="${PCDIAG_BRANCH:-main}"
DEST="${XDG_CACHE_HOME:-$HOME/.cache}/pcdiag"

command -v python3 >/dev/null || { echo "python3 wird benötigt (Arch/CachyOS: sudo pacman -S python)" >&2; exit 1; }
command -v curl >/dev/null || command -v wget >/dev/null || { echo "curl oder wget wird benötigt" >&2; exit 1; }

rm -rf "$DEST"
mkdir -p "$DEST"
URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
if command -v curl >/dev/null; then curl -fsSL "$URL" | tar -xz -C "$DEST" --strip-components=1
else wget -qO- "$URL" | tar -xz -C "$DEST" --strip-components=1; fi

cd "$DEST"
# When piped from curl, stdin is the script pipe: attach the menu to the terminal instead.
if [ ! -t 0 ] && [ -r /dev/tty ]; then exec </dev/tty; fi

if [ "$(id -u)" -ne 0 ] && [ -z "${PCDIAG_NO_SUDO:-}" ] && command -v sudo >/dev/null; then
  echo "Starte mit sudo für vollen Zugriff (SMART, dmesg, Roh-Lesetest). Ohne root: PCDIAG_NO_SUDO=1"
  exec sudo -E python3 -m pcdiag "$@"
fi
exec python3 -m pcdiag "$@"
