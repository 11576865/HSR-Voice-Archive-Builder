from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import os
import re
import secrets
import socket
import threading
import webbrowser


def full_server_available() -> bool:
    """Return whether the optional FastAPI server stack is importable."""
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("fastapi", "uvicorn", "pydantic")
    )


def _route_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect selects a route without sending application data.
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def local_ipv4_candidates() -> list[str]:
    values: list[str] = []
    routed = _route_local_ip()
    if routed and not routed.startswith("127."):
        values.append(routed)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip and not ip.startswith("127.") and ip not in values:
            values.append(ip)
    return values


def _is_loopback_host(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_port_available(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host and host != "0.0.0.0" else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        raise SystemExit(
            f"Port {port} is not available on {host}: {exc}. "
            "Close the other program or launch with --port <another-port>."
        ) from exc
    finally:
        probe.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Launch HSR Voice Archive Builder")
    p.add_argument("--lan", action="store_true", help="Allow control from another device on the same LAN")
    p.add_argument("--host")
    p.add_argument("--display-host", help="LAN address printed for phones/tablets; useful on multi-NIC/offline hosts")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--token", help="Explicit control token; generated automatically when omitted")
    p.add_argument("--lite", action="store_true", help="Use dependency-light stdlib server (Termux)")
    args = p.parse_args()

    if args.token:
        if len(args.token) < 16 or not re.fullmatch(r"[A-Za-z0-9_-]+", args.token):
            raise SystemExit(
                "--token must be at least 16 characters and contain only A-Z, a-z, 0-9, _ or -"
            )
        token = args.token
    else:
        token = secrets.token_urlsafe(24)
    os.environ["HSR_VOICE_TOKEN"] = token

    if args.lan:
        host = args.host or "0.0.0.0"
        candidates = local_ipv4_candidates()
        display_host = args.display_host or (candidates[0] if candidates else "")
        if not display_host:
            raise SystemExit(
                "Could not determine a LAN IPv4 address. "
                "Re-run with --display-host <this-device-LAN-IP>."
            )
        os.environ["HSR_VOICE_LAN_MODE"] = "1"
        os.environ["HSR_VOICE_ALLOWED_HOSTS"] = ",".join(
            {"127.0.0.1", "localhost", "::1", display_host, *candidates}
        )
        url = f"http://{display_host}:{args.port}/?token={token}"
        print("LAN control mode enabled.")
        print("Processing still runs on this machine.")
        print("The token gates both the dashboard and all control APIs.")
        if len(candidates) > 1:
            print("Detected LAN IPv4 addresses: " + ", ".join(candidates))
            print("If the printed URL uses the wrong adapter, restart with --display-host <IP>.")
        print(f"Open this URL from a device on the same LAN:\n{url}")
    else:
        host = args.host or "127.0.0.1"
        if not _is_loopback_host(host):
            raise SystemExit("Non-loopback hosts require --lan so token-gated LAN mode is enabled.")
        os.environ["HSR_VOICE_LAN_MODE"] = "0"
        os.environ["HSR_VOICE_ALLOWED_HOSTS"] = ",".join(
            {"127.0.0.1", "localhost", "::1", host}
        )
        url = f"http://127.0.0.1:{args.port}/"
        print(f"Local control URL: {url}")

    _check_port_available(host, args.port)

    if not args.no_browser and not args.lan:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    use_lite = args.lite or not full_server_available()
    if use_lite:
        if not args.lite:
            print("FastAPI/uvicorn is incomplete; using the built-in Termux lite server.")
        from .lite_server import serve
        serve(host, args.port)
    else:
        import uvicorn
        uvicorn.run("app.server:app", host=host, port=args.port, log_level="info")


_PROGRESS_CARD_RE = re.compile(
    r'<section\b[^>]*id="progressCard"[\s\S]*?</section>'
)
_MOBILE_SLOT_RE = re.compile(r'<div\s+id="mobileProgressSlot"[^>]*></div>')


def _extract_progress_card_markup(html: str) -> str:
    """Extract the desktop progress card plus the mobile progress slot markup."""
    card = _PROGRESS_CARD_RE.search(html)
    if not card:
        raise ValueError('index.html is missing the <section id="progressCard"> markup')
    slot = _MOBILE_SLOT_RE.search(html)
    if not slot:
        raise ValueError('index.html is missing the <div id="mobileProgressSlot"> markup')
    return card.group(0) + "\n" + slot.group(0)


def _build_progress_card_template() -> str:
    """Build a standalone progress card fragment with canonical element ids."""
    return (
        '<section id="progressCard" class="card" role="region" aria-label="处理进度">\n'
        '  <div class="topline"><h2>当前进度</h2><span id="progressTitle" class="muted"></span></div>\n'
        '  <div id="progressTrack" class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="progressBar" class="progress-bar"></div></div>\n'
        '  <p id="progressDesc" class="small muted"></p>\n'
        '  <div id="progressMeta" class="small muted"></div>\n'
        '  <pre id="progressLog" style="margin-top:8px"></pre>\n'
        '  <div id="mobileProgressSlot"></div>\n'
        '</section>'
    )


def _build_mobile_progress_bar_template() -> str:
    """Build a compact mobile progress bar fragment with canonical element ids."""
    return (
        '<div id="mobileProgressBar" class="mobile-progress-bar">\n'
        '  <div id="mobileProgressTrack" class="mobile-progress-track"><div class="mobile-progress-fill"></div></div>\n'
        '  <div id="mobileProgressTitle"></div>\n'
        '  <div id="mobileProgressDesc" class="small muted"></div>\n'
        '  <div id="mobileProgressMeta" class="small muted"></div>\n'
        '</div>'
    )


if __name__ == "__main__":
    main()
