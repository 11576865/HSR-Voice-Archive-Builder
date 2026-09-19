from __future__ import annotations

import os
import secrets

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_FALLBACK_TOKEN = secrets.token_urlsafe(24)


def api_token() -> str:
    return os.environ.get("HSR_VOICE_TOKEN", "") or _FALLBACK_TOKEN


def lan_mode() -> bool:
    return os.environ.get("HSR_VOICE_LAN_MODE", "") == "1"


def allowed_hosts() -> set[str]:
    hosts = set(LOOPBACK_HOSTS)
    extra = os.environ.get("HSR_VOICE_ALLOWED_HOSTS", "")
    hosts.update(x.strip().lower() for x in extra.split(",") if x.strip())
    return hosts


def host_allowed(hostname: str | None) -> bool:
    if not hostname:
        return False
    return hostname.strip("[]").lower() in allowed_hosts()


def token_matches(supplied: str | None) -> bool:
    expected = api_token()
    if not expected or not supplied:
        return False
    return secrets.compare_digest(expected, supplied)
