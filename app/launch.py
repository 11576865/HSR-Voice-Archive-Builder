from __future__ import annotations

import argparse
import os
import secrets
import socket
import threading
import webbrowser


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Launch HSR Voice Archive Builder")
    p.add_argument("--lan", action="store_true", help="Allow control from another device on the same LAN")
    p.add_argument("--host")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--token", help="Explicit LAN API token; generated automatically when omitted")
    args = p.parse_args()

    if args.lan:
        host = args.host or "0.0.0.0"
        token = args.token or secrets.token_urlsafe(18)
        os.environ["HSR_VOICE_TOKEN"] = token
        display_host = local_ip()
        url = f"http://{display_host}:{args.port}/?token={token}"
        print("LAN control mode enabled.")
        print("Processing still runs on this machine.")
        print(f"Open this URL from a device on the same LAN:\n{url}")
    else:
        host = args.host or "127.0.0.1"
        os.environ.pop("HSR_VOICE_TOKEN", None)
        url = f"http://127.0.0.1:{args.port}/"
        print(f"Local control URL: {url}")

    if not args.no_browser and not args.lan:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run("app.server:app", host=host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
