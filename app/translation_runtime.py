from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .credentials import STATE_DIR

CAPABILITY_CACHE_FILE = STATE_DIR / "translation_capabilities.json"
CAPABILITY_TTL_SECONDS = 7 * 24 * 60 * 60

# Built-in list prices are intentionally restricted to the official OpenAI
# provider. Relays/custom endpoints must provide explicit environment overrides
# or run with token-only budgeting.
OPENAI_PRICE_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
}


class TranslationBudgetExceeded(RuntimeError):
    pass


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _nonnegative_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def parse_usage(payload: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return None
    raw = payload["usage"]
    input_tokens = _nonnegative_int(raw.get("input_tokens", raw.get("prompt_tokens")))
    output_tokens = _nonnegative_int(raw.get("output_tokens", raw.get("completion_tokens")))
    total_tokens = _nonnegative_int(raw.get("total_tokens"))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None

    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens

    input_details = raw.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = raw.get("prompt_tokens_details")
    output_details = raw.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = raw.get("completion_tokens_details")

    cached_tokens = _nonnegative_int(
        input_details.get("cached_tokens") if isinstance(input_details, dict) else 0
    ) or 0
    reasoning_tokens = _nonnegative_int(
        output_details.get("reasoning_tokens") if isinstance(output_details, dict) else 0
    ) or 0

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


def records_fingerprint(records: list[dict[str, str]]) -> str:
    canonical = [
        {
            "id": str(row.get("id", "")),
            "english": str(row.get("english", "")),
            "reference_text": str(row.get("reference_text", "")),
            "reference_language": str(row.get("reference_language", "")),
        }
        for row in records
    ]
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def estimate_request_tokens(
    records: list[dict[str, str]],
    glossary: dict[str, str] | None = None,
) -> dict[str, int]:
    # This is deliberately a heuristic rather than a tokenizer result. It gives
    # the preflight/budget layer a conservative planning number while actual API
    # usage remains authoritative after each response.
    payload_chars = len(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
    glossary_chars = sum(len(k) + len(v) + 6 for k, v in (glossary or {}).items())
    english_chars = sum(len(str(row.get("english", ""))) for row in records)
    input_tokens = max(1, math.ceil((payload_chars + glossary_chars + 2200) / 4))
    output_tokens = max(16 * len(records), math.ceil(max(1, english_chars) / 2.6))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def estimate_workload_tokens(
    records: list[dict[str, str]],
    batch_size: int,
) -> dict[str, int | bool]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    total_input = total_output = calls = 0
    for start in range(0, len(records), batch_size):
        estimate = estimate_request_tokens(records[start:start + batch_size])
        total_input += estimate["input_tokens"]
        total_output += estimate["output_tokens"]
        calls += 1
    return {
        "translation_estimated_input_tokens": total_input,
        "translation_estimated_output_tokens": total_output,
        "translation_estimated_total_tokens": total_input + total_output,
        "translation_estimated_api_calls": calls,
        "translation_estimate_excludes_repair_pass": True,
    }


def resolve_pricing(provider: str, model: str) -> dict[str, object] | None:
    env_input = os.environ.get("HSR_TRANSLATION_INPUT_USD_PER_MTOK", "").strip()
    env_output = os.environ.get("HSR_TRANSLATION_OUTPUT_USD_PER_MTOK", "").strip()
    if env_input or env_output:
        try:
            if not env_input or not env_output:
                raise ValueError
            input_rate = float(env_input)
            output_rate = float(env_output)
            if input_rate < 0 or output_rate < 0:
                raise ValueError
        except ValueError as exc:
            raise RuntimeError(
                "Set both HSR_TRANSLATION_INPUT_USD_PER_MTOK and "
                "HSR_TRANSLATION_OUTPUT_USD_PER_MTOK to non-negative numbers"
            ) from exc
        return {
            "input_usd_per_mtok": input_rate,
            "output_usd_per_mtok": output_rate,
            "source": "environment",
        }

    if provider == "openai" and model in OPENAI_PRICE_USD_PER_MTOK:
        input_rate, output_rate = OPENAI_PRICE_USD_PER_MTOK[model]
        return {
            "input_usd_per_mtok": input_rate,
            "output_usd_per_mtok": output_rate,
            "source": "openai-model-catalog-2026-09-20",
        }
    return None


def estimate_cost_usd(
    usage: dict[str, int],
    pricing: dict[str, object] | None,
) -> float | None:
    if pricing is None:
        return None
    return (
        usage.get("input_tokens", 0) * float(pricing["input_usd_per_mtok"])
        + usage.get("output_tokens", 0) * float(pricing["output_usd_per_mtok"])
    ) / 1_000_000.0


class TranslationUsageLedger:
    def __init__(
        self,
        path: Path,
        *,
        identity: dict[str, str],
        estimate: dict[str, object],
        token_budget: int = 0,
        usd_budget: float = 0.0,
    ) -> None:
        self.path = path
        self.identity = dict(identity)
        self.token_budget = max(0, int(token_budget))
        self.usd_budget = max(0.0, float(usd_budget))
        self.pricing = resolve_pricing(
            self.identity.get("provider", ""),
            self.identity.get("model", ""),
        )
        self.payload = self._load_or_create()
        self.payload["estimate"] = dict(estimate)
        self.payload["budget"] = {
            "token_budget": self.token_budget,
            "usd_budget": self.usd_budget,
        }
        self.payload["pricing"] = self.pricing
        self._recompute()
        self._save()

    @property
    def budget_active(self) -> bool:
        return self.token_budget > 0 or self.usd_budget > 0

    def _load_or_create(self) -> dict[str, Any]:
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            existing = {}
        if (
            isinstance(existing, dict)
            and existing.get("schema_version") == 1
            and existing.get("identity") == self.identity
            and isinstance(existing.get("calls"), list)
        ):
            return existing
        return {
            "schema_version": 1,
            "identity": self.identity,
            "estimate": {},
            "budget": {},
            "pricing": self.pricing,
            "calls": [],
            "usage_missing": False,
            "actual": {},
        }

    def _recompute(self) -> None:
        input_tokens = output_tokens = total_tokens = cached_tokens = reasoning_tokens = 0
        usage_missing = False
        for call in self.payload.get("calls", []):
            usage = call.get("usage") if isinstance(call, dict) else None
            if not isinstance(usage, dict):
                usage_missing = True
                continue
            input_tokens += int(usage.get("input_tokens", 0))
            output_tokens += int(usage.get("output_tokens", 0))
            total_tokens += int(usage.get("total_tokens", 0))
            cached_tokens += int(usage.get("cached_input_tokens", 0))
            reasoning_tokens += int(usage.get("reasoning_tokens", 0))
        actual_usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_input_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
        }
        self.payload["actual"] = actual_usage
        self.payload["usage_missing"] = usage_missing
        self.payload["estimated_cost_usd"] = estimate_cost_usd(actual_usage, self.pricing)

    def _save(self) -> None:
        self.payload["updated_at_unix"] = time.time()
        _atomic_write_json(self.path, self.payload)

    def check_before_request(
        self,
        estimate: dict[str, int],
        *,
        phase: str,
    ) -> None:
        self._recompute()
        if self.payload.get("usage_missing") and self.budget_active:
            raise TranslationBudgetExceeded(
                "Translation API stopped because a previous response omitted usage data; "
                "the configured budget can no longer be enforced safely."
            )

        actual = self.payload["actual"]
        projected = {
            "input_tokens": int(actual.get("input_tokens", 0)) + int(estimate.get("input_tokens", 0)),
            "output_tokens": int(actual.get("output_tokens", 0)) + int(estimate.get("output_tokens", 0)),
            "total_tokens": int(actual.get("total_tokens", 0)) + int(estimate.get("total_tokens", 0)),
        }
        if self.token_budget and projected["total_tokens"] > self.token_budget:
            raise TranslationBudgetExceeded(
                f"Translation token budget would be exceeded before {phase}: "
                f"projected {projected['total_tokens']} > limit {self.token_budget}"
            )
        if self.usd_budget:
            if self.pricing is None:
                raise TranslationBudgetExceeded(
                    "A USD translation budget is configured, but pricing is unknown for this "
                    "provider/model. Use a token budget or set "
                    "HSR_TRANSLATION_INPUT_USD_PER_MTOK and "
                    "HSR_TRANSLATION_OUTPUT_USD_PER_MTOK."
                )
            projected_cost = estimate_cost_usd(projected, self.pricing) or 0.0
            if projected_cost > self.usd_budget:
                raise TranslationBudgetExceeded(
                    f"Translation USD budget would be exceeded before {phase}: "
                    f"estimated USD {projected_cost:.6f} > limit USD {self.usd_budget:.6f}"
                )

    def record(self, phase: str, usage: dict[str, int] | None) -> None:
        self.payload.setdefault("calls", []).append({
            "phase": phase,
            "usage": usage,
            "recorded_at_unix": time.time(),
        })
        self._recompute()
        self._save()

    def assert_observable(self) -> None:
        if self.budget_active and self.payload.get("usage_missing"):
            raise TranslationBudgetExceeded(
                "Translation API response omitted token usage. The paid result was preserved, "
                "but the build is stopping before another request because the configured budget "
                "cannot be verified."
            )

    def report(self) -> dict[str, object]:
        self._recompute()
        actual = self.payload["actual"]
        return {
            **self.payload.get("estimate", {}),
            "translation_actual_input_tokens": actual["input_tokens"],
            "translation_actual_output_tokens": actual["output_tokens"],
            "translation_actual_total_tokens": actual["total_tokens"],
            "translation_cached_input_tokens": actual["cached_input_tokens"],
            "translation_reasoning_tokens": actual["reasoning_tokens"],
            "translation_api_call_count": len(self.payload.get("calls", [])),
            "translation_usage_missing": bool(self.payload.get("usage_missing")),
            "translation_estimated_cost_usd": self.payload.get("estimated_cost_usd"),
            "translation_pricing_source": (
                self.pricing.get("source") if isinstance(self.pricing, dict) else None
            ),
            "translation_token_budget": self.token_budget,
            "translation_budget_usd": self.usd_budget,
        }


def _capability_key(identity: dict[str, str]) -> str:
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_capability_cache() -> dict[str, Any]:
    try:
        payload = json.loads(CAPABILITY_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"schema_version": 1, "entries": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        return {"schema_version": 1, "entries": {}}
    return payload


def get_cached_capability(
    *,
    provider: str,
    base_url: str,
    model: str,
    schema_fingerprint: str,
    max_age_seconds: int = CAPABILITY_TTL_SECONDS,
    now: float | None = None,
) -> dict[str, object] | None:
    identity = {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "schema_fingerprint": schema_fingerprint,
    }
    cache = _read_capability_cache()
    row = cache["entries"].get(_capability_key(identity))
    if not isinstance(row, dict) or row.get("identity") != identity or not row.get("ok"):
        return None
    checked_at = float(row.get("checked_at_unix", 0.0))
    current = time.time() if now is None else float(now)
    if current - checked_at > max(0, int(max_age_seconds)):
        return None
    return dict(row)


def save_capability(
    *,
    provider: str,
    base_url: str,
    model: str,
    schema_fingerprint: str,
    usage_supported: bool,
    now: float | None = None,
) -> None:
    identity = {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "schema_fingerprint": schema_fingerprint,
    }
    cache = _read_capability_cache()
    cache["schema_version"] = 1
    cache.setdefault("entries", {})[_capability_key(identity)] = {
        "identity": identity,
        "ok": True,
        "usage_supported": bool(usage_supported),
        "checked_at_unix": time.time() if now is None else float(now),
    }
    _atomic_write_json(CAPABILITY_CACHE_FILE, cache)


def invalidate_capability(
    *,
    provider: str,
    base_url: str,
    model: str,
    schema_fingerprint: str,
) -> None:
    identity = {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "schema_fingerprint": schema_fingerprint,
    }
    cache = _read_capability_cache()
    cache.setdefault("entries", {}).pop(_capability_key(identity), None)
    _atomic_write_json(CAPABILITY_CACHE_FILE, cache)
