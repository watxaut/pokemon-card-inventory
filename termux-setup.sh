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
    echo "Shared storage can be read by other apps, and your collection will live here." >&2
    exit 1
    ;;
esac

echo "==> 1/4 Updating Termux packages (if asked about config files, press Enter to keep the default)"
pkg update -y
pkg upgrade -y

echo "==> 2/4 Installing Python, Tesseract (text recognition), prebuilt Pillow and the Rust compiler"
pkg install -y python python-pip python-pillow python-numpy tesseract rust llvm
tesseract --version | head -n 1


echo "==> 3/4 Installing the app's Python packages"
echo "    pydantic-core is compiled on the phone. This can take 5-15 minutes:"
echo "    keep Termux open and the phone plugged in."
# --system-site-packages lets the venv use Termux's prebuilt Pillow, numpy and pip.
python -m venv --system-site-packages --without-pip .venv
# shellcheck disable=SC1091
source .venv/bin/activate
ANDROID_API_LEVEL="$(getprop ro.build.version.sdk)"
export ANDROID_API_LEVEL
requirements="$(mktemp)"
grep -v -i '^numpy' requirements.txt > "${requirements}"  # numpy comes prebuilt from pkg (above)
if command -v termux-wake-lock > /dev/null; then termux-wake-lock || true; fi
python -m pip install -r "${requirements}"
if command -v termux-wake-unlock > /dev/null; then termux-wake-unlock || true; fi
rm -f "${requirements}"
python -c "import fastapi, PIL, pydantic_core, pytesseract, uvicorn; print('    All packages installed.')"
echo "    Downloading the Japanese model for reading card names (about 14 MB)"
python scripts/get_jpn_model.py || echo "    Skipped: card names won't be checked. Run 'python scripts/get_jpn_model.py' later."

echo "==> 4/4 Settings"
if [[ ! -f .env ]]; then
  cp .env.example .env
fi
chmod 600 .env

echo
echo "Setup complete. Start the app with:"
echo
echo "    cd \"${PWD}\" && source .venv/bin/activate && python run.py"
echo
echo "then open http://localhost:8000 in Chrome on this phone."
