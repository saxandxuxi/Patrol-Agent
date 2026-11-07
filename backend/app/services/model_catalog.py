from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from ..config import settings
from .logging_service import log_error, log_event

_CACHE_PATH: Path = settings.data_dir / "model_catalog.json"
_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
_LOCK = threading.Lock()
_REFRESH_THREAD: threading.Thread | None = None
_LAST_ERROR = ""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _CACHE_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(_CACHE_PATH)


def _age_seconds(data: dict[str, Any]) -> float | None:
    try:
        updated = datetime.fromisoformat(str(data.get("updated_at") or ""))
    except ValueError:
        return None
    return max(0.0, time.time() - updated.timestamp())


def _credential_fingerprint() -> str:
    values = [
        f"{provider}:{config.get('api_key') or ''}"
        for provider, config in sorted(settings.provider_configs().items())
    ]
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _dashscope_catalog_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}/api/v1/models"


def _dashscope_candidates(config: dict[str, Any]) -> list[dict[str, Any]]:
    base_url = str(config.get("base_url") or "")
    api_key = str(config.get("api_key") or "")
    if not api_key:
        return []
    url = _dashscope_catalog_url(base_url)
    candidates: list[dict[str, Any]] = []
    page = 1
    total = 0
    while True:
        response = requests.get(
            url,
            headers=_headers(api_key),
            params={"page_no": page, "page_size": 100},
            timeout=30,
        )
        response.raise_for_status()
        output = (response.json() or {}).get("output") or {}
        total = int(output.get("total") or total or 0)
        batch = list(output.get("models") or [])
        if not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            capabilities = [str(value) for value in item.get("capabilities") or []]
            metadata = item.get("inference_metadata") or {}
            response_modality = [
                str(value) for value in metadata.get("response_modality") or []
            ]
            if "TG" not in capabilities or "Text" not in response_modality:
                continue
            model_id = str(item.get("model") or "").strip()
            if not model_id:
                continue
            candidates.append(
                {
                    "id": model_id,
                    "name": str(item.get("name") or model_id),
                    "gateway_provider": "dashscope",
                    "vendor": str(item.get("provider") or ""),
                    "role_choice": f"dashscope/{model_id}",
                    "capabilities": capabilities,
                    "features": [
                        str(value) for value in item.get("features") or []
                    ],
                    "request_modalities": [
                        str(value)
                        for value in metadata.get("request_modality") or []
                    ],
                    "response_modalities": response_modality,
                    "context_window": int(
                        (item.get("model_info") or {}).get("context_window") or 0
                    ),
                    "published_time": str(item.get("published_time") or ""),
                }
            )
        if len(batch) < 100 or (total and page * 100 >= total):
            break
        page += 1
    return candidates


def _openai_compatible_candidates(
    provider: str, config: dict[str, Any]
) -> list[dict[str, Any]]:
    base_url = str(config.get("base_url") or "").rstrip("/")
    api_key = str(config.get("api_key") or "")
    if not base_url or not api_key:
        return []
    response = requests.get(
        f"{base_url}/models",
        headers=_headers(api_key),
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json() or {}
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []
    candidates: list[dict[str, Any]] = []
    for item in rows:
        model_id = str((item or {}).get("id") or "").strip()
        if not model_id:
            continue
        candidates.append(
            {
                "id": model_id,
                "name": model_id,
                "gateway_provider": provider,
                "vendor": provider,
                "role_choice": f"{provider}/{model_id}",
                "capabilities": ["TG"],
                "features": [],
                "request_modalities": ["Text"],
                "response_modalities": ["Text"],
                "context_window": 0,
                "published_time": "",
            }
        )
    return candidates


def _probe_text_model(candidate: dict[str, Any], config: dict[str, Any]) -> bool:
    base_url = str(config.get("base_url") or "").rstrip("/")
    api_key = str(config.get("api_key") or "")
    if not base_url or not api_key:
        return False
    payload = {
        "model": candidate["id"],
        "messages": [{"role": "user", "content": "只回复 OK"}],
        "max_tokens": 8,
        "temperature": 0,
        "enable_thinking": False,
    }
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=_headers(api_key),
            json=payload,
            timeout=20,
        )
        if response.status_code == 400 and "enable_thinking" in response.text:
            payload.pop("enable_thinking", None)
            response = requests.post(
                f"{base_url}/chat/completions",
                headers=_headers(api_key),
                json=payload,
                timeout=20,
            )
        if not response.ok:
            return False
        body = response.json() or {}
        return bool(body.get("choices"))
    except Exception:
        return False


