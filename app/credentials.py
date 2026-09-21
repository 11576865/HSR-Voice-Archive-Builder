from __future__ import annotations

import argparse
import getpass
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

STATE_DIR = Path.home() / ".hsr-voice-archive-builder"
CREDENTIALS_FILE = STATE_DIR / "translation_credentials.json"

PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "vapi": "https://api.gpt.ge/v1",
}
FALLBACK_TRANSLATION_MODEL = "gpt-5.6-terra"


def normalize_base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("Translation API Base URL is required")
    parsed = urlsplit(raw)
    if parsed.username or parsed.password:
        raise ValueError("Translation API Base URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Translation API Base URL must not contain query or fragment components")
    if not parsed.hostname:
        raise ValueError("Translation API Base URL must include a hostname")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (scheme == "http" and loopback):
        raise ValueError("Translation API Base URL must use HTTPS, except loopback HTTP endpoints")
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class TranslationCredentials:
    provider: str
    base_url: str
    api_key: str
    default_model: str
    source: str

    @property
    def responses_url(self) -> str:
        return self.base_url.rstrip("/") + "/responses"


def _read_file() -> dict[str, str]:
    if not CREDENTIALS_FILE.is_file():
        return {}
    try:
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v is not None}


def _default_base_url(provider: str) -> str:
    return PROVIDER_BASE_URLS.get(provider, "")


def translation_default_model() -> str:
    """Return the local default model without coupling it to a provider preset."""
    saved = _read_file()
    return (
        os.environ.get("HSR_TRANSLATION_MODEL", "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or saved.get("default_model", "").strip()
        or FALLBACK_TRANSLATION_MODEL
    )


def translation_identity() -> tuple[str, str]:
    saved = _read_file()
    provider = (
        os.environ.get("HSR_TRANSLATION_PROVIDER", "").strip()
        or saved.get("provider", "").strip()
        or "openai"
    )
    env_base = os.environ.get("HSR_TRANSLATION_BASE_URL", "").strip()
    saved_base = ""
    if saved.get("provider", "").strip() == provider:
        saved_base = saved.get("base_url", "").strip()
    base_url = env_base or saved_base or _default_base_url(provider)
    if not base_url:
        raise RuntimeError(
            "No translation API Base URL is configured. "
            "Run: python -m app.credentials configure --provider custom"
        )
    return provider, normalize_base_url(base_url)


def load_translation_credentials() -> TranslationCredentials:
    saved = _read_file()
    provider, base_url = translation_identity()

    env_key = os.environ.get("HSR_TRANSLATION_API_KEY", "").strip()
    legacy_key = os.environ.get("OPENAI_API_KEY", "").strip() if provider == "openai" else ""
    saved_key = ""
    if (
        saved.get("provider", "").strip() == provider
        and saved.get("base_url", "").strip()
        and normalize_base_url(saved["base_url"]) == base_url
    ):
        saved_key = saved.get("api_key", "").strip()

    if env_key:
        key, source = env_key, "HSR_TRANSLATION_API_KEY"
    elif legacy_key:
        key, source = legacy_key, "OPENAI_API_KEY"
    elif saved_key:
        key, source = saved_key, str(CREDENTIALS_FILE)
    else:
        key, source = "", "not-configured"

    return TranslationCredentials(
        provider=provider,
        base_url=base_url,
        api_key=key,
        default_model=translation_default_model(),
        source=source,
    )


def credentials_status() -> dict[str, object]:
    creds = load_translation_credentials()
    return {
        "provider": creds.provider,
        "base_url": creds.base_url,
        "configured": bool(creds.api_key),
        "source": creds.source if creds.api_key else "not-configured",
        "default_model": creds.default_model,
    }


def save_translation_credentials(
    provider: str, base_url: str, api_key: str, default_model: str | None = None
) -> Path:
    provider = str(provider or "").strip().lower()
    if not provider:
        raise ValueError("Provider is required")
    if provider in PROVIDER_BASE_URLS and not str(base_url or "").strip():
        base_url = PROVIDER_BASE_URLS[provider]
    base_url = normalize_base_url(base_url)
    api_key = str(api_key or "").strip()
    if not api_key:
        raise ValueError("API key is required")
    model = str(default_model or "").strip() or translation_default_model()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass

    payload = {
        "schema_version": 2,
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "default_model": model,
    }
    fd, temp_name = tempfile.mkstemp(prefix=".translation_credentials.", suffix=".tmp", dir=STATE_DIR)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, CREDENTIALS_FILE)
        try:
            CREDENTIALS_FILE.chmod(0o600)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)
    return CREDENTIALS_FILE


def clear_translation_credentials() -> None:
    CREDENTIALS_FILE.unlink(missing_ok=True)


def _configure(args: argparse.Namespace) -> None:
    provider = args.provider
    base_url = args.base_url or _default_base_url(provider)
    if not base_url:
        base_url = input("Base URL: ").strip()
    api_key = getpass.getpass("API key (input hidden): ").strip()
    path = save_translation_credentials(provider, base_url, api_key, args.model)
    status = credentials_status()
    print(f"Saved translation credentials to {path}")
    print(f"Provider: {status['provider']}")
    print(f"Base URL: {status['base_url']}")
    print(f"Default model: {status['default_model']}")
    print("API key: configured (hidden)")


def _status() -> None:
    status = credentials_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))


def _test(args: argparse.Namespace) -> None:
    from .translator import ensure_translation_capability

    status = credentials_status()
    if not status["configured"]:
        raise SystemExit(
            "Translation API key is not configured. "
            "Run: python -m app.credentials configure --provider custom --base-url <OpenAI-compatible-Base-URL>"
        )
    result = ensure_translation_capability(
        model=args.model,
        force=args.force,
    )
    print(json.dumps(
        {
            "ok": True,
            "provider": status["provider"],
            "base_url": status["base_url"],
            "model": args.model,
            "capability": result,
        },
        ensure_ascii=False,
        indent=2,
    ))


def _set_model(args: argparse.Namespace) -> None:
    saved = _read_file()
    api_key = saved.get("api_key", "").strip()
    provider = saved.get("provider", "").strip()
    base_url = saved.get("base_url", "").strip()
    if not (api_key and provider and base_url):
        raise SystemExit(
            "No locally saved translation credentials. Run: "
            "python -m app.credentials configure --provider custom --base-url https://example.com/v1"
        )
    path = save_translation_credentials(provider, base_url, api_key, args.model)
    print(f"Saved default translation model to {path}: {args.model}")


def main() -> None:
    p = argparse.ArgumentParser(description="Manage local translation API credentials")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("configure", help="Save a translation API key locally with hidden input")
    c.add_argument("--provider", choices=["openai", "vapi", "custom"], default="vapi")
    c.add_argument("--base-url", default="")
    c.add_argument("--model", default="", help="Default model for new projects")
    c.set_defaults(func=_configure)

    s = sub.add_parser("status", help="Show provider/Base URL without revealing the key")
    s.set_defaults(func=lambda args: _status())

    t = sub.add_parser("test", help="Verify structured translation capability; reuse a fresh cached success")
    t.add_argument("--model", default=translation_default_model())
    t.add_argument("--force", action="store_true", help="Ignore the capability cache and send a fresh smoke request")
    t.set_defaults(func=_test)

    x = sub.add_parser("clear", help="Delete the locally saved translation credentials")
    x.set_defaults(func=lambda args: (clear_translation_credentials(), print("Translation credentials cleared.")))

    m = sub.add_parser("model", help="Change the locally saved default model without re-entering the API key")
    m.add_argument("model")
    m.set_defaults(func=_set_model)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
