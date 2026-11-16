#!/usr/bin/env python3
"""Start Card Binder and print the addresses to open on this computer and on your phone.

    python run.py                 # http://localhost:8000 + your Wi-Fi address
    python run.py --port 9000
    python run.py --ssl-certfile cert.pem --ssl-keyfile key.pem   # HTTPS (live camera on phones)
"""

from __future__ import annotations

import argparse
import socket

import uvicorn

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Pokémon card inventory app.")
    parser.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from your phone on the same Wi-Fi")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ssl-certfile", help="certificate for HTTPS (e.g. made with mkcert)")
    parser.add_argument("--ssl-keyfile", help="private key for HTTPS")
    parser.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    args = parser.parse_args()

    scheme = "https" if args.ssl_certfile else "http"
    print("\n  Card Binder")
    print(f"  This computer:  {scheme}://localhost:{args.port}")
    ip = lan_ip()
    if ip and args.host == "0.0.0.0":
        print(f"  Phone (same Wi-Fi): {scheme}://{ip}:{args.port}")
    print(f"  Data folder:    {settings.data_dir}")
    if not settings.claude_configured:
        print("\n  ! ANTHROPIC_API_KEY is not set: card recognition is disabled until you add it to .env")
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