def _refresh_catalog() -> None:
    global _LAST_ERROR
    started_at = _now()
    try:
        provider_configs = settings.provider_configs()
        candidates_by_key: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        catalog_total = 0
        for provider, config in provider_configs.items():
            if not str(config.get("api_key") or ""):
                continue
            try:
                if provider == "dashscope":
                    candidates = _dashscope_candidates(config)
                    catalog_total += len(candidates)
                else:
                    candidates = _openai_compatible_candidates(provider, config)
                    catalog_total += len(candidates)
            except Exception as exc:  # noqa: BLE001
                log_error("model_catalog", f"{provider}_list_failed", exc)
                candidates = [
                    {
                        "id": str(model),
                        "name": str(model),
                        "gateway_provider": provider,
                        "vendor": provider,
                        "role_choice": f"{provider}/{model}",
                        "capabilities": ["TG"],
                        "features": [],
                        "request_modalities": ["Text"],
                        "response_modalities": ["Text"],
                        "context_window": 0,
                        "published_time": "",
                    }
                    for model in config.get("models") or []
                ]
            for candidate in candidates:
                candidates_by_key[(provider, candidate["id"])] = (candidate, config)

        verified: list[dict[str, Any]] = []
        items = list(candidates_by_key.values())
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_probe_text_model, candidate, config): candidate
                for candidate, config in items
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    ok = bool(future.result())
                except Exception:
                    ok = False
                if ok:
                    verified.append({**candidate, "verified_at": started_at})

        verified.sort(
            key=lambda item: (
                str(item.get("gateway_provider") or ""),
                str(item.get("vendor") or ""),
                str(item.get("name") or "").lower(),
            )
        )
        data = {
            "updated_at": _now(),
            "source": "provider_model_catalog",
            "credential_fingerprint": _credential_fingerprint(),
            "catalog_total": catalog_total,
            "candidate_count": len(items),
            "verified_count": len(verified),
            "models": verified,
            "last_error": "",
        }
        _write_cache(data)
        _LAST_ERROR = ""
        log_event(
            "model_catalog",
            "INFO",
            "refresh_done",
            catalog_total=catalog_total,
            candidates=len(items),
            verified=len(verified),
        )
    except Exception as exc:  # noqa: BLE001
        _LAST_ERROR = str(exc)[:300]
        log_error("model_catalog", "refresh_failed", exc)


def _refresh_running() -> bool:
    return bool(_REFRESH_THREAD and _REFRESH_THREAD.is_alive())


def trigger_refresh(force: bool = False) -> bool:
    global _REFRESH_THREAD
    with _LOCK:
        if _refresh_running():
            return False
        cache = _read_cache()
        age = _age_seconds(cache)
        same_credentials = (
            str(cache.get("credential_fingerprint") or "")
            == _credential_fingerprint()
        )
        if (
            not force
            and age is not None
            and age < _CACHE_TTL_SECONDS
            and same_credentials
        ):
            return False
        _REFRESH_THREAD = threading.Thread(
            target=_refresh_catalog,
            name="model-catalog-refresh",
            daemon=True,
        )
        _REFRESH_THREAD.start()
        return True


def snapshot(auto_refresh: bool = True) -> dict[str, Any]:
    cache = _read_cache()
    age = _age_seconds(cache)
    stale = (
        age is None
        or age >= _CACHE_TTL_SECONDS
        or str(cache.get("credential_fingerprint") or "")
        != _credential_fingerprint()
    )
    started = False
    if auto_refresh and stale:
        started = trigger_refresh()
    return {
        **cache,
        "models": list(cache.get("models") or []),
        "stale": stale,
        "refreshing": _refresh_running(),
        "refresh_started": started,
        "last_error": _LAST_ERROR or str(cache.get("last_error") or ""),
        "ttl_days": 30,
    }


def role_choices() -> list[str]:
    cache = _read_cache()
    age = _age_seconds(cache)
    if (
        age is None
        or age >= _CACHE_TTL_SECONDS
        or str(cache.get("credential_fingerprint") or "")
        != _credential_fingerprint()
    ):
        trigger_refresh()
    return [
        str(item.get("role_choice") or "")
        for item in cache.get("models") or []
        if item.get("role_choice")
    ]


def refresh_now() -> dict[str, Any]:
    global _REFRESH_THREAD
    with _LOCK:
        if _refresh_running():
            return snapshot(auto_refresh=False)
        _REFRESH_THREAD = threading.Thread(
            target=_refresh_catalog,
            name="model-catalog-refresh",
            daemon=True,
        )
        _REFRESH_THREAD.start()
    return snapshot(auto_refresh=False)
