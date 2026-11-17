#!/data/data/com.termux/files/usr/bin/bash
# One-time setup for running Card Binder on an Android phone with Termux.
#
#   cd ~/pokemon-card-inventory
#   bash termux-setup.sh
#
# Then start the app any time with:
#   cd ~/pokemon-card-inventory && source .venv/bin/activate && python run.py
set -euo pipefail

if [[ -z "${TERMUX_VERSION:-}" && "${PREFIX:-}" != *com.termux* ]]; then
  echo "This script is meant for Termux on Android." >&2
  exit 1
fi

cd "$(dirname "$(readlink -f "$0")")"
case "$PWD" in
  /sdcard/* | /storage/* | /mnt/*)
    echo "Move this folder into your Termux home (~) first and run the script again." >&2
    echo "Shared storage can be read by other apps, and your API key and collection will live here." >&2
    exit 1
    ;;
esac

echo "==> 1/4 Updating Termux packages (if asked about config files, press Enter to keep the default)"
pkg update -y
pkg upgrade -y

echo "==> 2/4 Installing Python, prebuilt Pillow and the Rust compiler"
pkg install -y python python-pip python-pillow rust llvm

echo "==> 3/4 Installing the app's Python packages"
echo "    pydantic-core and jiter are compiled on the phone. This can take 10-30 minutes:"
echo "    keep Termux open and the phone plugged in."
# --system-site-packages lets the venv use Termux's prebuilt Pillow and pip.
python -m venv --system-site-packages --without-pip .venv
# shellcheck disable=SC1091
source .venv/bin/activate
ANDROID_API_LEVEL="$(getprop ro.build.version.sdk)"
export ANDROID_API_LEVEL
if command -v termux-wake-lock > /dev/null; then termux-wake-lock || true; fi
python -m pip install -r requirements.txt
if command -v termux-wake-unlock > /dev/null; then termux-wake-unlock || true; fi
python -c "import anthropic, fastapi, PIL, pydantic_core, uvicorn; print('    All packages installed.')"

echo "==> 4/4 Settings"
if [[ ! -f .env ]]; then
  cp .env.example .env
fi
chmod 600 .env
if grep -q '^ANTHROPIC_API_KEY=sk-ant-\.\.\.$' .env; then
  read -rsp "    Paste your Claude API key (hidden; press Enter to skip): " key
  echo
  if [[ -n "${key}" ]]; then
    KEY="${key}" python - << 'PY'
import os, pathlib, re
env = pathlib.Path(".env")
env.write_text(re.sub(r"(?m)^ANTHROPIC_API_KEY=.*$", lambda _: "ANTHROPIC_API_KEY=" + os.environ["KEY"].strip(),
                      env.read_text(encoding="utf-8")), encoding="utf-8")
PY
    echo "    Saved to .env (readable only by Termux)."
  else
    echo "    Skipped. Edit .env later with: nano .env"
  fi
fi

echo
echo "Setup complete. Start the app with:"
echo
echo "    cd \"${PWD}\" && source .venv/bin/activate && python run.py"
echo
echo "then open http://localhost:8000 in Chrome on this phone."
