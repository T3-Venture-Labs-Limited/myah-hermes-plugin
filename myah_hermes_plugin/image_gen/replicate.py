"""Replicate image generation backend for Hermes Agent.

The hosted Myah path prefers a platform broker so the shared Replicate key stays
server-side. OSS/local/BYOK installs can call Replicate directly with
``REPLICATE_API_TOKEN``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "openai/gpt-image-2"
DIRECT_API_BASE = "https://api.replicate.com/v1"
DEFAULT_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 2.0

_MODELS: Dict[str, Dict[str, Any]] = {
    "openai/gpt-image-2": {
        "display": "GPT Image 2",
        "speed": "~15-90s",
        "strengths": "Commercial polish, text rendering, instruction following",
        "price": "Replicate billing",
        "aspect_ratios": {
            "landscape": "3:2",
            "square": "1:1",
            "portrait": "2:3",
        },
    },
    "google/nano-banana-2": {
        "display": "Nano Banana 2",
        "speed": "~10-60s",
        "strengths": "Reference-heavy edits, broad social ratios, image fusion",
        "price": "Replicate billing",
        "aspect_ratios": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
    },
}


class ReplicateHTTPError(RuntimeError):
    """Raised for bounded Replicate/Broker HTTP errors."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


def _redact(value: str, secret: str | None) -> str:
    if secret:
        return value.replace(secret, "[redacted]")
    return value


def _load_image_gen_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model() -> str:
    env_override = os.environ.get("REPLICATE_IMAGE_MODEL", "").strip()
    if env_override in _MODELS:
        return env_override

    cfg = _load_image_gen_config()
    replicate_cfg = cfg.get("replicate") if isinstance(cfg.get("replicate"), dict) else {}
    candidate = replicate_cfg.get("model") if isinstance(replicate_cfg, dict) else None
    if isinstance(candidate, str) and candidate in _MODELS:
        return candidate

    top = cfg.get("model")
    if isinstance(top, str) and top in _MODELS:
        return top

    return DEFAULT_MODEL


def _model_aspect_ratio(model_id: str, aspect_ratio: str) -> str:
    aspect = resolve_aspect_ratio(aspect_ratio)
    meta = _MODELS.get(model_id) or _MODELS[DEFAULT_MODEL]
    ratios = meta.get("aspect_ratios") or {}
    return str(ratios.get(aspect) or ratios.get(DEFAULT_ASPECT_RATIO) or "1:1")


def _json_request(method: str, url: str, payload: dict | None, headers: dict, timeout: float) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json", **headers}
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        message = raw[:500]
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                message = str(parsed.get("detail") or parsed.get("error") or parsed.get("message") or message)
        except Exception:
            pass
        raise ReplicateHTTPError(exc.code, message) from exc
    except urllib.error.URLError as exc:
        raise ReplicateHTTPError(0, str(exc)) from exc

    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ReplicateHTTPError(0, f"invalid JSON response: {exc}") from exc
    return data if isinstance(data, dict) else {"output": data}


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    return _json_request("POST", url, payload, headers, timeout)


def _get_json(url: str, headers: dict, timeout: float) -> dict:
    return _json_request("GET", url, None, headers, timeout)


