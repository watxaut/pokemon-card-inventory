#!/usr/bin/env python3
"""Start Card Binder and print the addresses to open on this computer and on your phone.

    uv run run.py                 # http://localhost:8000 + your Wi-Fi address
    uv run run.py --port 9000
    uv run run.py --ssl-certfile cert.pem --ssl-keyfile key.pem   # HTTPS (live camera on phones)
"""

from __future__ import annotations

import argparse
import os
import socket

import uvicorn

from app import reader
from app.config import settings


def lan_ip() -> str | None:
    """The address other devices on your Wi-Fi can reach this computer at."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outgoing interface
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def on_android() -> bool:
    """True inside Termux on an Android phone."""
    return bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in os.environ.get("PREFIX", "")


def main() -> None:
    android = on_android()
    parser = argparse.ArgumentParser(description="Run the Pokémon card inventory app.")
    parser.add_argument(
        "--host",
        # On the phone itself, only the phone can connect unless you ask otherwise.
        default="127.0.0.1" if android else "0.0.0.0",
        help="127.0.0.1 = this device only; 0.0.0.0 = also reachable from other devices on the same Wi-Fi",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ssl-certfile", help="certificate for HTTPS (e.g. made with mkcert)")
    parser.add_argument("--ssl-keyfile", help="private key for HTTPS")
    parser.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    args = parser.parse_args()

    scheme = "https" if args.ssl_certfile else "http"
    print("\n  Card Binder")
    print(f"  This device:    {scheme}://localhost:{args.port}")
    ip = lan_ip()
    if ip and args.host == "0.0.0.0":
        print(f"  Other devices (same Wi-Fi): {scheme}://{ip}:{args.port}")
        if android and not settings.app_password:
            print("  ! Anyone on this Wi-Fi can open the app. Set APP_PASSWORD in .env, or drop --host 0.0.0.0.")
    elif android:
        print("  Only this phone can open it (use --host 0.0.0.0 to share on your Wi-Fi).")
    print(f"  Data folder:    {settings.data_dir}")
    if not reader.tesseract_available():
        print("\n  ! Tesseract is not installed: photos can't be read until you install it (see README).")
        print("    You can still type card numbers in.")
    elif not reader.title_available():
        print("  Card names are not checked: run 'uv run scripts/get_jpn_model.py' to enable it.")
    if settings.app_password:
        print("  Password protection is ON (APP_PASSWORD).")
    print()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        log_level="info",
    )


if __name__ == "__main__":
    main()