def _extract_output_url(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    if isinstance(value, list):
        for item in value:
            found = _extract_output_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("url", "get", "image", "output"):
            found = _extract_output_url(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _extract_output_url(item)
            if found:
                return found
    return None


def _direct_prediction_url(model_id: str) -> str:
    owner, name = model_id.split("/", 1)
    return f"{DIRECT_API_BASE}/models/{owner}/{name}/predictions"


def _broker_url() -> str:
    explicit = os.environ.get("MYAH_REPLICATE_IMAGE_BROKER_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return ""


def _broker_bearer() -> str:
    return (
        os.environ.get("MYAH_PLATFORM_BEARER")
        or os.environ.get("MYAH_AGENT_BEARER_TOKEN")
        or os.environ.get("MYAH_AGENT_TOKEN")
        or ""
    ).strip()


class ReplicateImageGenProvider(ImageGenProvider):
    """Replicate-backed image generation provider."""

    @property
    def name(self) -> str:
        return "replicate"

    @property
    def display_name(self) -> str:
        return "Replicate"

    def is_available(self) -> bool:
        return bool(os.environ.get("REPLICATE_API_TOKEN") or _broker_url())

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Replicate",
            "badge": "paid",
            "tag": "Replicate-hosted image models; hosted Myah can use a brokered shared key",
            "env_vars": [
                {
                    "key": "REPLICATE_API_TOKEN",
                    "prompt": "Replicate API token",
                    "url": "https://replicate.com/account/api-tokens",
                }
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model_id = _resolve_model()

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=self.name,
                model=model_id,
                aspect_ratio=aspect,
            )

        broker_url = _broker_url()
        if broker_url:
            return self._generate_via_broker(broker_url, prompt, aspect, model_id)
        return self._generate_direct(prompt, aspect, model_id)

    def _generate_via_broker(self, broker_url: str, prompt: str, aspect: str, model_id: str) -> Dict[str, Any]:
        bearer = _broker_bearer()
        if not bearer:
            return error_response(
                error="Myah Replicate broker is configured but no Myah agent bearer token is available.",
                error_type="auth_required",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        payload = {
            "prompt": prompt,
            "aspect_ratio": aspect,
            "model": model_id,
            "user_id": os.environ.get("MYAH_USER_ID", ""),
        }
        headers = {"Authorization": f"Bearer {bearer}"}
        try:
            data = _post_json(broker_url, payload, headers, timeout=DEFAULT_TIMEOUT_SECONDS)
        except ReplicateHTTPError as exc:
            etype = "rate_limited" if exc.status_code == 429 else "broker_error"
            return error_response(
                error=f"Myah Replicate broker failed ({exc.status_code}): {_redact(exc.message, bearer)}",
                error_type=etype,
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if data.get("success") is False:
            return error_response(
                error=str(data.get("error") or "Myah Replicate broker returned an error"),
                error_type=str(data.get("error_type") or "broker_error"),
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        image = data.get("image") or data.get("url") or data.get("media_url")
        if not isinstance(image, str) or not image:
            return error_response(
                error="Myah Replicate broker returned no image reference",
                error_type="invalid_output",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        return success_response(
            image=image,
            model=str(data.get("model") or model_id),
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
        )

    def _generate_direct(self, prompt: str, aspect: str, model_id: str) -> Dict[str, Any]:
        token = os.environ.get("REPLICATE_API_TOKEN", "").strip()
        if not token:
            return error_response(
                error=(
                    "Replicate image generation is not configured. Set REPLICATE_API_TOKEN "
                    "or configure the hosted Myah Replicate image broker."
                ),
                error_type="auth_required",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "input": {
                "prompt": prompt,
                "aspect_ratio": _model_aspect_ratio(model_id, aspect),
                "output_format": "png",
            }
        }

        try:
            prediction = _post_json(_direct_prediction_url(model_id), payload, headers, timeout=30.0)
            prediction = self._poll_prediction(prediction, headers, token)
        except ReplicateHTTPError as exc:
            etype = "rate_limited" if exc.status_code == 429 else "api_error"
            return error_response(
                error=f"Replicate image generation failed ({exc.status_code}): {_redact(exc.message, token)}",
                error_type=etype,
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        output_url = _extract_output_url(prediction.get("output"))
        if not output_url:
            return error_response(
                error="Replicate returned no usable image output",
                error_type="invalid_output",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            saved_path = save_url_image(output_url, prefix=f"replicate_{model_id.replace('/', '_')}")
            image_ref = str(saved_path)
        except Exception as exc:
            logger.warning("Replicate output URL could not be cached (%s); returning raw URL", exc)
            image_ref = output_url

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
        )

    def _poll_prediction(self, prediction: dict, headers: dict, token: str) -> dict:
        status = str(prediction.get("status") or "").lower()
        if status in {"succeeded", "successful", "success"}:
            return prediction
        if status in {"failed", "canceled", "cancelled"}:
            raise ReplicateHTTPError(0, str(prediction.get("error") or f"prediction {status}"))

        get_url = ""
        urls = prediction.get("urls")
        if isinstance(urls, dict):
            get_url = str(urls.get("get") or "")
        if not get_url and prediction.get("id"):
            get_url = f"{DIRECT_API_BASE}/predictions/{prediction['id']}"
        if not get_url:
            raise ReplicateHTTPError(0, "prediction response did not include a polling URL")

        deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS
        delay = POLL_INTERVAL_SECONDS
        while time.monotonic() < deadline:
            try:
                time.sleep(delay)
                current = _get_json(get_url, headers, timeout=30.0)
            except ReplicateHTTPError as exc:
                if exc.status_code == 429 and time.monotonic() < deadline:
                    delay = min(delay * 1.5, 10.0)
                    continue
                raise
            status = str(current.get("status") or "").lower()
            if status in {"succeeded", "successful", "success"}:
                return current
            if status in {"failed", "canceled", "cancelled"}:
                raise ReplicateHTTPError(0, _redact(str(current.get("error") or f"prediction {status}"), token))
            delay = min(delay * 1.25, 10.0)

        raise ReplicateHTTPError(0, f"prediction timed out after {DEFAULT_TIMEOUT_SECONDS}s")
