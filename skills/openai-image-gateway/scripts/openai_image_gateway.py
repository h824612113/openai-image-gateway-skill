#!/usr/bin/env python3
import argparse
import base64
import getpass
import hashlib
import io
import json
import mimetypes
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests


SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "local_config.json"
GIF_MODEL_CANDIDATES = (
    "gpt-image-2.6-sunburst",
    "gpt-image-2.6-flare",
)
DEFAULT_MODEL_CANDIDATES = (
    "gpt-image-2",
    *GIF_MODEL_CANDIDATES,
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image",
)
ENDPOINT_MODES = ("auto", "images", "responses")
GIF_PROFILES = ("auto", "generic", "cx")
CX_GIF_HOST = "api.www-cx-api.cn.mt"
BACKGROUND_POLL_SECONDS = 2
IMAGE_POLL_MIN_SECONDS = 5
IMAGE_TASK_STATUSES = {"queued", "in_progress"}
IMAGE_SUBMISSION_RETRY_STATUSES = {429, 502, 503, 504}
IMAGE_SUBMISSION_MAX_ATTEMPTS = 2
IMAGE_DOWNLOAD_RETRY_STATUSES = {429, 500, 502, 503, 504}
IMAGE_DOWNLOAD_MAX_ATTEMPTS = 3
IMAGE_DOWNLOAD_RETRY_SECONDS = 2
PENDING_REQUEST_FILENAME = "pending_image_request.json"
PARTIAL_IMAGE_COUNT = 3
MODEL_REJECTION_CODES = {"invalid_model", "model_not_found", "unsupported_model"}
MODEL_REJECTION_PHRASES = (
    "unsupported model",
    "model not found",
    "invalid model",
    "不支持的模型",
    "已下架模型",
)


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def normalize_base_url(raw):
    """Return the configured API prefix without inventing a ``/v1`` segment.

    OpenAI-compatible gateways do not agree on whether Images lives below
    ``/v1``.  Treating a bare host as ``<host>/v1`` changed a configured
    endpoint and broke gateways that expose ``/images/*`` at their root.  The
    operator can still configure a ``/v1`` suffix when that is the provider's
    actual API prefix.
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith("/responses"):
        raw = raw[: -len("/responses")].rstrip("/")
    return raw


def responses_endpoint(raw):
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith("/responses"):
        return raw
    if raw.endswith("/v1"):
        raw = raw[:-3].rstrip("/")
    return f"{raw}/responses"


def mask_key(key):
    key = key or ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def endpoint_fingerprint(raw_base_url, api_key):
    value = f"{normalize_base_url(raw_base_url)}\0{api_key}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def model_fingerprint(raw_base_url, api_key, endpoint_mode):
    value = f"{endpoint_fingerprint(raw_base_url, api_key)}\0{endpoint_mode}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def load_config():
    if not CONFIG_PATH.exists():
        fail(f"Config not found: {CONFIG_PATH}")
    try:
        # Accept UTF-8 files with or without BOM because some Windows editors add it.
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        fail(f"Invalid config JSON: {exc}")

    raw_base_url = (data.get("base_url", "") or "").strip().rstrip("/")
    base_url = normalize_base_url(raw_base_url)
    api_key = (data.get("api_key", "") or "").strip()
    model = (data.get("model", "") or "auto").strip()
    responses_model = (data.get("responses_model", "") or "").strip()
    model_candidates = data.get("model_candidates")
    if not isinstance(model_candidates, list):
        model_candidates = list(DEFAULT_MODEL_CANDIDATES)
    model_candidates = [str(item).strip() for item in model_candidates if str(item).strip()]
    resolved_model = (data.get("resolved_model", "") or "").strip()
    endpoint_mode = (data.get("endpoint_mode", "auto") or "auto").strip().lower()
    gif_profile = (data.get("gif_profile", "auto") or "auto").strip().lower()
    if not base_url or not api_key:
        fail(f"Config incomplete: {CONFIG_PATH}")
    if endpoint_mode not in ENDPOINT_MODES:
        fail(f"Invalid endpoint_mode in {CONFIG_PATH}: {endpoint_mode}")
    if gif_profile not in GIF_PROFILES:
        fail(f"Invalid gif_profile in {CONFIG_PATH}: {gif_profile}")
    current_fingerprint = endpoint_fingerprint(raw_base_url, api_key)
    last_successful_mode = (data.get("last_successful_mode", "") or "").strip().lower()
    last_successful_mode_is_current = bool(
        last_successful_mode in ("images", "responses")
        and data.get("last_successful_mode_fingerprint", "") == current_fingerprint
    )
    resolved_endpoint_mode = (
        data.get("resolved_endpoint_mode") or endpoint_mode
    ).strip().lower()
    active_model_mode = (
        endpoint_mode
        if endpoint_mode in ("images", "responses")
        else last_successful_mode if last_successful_mode_is_current else resolved_endpoint_mode
    )
    current_model_fingerprint = model_fingerprint(
        raw_base_url, api_key, resolved_endpoint_mode
    )
    return {
        "raw_base_url": raw_base_url,
        "base_url": base_url,
        "responses_base_url": responses_endpoint(raw_base_url),
        "api_key": api_key,
        "model": model,
        "responses_model": responses_model,
        "model_candidates": model_candidates,
        "resolved_model": resolved_model,
        "resolved_endpoint_mode": resolved_endpoint_mode,
        "model_fingerprint": data.get("model_fingerprint", ""),
        "model_cache_is_current": bool(
            resolved_model
            and data.get("model_fingerprint", "") == current_model_fingerprint
            and resolved_endpoint_mode == active_model_mode
            and active_model_mode in ("images", "responses")
        ),
        "endpoint_mode": endpoint_mode,
        "gif_profile": gif_profile,
        "endpoint_fingerprint": current_fingerprint,
        "last_successful_mode": last_successful_mode,
        "last_successful_mode_is_current": last_successful_mode_is_current,
    }


def write_config(payload):
    CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(CONFIG_PATH, 0o600)


def save_config(base_url, api_key, model, responses_model, endpoint_mode, gif_profile=None):
    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            pass
    fingerprint = endpoint_fingerprint(base_url, api_key)
    if responses_model is None:
        responses_model = existing.get("responses_model", "")
    payload = {
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model": (model or "auto").strip() or "auto",
        "responses_model": (responses_model or "").strip(),
        "endpoint_mode": endpoint_mode,
        "gif_profile": gif_profile if gif_profile is not None else existing.get("gif_profile", "auto"),
    }
    if payload["gif_profile"] not in GIF_PROFILES:
        fail(f"Invalid GIF profile: {payload['gif_profile']}")
    if isinstance(existing.get("model_candidates"), list):
        payload["model_candidates"] = existing["model_candidates"]
    if existing.get("last_successful_mode_fingerprint") == fingerprint:
        for key in ("last_successful_mode", "last_successful_mode_fingerprint"):
            if existing.get(key):
                payload[key] = existing[key]
    write_config(payload)
    print(f"Saved config: {CONFIG_PATH}")
    print(f"Images Base URL: {normalize_base_url(payload['base_url'])}")
    print(f"Responses URL: {responses_endpoint(payload['base_url'])}")
    print(f"API Key: {mask_key(payload['api_key'])}")
    print(f"Model: {payload['model']}")
    print(f"Responses model: {payload['responses_model'] or '(uses image model candidates)'}")
    print(f"Endpoint mode: {payload['endpoint_mode']}")
    print(f"GIF profile: {payload['gif_profile']}")


def resolve_gif_profile(cfg, args):
    profile = getattr(args, "gif_profile", None) or cfg.get("gif_profile", "auto")
    if profile == "auto":
        return "cx" if urlparse(cfg["base_url"]).hostname == CX_GIF_HOST else "generic"
    return profile


def save_endpoint_mode(endpoint_mode):
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    data["endpoint_mode"] = endpoint_mode
    write_config(data)


def save_generation_success(cfg, endpoint_mode, model):
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    data["last_successful_mode"] = endpoint_mode
    data["last_successful_mode_fingerprint"] = cfg["endpoint_fingerprint"]
    data["resolved_model"] = model
    data["resolved_endpoint_mode"] = endpoint_mode
    data["model_fingerprint"] = model_fingerprint(
        cfg["raw_base_url"], cfg["api_key"], endpoint_mode
    )
    write_config(data)


def model_candidates(cfg, endpoint_mode, override_model=None, available_models=None):
    ordered = []

    def add(model):
        model = (model or "").strip()
        if model and model.lower() != "auto" and model not in ordered:
            ordered.append(model)

    add(override_model)
    if endpoint_mode == "responses":
        add(cfg.get("responses_model"))
    if cfg.get("model", "").lower() != "auto":
        add(cfg.get("model"))
    if cfg.get("model_cache_is_current"):
        add(cfg.get("resolved_model"))

    configured_candidates = cfg.get("model_candidates", DEFAULT_MODEL_CANDIDATES)
    if available_models:
        for model in configured_candidates:
            if model in available_models:
                add(model)
        discovered = sorted(
            model
            for model in available_models
            if any(token in model.lower() for token in ("image", "dall-e", "flux", "imagen"))
        )
        for model in discovered:
            add(model)

    for model in configured_candidates:
        add(model)

    if not ordered:
        fail("No image model candidates configured")
    return ordered


def model_list_urls(cfg):
    """Return conservative, read-only model-list candidates for a gateway.

    Images uses the configured prefix verbatim.  A few gateways expose Images
    at their host root but keep the model list below ``/v1``; trying that
    second location keeps ``test`` and optional model discovery useful without
    changing the generation route.
    """
    base_url = cfg["base_url"].rstrip("/")
    candidates = [f"{base_url}/models"]
    parsed = urlparse(base_url)
    if not parsed.path.rstrip("/"):
        candidates.append(f"{base_url}/v1/models")
    return tuple(candidates)


def gif_model_candidates(cfg, override_model=None, available_models=None):
    # An explicit gateway alias must remain pinned; a rejected animation must
    # never silently fall back to a static-image model.
    if override_model:
        return [override_model]
    candidates = model_candidates(cfg, "images", available_models=available_models)
    compatible = [
        model for model in candidates
        if any(model == name or model.startswith(f"{name}-") for name in GIF_MODEL_CANDIDATES)
    ]
    if available_models:
        compatible = [model for model in compatible if model in available_models]
        if not compatible:
            fail("No GIF-capable GPT Image 2.6 model is listed. Use --model for a provider GIF alias.")
    return compatible or list(GIF_MODEL_CANDIDATES)


def fetch_available_models(cfg, timeout):
    for url in model_list_urls(cfg):
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            continue

        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            continue

        data = payload.get("data")
        if isinstance(data, list):
            return {
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
    return set()


def error_text(resp):
    if not resp.text:
        return str(resp.status_code)
    try:
        payload = resp.json()
    except Exception:
        return resp.text[:1000]

    err = payload.get("error")
    if isinstance(err, dict) and err.get("message"):
        return err["message"]
    msg = payload.get("msg")
    if isinstance(msg, str) and msg.strip():
        return msg
    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail
    return resp.text[:1000]


def is_model_rejection(resp):
    if resp.status_code not in (400, 404):
        return False

    try:
        payload = resp.json()
    except Exception:
        payload = {}

    error = payload.get("error")
    if isinstance(error, dict):
        if error.get("param") == "model":
            return True
        if str(error.get("code", "")).lower() in MODEL_REJECTION_CODES:
            return True

    message = error_text(resp).lower()
    return any(phrase in message for phrase in MODEL_REJECTION_PHRASES)


def result_url_host(url):
    """Return a non-secret host label for diagnostics."""
    parsed = urlparse(str(url))
    return parsed.hostname or parsed.netloc or "(unknown)"


def result_download_retry_seconds(response):
    """Use a bounded Retry-After delay for a transient result download error."""
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After")
    if retry_after is None:
        retry_after = next(
            (value for key, value in headers.items() if str(key).lower() == "retry-after"),
            None,
        )
    try:
        delay = int(retry_after)
    except (TypeError, ValueError):
        delay = IMAGE_DOWNLOAD_RETRY_SECONDS
    # A signed URL should be fetched promptly; never let a provider-supplied
    # header make the CLI sleep indefinitely.
    return min(30, max(IMAGE_DOWNLOAD_RETRY_SECONDS, delay))


def download_result_image(url, timeout=120):
    """Download one completed result URL without re-submitting generation.

    Image2 result URLs are signed public URLs.  A task has already completed by
    this point, so retries are restricted to this exact URL and never invoke a
    second POST.  The URL may contain a provider token; diagnostics therefore
    expose only its host.
    """
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        fail(f"Image result URL is invalid (host {result_url_host(url)})")

    last_error = None
    for attempt in range(1, IMAGE_DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            # Signed result URLs are public; forwarding the gateway Bearer can
            # leak credentials to a different provider or invalidate signing.
            response = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"network error: {exc}"
            if attempt < IMAGE_DOWNLOAD_MAX_ATTEMPTS:
                print(
                    f"Transient result-image download error from host {result_url_host(url)}; "
                    f"retrying ({attempt}/{IMAGE_DOWNLOAD_MAX_ATTEMPTS - 1}).",
                    file=sys.stderr,
                )
                time.sleep(IMAGE_DOWNLOAD_RETRY_SECONDS)
                continue
            fail(
                f"Image download failed for result host {result_url_host(url)}: "
                f"{last_error}. The generation task is already complete; do not resubmit."
            )

        if response.status_code == 200:
            return response.content

        last_error = f"HTTP {response.status_code}: {response.text[:500]}"
        if response.status_code in IMAGE_DOWNLOAD_RETRY_STATUSES and attempt < IMAGE_DOWNLOAD_MAX_ATTEMPTS:
            print(
                f"Transient result-image download HTTP {response.status_code} from host "
                f"{result_url_host(url)}; retrying ({attempt}/{IMAGE_DOWNLOAD_MAX_ATTEMPTS - 1}).",
                file=sys.stderr,
            )
            time.sleep(result_download_retry_seconds(response))
            continue
        fail(
            f"Image download failed for result host {result_url_host(url)}: {last_error}. "
            "The generation task is already complete; do not resubmit."
        )

    # The loop always returns or calls fail; retain a defensive guard for type
    # checkers and future changes to the retry policy.
    fail(
        f"Image download failed for result host {result_url_host(url)}: "
        "retry budget exhausted. The generation task is already complete; do not resubmit."
    )


def extract_image_bytes(payload, api_key=None, timeout=120):
    """Extract image bytes from an Images API response.

    ``api_key`` remains accepted for compatibility with older callers but is
    deliberately ignored when downloading signed result URLs.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        fail(f"Unexpected generation payload: {payload}")

    for item in data:
        if not isinstance(item, dict):
            continue
        for key in ("b64_json", "base64", "image_b64"):
            value = item.get(key)
            if value:
                return base64.b64decode(value)
        url = item.get("url")
        if url:
            return download_result_image(url, timeout=timeout)
    fail("No image data returned")


def image_poll_after(value, fallback=IMAGE_POLL_MIN_SECONDS):
    """Normalize a provider poll hint while enforcing Image2's 5s minimum."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = fallback
    return max(IMAGE_POLL_MIN_SECONDS, value)


def retry_after_seconds(response, fallback=IMAGE_POLL_MIN_SECONDS):
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After")
    if retry_after is None:
        retry_after = next(
            (value for key, value in headers.items() if str(key).lower() == "retry-after"),
            None,
        )
    return image_poll_after(retry_after, fallback=fallback)


def image_task_url(cfg, task_id):
    # Task IDs are opaque provider values; quote them so a malformed value
    # cannot alter the polling path.
    return f"{cfg['base_url']}/images/tasks/{quote(str(task_id), safe='')}"


def normalize_idempotency_key(value):
    """Validate a caller-supplied key without allowing log/control injection."""
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    if len(key) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        fail("Invalid --idempotency-key: use a non-empty value up to 255 characters")
    return key


def image_request_fingerprint(endpoint, payload, reference_path=None):
    """Create a non-secret fingerprint for an exact Images request shape."""
    material = {"endpoint": endpoint, "payload": payload}
    if reference_path is not None:
        path = Path(reference_path).expanduser()
        try:
            material["reference_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            material["reference_name"] = path.name
        except OSError:
            # Validation happens before submission; this keeps diagnostics safe
            # if a file disappears between validation and fingerprinting.
            material["reference_name"] = path.name
            material["reference_sha256"] = "unavailable"
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pending_request_path():
    """Return a config-adjacent, user-private recovery record path."""
    # CONFIG_PATH can be a symlink from a checked-out skill to ~/.codex.  Resolve
    # it so recovery metadata does not unexpectedly dirty the source checkout.
    try:
        config_path = CONFIG_PATH.resolve()
    except OSError:
        config_path = CONFIG_PATH
    return config_path.parent / PENDING_REQUEST_FILENAME


def save_pending_request(endpoint, model, idempotency_key, fingerprint):
    """Persist only replay metadata; never persist the prompt or API key."""
    if not idempotency_key:
        return
    path = pending_request_path()
    record = {
        "endpoint": endpoint,
        "model": model,
        "idempotency_key": idempotency_key,
        "request_fingerprint": fingerprint,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        # Recovery metadata is best effort and must never hide the upstream
        # error or turn a successful generation into a local failure.
        return


def clear_pending_request(idempotency_key):
    """Remove recovery metadata only when it belongs to the completed request."""
    if not idempotency_key:
        return
    path = pending_request_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if record.get("idempotency_key") != idempotency_key:
        return
    try:
        path.unlink()
    except OSError:
        return


def ambiguous_image_submission(reason, endpoint, model, idempotency_key, fingerprint):
    """Stop after one safe replay and leave exact replay instructions."""
    save_pending_request(endpoint, model, idempotency_key, fingerprint)
    fail(
        "Images submission outcome is ambiguous "
        f"({reason}); the gateway may still be processing it. "
        f"Replay the identical request with --idempotency-key {idempotency_key} "
        f"(request fingerprint {fingerprint}) instead of changing the prompt or options."
    )


def submit_images_request(request_fn, endpoint, model, async_mode, idempotency_key, fingerprint):
    """Submit an Images request with one idempotent replay for transient failures.

    ``request_fn`` receives the key so multipart requests can reopen their file
    handle on the replay.  Once both attempts are ambiguous, we stop and retain
    the key rather than silently creating another billable generation.
    """
    attempts = 0
    while True:
        try:
            response = request_fn(idempotency_key)
        except requests.RequestException as exc:
            if async_mode and attempts + 1 < IMAGE_SUBMISSION_MAX_ATTEMPTS:
                attempts += 1
                print(
                    "Transient Images submission error; retrying once with the same "
                    "Idempotency-Key.",
                    file=sys.stderr,
                )
                time.sleep(IMAGE_POLL_MIN_SECONDS)
                continue
            if async_mode:
                ambiguous_image_submission(
                    f"network error: {exc}",
                    endpoint,
                    model,
                    idempotency_key,
                    fingerprint,
                )
            raise

        if (
            async_mode
            and response.status_code in IMAGE_SUBMISSION_RETRY_STATUSES
            and attempts + 1 < IMAGE_SUBMISSION_MAX_ATTEMPTS
        ):
            attempts += 1
            delay = (
                retry_after_seconds(response)
                if response.status_code == 429
                else IMAGE_POLL_MIN_SECONDS
            )
            print(
                f"Transient Images submission HTTP {response.status_code}; retrying once "
                "with the same Idempotency-Key.",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue

        if async_mode and response.status_code in IMAGE_SUBMISSION_RETRY_STATUSES:
            ambiguous_image_submission(
                f"HTTP {response.status_code}",
                endpoint,
                model,
                idempotency_key,
                fingerprint,
            )
        return response


def await_image_task(cfg, task, timeout):
    """Poll an Image2 async task until it completes or the budget expires.

    Once a task ID exists, transient 429/5xx responses are handled by polling
    the same task again.  We never submit a second generation request here.
    """
    task_id = task.get("id")
    if not task_id:
        fail(f"Async image response is missing an id: {task}")

    deadline = time.monotonic() + max(timeout, IMAGE_POLL_MIN_SECONDS)
    poll_after = image_poll_after(task.get("poll_after"))
    last_poll_error = None

    while True:
        status = str(task.get("status", "")).lower()
        if status == "completed":
            return task
        if status == "failed":
            error = task.get("error") or {}
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or "生图失败"
            else:
                message = str(error)
            fail(f"Image task {task_id} failed: {message}")
        if status not in IMAGE_TASK_STATUSES:
            fail(f"Image task {task_id} returned unexpected status: {status or '(missing)'}")

        if time.monotonic() >= deadline:
            detail = f"; last poll error: {last_poll_error}" if last_poll_error else ""
            fail(
                f"Image task {task_id} still {status} after {timeout}s. "
                f"It may still finish upstream; continue querying the task instead of resubmitting{detail}."
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        # Keep the documented minimum interval, but avoid sleeping beyond the
        # caller's budget when a provider asks for a very long delay.
        time.sleep(min(poll_after, remaining))
        try:
            response = requests.get(
                image_task_url(cfg, task_id),
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=max(1, min(timeout, int(max(1, remaining)))),
            )
        except requests.RequestException as exc:
            # A poll is idempotent.  Keep the task ID and try again within the
            # caller's bounded budget instead of creating a duplicate job.
            last_poll_error = str(exc)
            poll_after = image_poll_after(None, fallback=poll_after)
            continue

        if response.status_code == 429:
            last_poll_error = "HTTP 429"
            poll_after = retry_after_seconds(response, fallback=poll_after)
            continue
        if 500 <= response.status_code <= 599:
            last_poll_error = f"HTTP {response.status_code}"
            poll_after = retry_after_seconds(response, fallback=poll_after)
            continue
        if response.status_code not in (200, 202):
            fail(f"Image task poll HTTP {response.status_code}: {error_text(response)}")
        try:
            task = response.json()
        except (ValueError, json.JSONDecodeError):
            fail(f"Non-JSON image task response: {response.text[:500]}")
        if not isinstance(task, dict):
            fail(f"Unexpected image task response: {task}")
        poll_after = image_poll_after(task.get("poll_after"), fallback=poll_after)


def extract_image_bytes_from_responses(payload):
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("type") == "image_generation_call":
                for key in ("result", "b64_json", "base64", "image_b64"):
                    value = item.get(key)
                    if value:
                        return base64.b64decode(value)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    fail("No image data returned from responses endpoint")


def extract_image_bytes_from_stream(resp):
    saw_partial = False
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "response.image_generation_call.partial_image":
            # Partial previews are never written to disk; only the final image counts.
            saw_partial = True
        elif event_type == "response.completed":
            return extract_image_bytes_from_responses(event.get("response", {}))
        elif event_type in {"response.failed", "error"}:
            fail(f"Streaming generation failed: {error_event_text(event)}")

    if saw_partial:
        fail("Streaming ended before the final image; partial previews were discarded")
    fail("No image data returned from streaming responses endpoint")


def error_event_text(event):
    for holder in (event, event.get("response") or {}):
        if not isinstance(holder, dict):
            continue
        error = holder.get("error")
        if isinstance(error, dict) and error.get("message"):
            return error["message"]
        if isinstance(error, str) and error.strip():
            return error
    return json.dumps(event, ensure_ascii=False)[:500]


def await_background_response(cfg, payload, headers, timeout):
    # Bound the wait so a stuck job cannot poll forever; report instead of retrying.
    deadline = time.monotonic() + max(timeout, BACKGROUND_POLL_SECONDS)
    while payload.get("status") in {"queued", "in_progress"}:
        response_id = payload.get("id")
        if not response_id:
            fail(f"Background response is missing an id: {payload}")
        if time.monotonic() >= deadline:
            fail(
                f"Background generation still {payload['status']} after {timeout}s "
                f"(id {response_id}). It may still finish upstream; do not retry blindly."
            )
        time.sleep(BACKGROUND_POLL_SECONDS)
        resp = requests.get(
            f"{cfg['responses_base_url']}/{response_id}",
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code != 200:
            fail(f"Background poll HTTP {resp.status_code}: {error_text(resp)}")
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            fail(f"Non-JSON background response: {resp.text[:500]}")

    status = payload.get("status")
    if status in {"failed", "cancelled", "incomplete"}:
        fail(f"Background generation {status}: {error_event_text(payload)}")
    return payload


def preferred_endpoint_modes(cfg):
    base_order = (
        ("responses", "images")
        if cfg["raw_base_url"].endswith("/responses")
        else ("images", "responses")
    )
    if cfg["endpoint_mode"] in ("images", "responses"):
        preferred = cfg["endpoint_mode"]
    elif cfg.get("last_successful_mode_is_current"):
        preferred = cfg["last_successful_mode"]
    else:
        return base_order
    return (preferred,) + tuple(mode for mode in base_order if mode != preferred)


def probe_endpoint(cfg, endpoint_mode, timeout):
    url = (
        f"{cfg['base_url']}/images/generations"
        if endpoint_mode == "images"
        else cfg["responses_base_url"]
    )
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
                "X-Image-Gateway-Probe": "1",
            },
            json={},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {
            "mode": endpoint_mode,
            "reachable": False,
            "detail": f"error ({exc})",
        }

    reachable = response.status_code in (200, 202, 400, 422)
    if reachable:
        detail = f"{response.status_code} (reachable, generation unverified)"
    else:
        detail = f"{response.status_code} (unreachable)"
    return {"mode": endpoint_mode, "reachable": reachable, "detail": detail}


def read_only_check(cfg, timeout):
    last_detail = "no model-list response"
    for url in model_list_urls(cfg):
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            last_detail = f"error ({exc})"
            continue

        detail = str(response.status_code)
        if 200 <= response.status_code < 300:
            return {"reachable": True, "detail": detail}
        last_detail = detail
    return {"reachable": False, "detail": last_detail}


def probe_endpoints(cfg, timeout):
    return [probe_endpoint(cfg, mode, timeout) for mode in preferred_endpoint_modes(cfg)]


def format_probe_results(probe_results):
    return [f"{item['mode']}={item['detail']}" for item in probe_results]


def select_endpoint_mode(cfg, timeout, probe_results=None):
    if cfg["endpoint_mode"] in ("images", "responses"):
        return cfg["endpoint_mode"], []
    if cfg.get("last_successful_mode_is_current"):
        return cfg["last_successful_mode"], []

    probe_results = probe_results or probe_endpoints(cfg, timeout)
    results = format_probe_results(probe_results)
    for result in probe_results:
        if result["reachable"]:
            return result["mode"], results

    fail("No reachable image endpoint found (" + ", ".join(results) + ")")


def encode_image_data_url(image_path):
    path = Path(image_path).expanduser()
    if not path.is_file():
        fail(f"Reference image not found: {path}")
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{raw}"


def image_generation_tool(args, output_format):
    tool = {"type": "image_generation"}
    size = args.size or "1024x1024"
    if size != "auto":
        tool["size"] = size
    if args.quality != "auto":
        tool["quality"] = args.quality
    tool["output_format"] = output_format
    if output_format in {"jpeg", "webp"}:
        compression = getattr(args, "compression", None)
        if compression is not None:
            tool["output_compression"] = compression
    if getattr(args, "stream", False):
        tool["partial_images"] = PARTIAL_IMAGE_COUNT
    return tool


def generate_with_responses(cfg, args, timeout, output_format, available_models=None):
    content = [{"type": "input_text", "text": args.prompt}]
    if args.image:
        content.append({"type": "input_image", "image_url": encode_image_data_url(args.image)})

    stream = getattr(args, "stream", False)
    background = getattr(args, "background", False)
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    candidates = model_candidates(cfg, "responses", args.model, available_models)
    failures = []
    for model in candidates:
        responses_payload = {
            "model": model,
            "input": [{"role": "user", "content": content}],
            "tools": [image_generation_tool(args, output_format)],
        }
        if background:
            responses_payload["background"] = True
        if stream:
            responses_payload["stream"] = True
        try:
            resp = requests.post(
                cfg["responses_base_url"],
                headers=headers,
                json=responses_payload,
                timeout=timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            failures.append(f"{model}: {exc}")
            continue
        if resp.status_code == 200:
            if stream:
                raw = extract_image_bytes_from_stream(resp)
                save_generation_success(cfg, "responses", model)
                return raw, model
            try:
                payload = resp.json()
            except json.JSONDecodeError:
                fail(f"Non-JSON response: {resp.text[:500]}")
            payload = await_background_response(cfg, payload, headers, timeout)
            raw = extract_image_bytes_from_responses(payload)
            save_generation_success(cfg, "responses", model)
            return raw, model
        failures.append(f"{model}: HTTP {resp.status_code}: {error_text(resp)}")
        continue

    fail(
        "No configured image model was accepted by "
        f"{cfg['responses_base_url']}. Tried: {', '.join(candidates)}. "
        "Failures: " + "; ".join(failures)
    )


def is_http_url(value):
    parsed = urlparse((value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def images_payload(args, model, output_format):
    """Build the portable Images request body used by common gateways.

    Always sending the ordinary single-image, low-quality PNG defaults keeps
    the payload aligned with compatible synchronous Images implementations.
    The response parser already accepts both URL and Base64 results, so there
    is no need to force a provider-specific ``response_format`` field.
    """
    payload = {
        "model": model,
        "prompt": args.prompt,
        "n": 1,
        "output_format": output_format,
    }
    size = args.size or ("auto" if output_format == "gif" else "1024x1024")
    cx_gif = output_format == "gif" and getattr(args, "resolved_gif_profile", "generic") == "cx"
    if size != "auto" or cx_gif:
        payload["size"] = size
    image_size = getattr(args, "image_size", "auto")
    if image_size != "auto" and ":" in str(size):
        payload["image_size"] = image_size
    quality = getattr(args, "quality", "auto")
    if quality != "auto":
        payload["quality"] = quality
    if output_format in {"jpeg", "webp"}:
        compression = getattr(args, "compression", None)
        if compression is not None:
            payload["output_compression"] = compression
    if output_format == "gif":
        payload["frame_count"] = getattr(args, "frame_count", None) or (6 if cx_gif else 9)
        payload["frame_duration_ms"] = getattr(args, "frame_duration_ms", None) or 200
        if cx_gif:
            payload["stream"] = False
    response_format = getattr(args, "response_format", None) or ("url" if cx_gif else None)
    if response_format:
        payload["response_format"] = response_format
    return payload


def images_request_url(cfg, edit, async_mode):
    path = "images/edits" if edit else "images/generations"
    url = f"{cfg['base_url']}/{path}"
    return f"{url}?async=true" if async_mode else url


def images_headers(cfg, async_mode, json_body=False, idempotency_key=None):
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if async_mode:
        headers["Idempotency-Key"] = normalize_idempotency_key(idempotency_key) or str(uuid.uuid4())
    return headers


def image_url_shape_error(response):
    """Some OpenAI-compatible gateways require ``images[].image_url`` objects."""
    if response.status_code != 400:
        return False
    return "images[].image_url" in error_text(response).lower()


def parse_images_response(response, cfg, timeout, async_mode):
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        fail(f"Non-JSON Images response: {response.text[:500]}")
    if not isinstance(payload, dict):
        fail(f"Unexpected Images response: {payload}")

    if response.status_code == 202:
        return await_image_task(cfg, payload, timeout)
    if response.status_code == 200:
        # A few compatible gateways complete immediately even when async=true.
        if async_mode and payload.get("status") in IMAGE_TASK_STATUSES:
            return await_image_task(cfg, payload, timeout)
        return payload
    fail(f"Unexpected Images response HTTP {response.status_code}: {error_text(response)}")


def generate_with_images(cfg, args, timeout, output_format, available_models=None):
    candidates = (
        gif_model_candidates(cfg, args.model, available_models)
        if output_format == "gif"
        else model_candidates(cfg, "images", args.model, available_models)
    )
    async_mode = getattr(args, "background", False)
    supplied_key = normalize_idempotency_key(getattr(args, "idempotency_key", None))
    for candidate_index, model in enumerate(candidates):
        payload = images_payload(args, model, output_format)
        edit = bool(args.image)
        endpoint = images_request_url(cfg, edit, async_mode)

        # A model fallback changes the request body, so it must get a fresh key.
        idempotency_key = (
            (
                (supplied_key or str(uuid.uuid4()))
                if candidate_index == 0
                else str(uuid.uuid4())
            )
            if async_mode
            else None
        )

        def submit(request_fn, request_payload, reference_path=None, key=idempotency_key):
            return submit_images_request(
                request_fn,
                endpoint,
                model,
                async_mode,
                key,
                image_request_fingerprint(
                    endpoint, request_payload, reference_path=reference_path
                ),
            )

        if edit and is_http_url(args.image):
            json_payload = dict(payload)
            json_payload["images"] = [args.image.strip()]

            def post_url_edit(key, request_payload=json_payload):
                return requests.post(
                    endpoint,
                    headers=images_headers(
                        cfg, async_mode, json_body=True, idempotency_key=key
                    ),
                    json=request_payload,
                    timeout=timeout,
                )

            try:
                response = submit(post_url_edit, json_payload)
            except requests.RequestException as exc:
                if output_format != "gif" and candidate_index + 1 < len(candidates):
                    print(
                        f"Image model {model} failed with network error {exc}; "
                        "trying the next configured model.",
                        file=sys.stderr,
                    )
                    continue
                raise
            # The Image2 document accepts URL strings, while some gateways
            # expose the OpenAI-compatible object form.  A validation-only 400
            # is safe to retry with the alternate shape and a fresh key because
            # the request body changes.
            if image_url_shape_error(response):
                json_payload["images"] = [{"image_url": args.image.strip()}]
                idempotency_key = str(uuid.uuid4()) if async_mode else None
                try:
                    response = submit(post_url_edit, json_payload, key=idempotency_key)
                except requests.RequestException as exc:
                    if output_format != "gif" and candidate_index + 1 < len(candidates):
                        print(
                            f"Image model {model} failed with network error {exc}; "
                            "trying the next configured model.",
                            file=sys.stderr,
                        )
                        continue
                    raise
        elif edit:
            image_path = Path(args.image).expanduser()
            if not image_path.is_file():
                fail(f"Reference image not found: {image_path}")
            mime_type = mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
            form_data = {
                key: json.dumps(value) if isinstance(value, bool) else str(value)
                for key, value in payload.items()
            }
            image_field = (
                "image[]"
                if output_format == "gif" and getattr(args, "resolved_gif_profile", "generic") == "cx"
                else "image"
            )

            def post_file_edit(key):
                # Reopen the file for each attempt; requests consumes the file
                # stream and a replay must send byte-for-byte identical content.
                with image_path.open("rb") as image_file:
                    return requests.post(
                        endpoint,
                        headers=images_headers(
                            cfg, async_mode, idempotency_key=key
                        ),
                        data=form_data,
                        files={image_field: (image_path.name, image_file, mime_type)},
                        timeout=timeout,
                    )

            try:
                response = submit(post_file_edit, form_data, reference_path=image_path)
            except requests.RequestException as exc:
                if output_format != "gif" and candidate_index + 1 < len(candidates):
                    print(
                        f"Image model {model} failed with network error {exc}; "
                        "trying the next configured model.",
                        file=sys.stderr,
                    )
                    continue
                raise
        else:
            def post_generation(key, request_payload=payload):
                return requests.post(
                    endpoint,
                    headers=images_headers(
                        cfg, async_mode, json_body=True, idempotency_key=key
                    ),
                    json=request_payload,
                    timeout=timeout,
                )

            try:
                response = submit(post_generation, payload)
            except requests.RequestException as exc:
                if output_format != "gif" and candidate_index + 1 < len(candidates):
                    print(
                        f"Image model {model} failed with network error {exc}; "
                        "trying the next configured model.",
                        file=sys.stderr,
                    )
                    continue
                raise

        if response.status_code in (200, 202):
            response_payload = parse_images_response(response, cfg, timeout, async_mode)
            result_payload = response_payload.get("result", response_payload)
            raw = extract_image_bytes(result_payload, timeout=timeout)
            if output_format == "gif":
                validate_gif_output(raw)
            clear_pending_request(idempotency_key)
            save_generation_success(cfg, "images", model)
            return raw, model, "edit" if edit else "generate"
        if is_model_rejection(response):
            continue

        hint = ""
        if output_format == "gif" and response.status_code in IMAGE_DOWNLOAD_RETRY_STATUSES:
            hint = "; GIF submission may already have completed; do not resubmit automatically"
        elif response.status_code in (502, 503, 504):
            if async_mode:
                hint = "；异步提交也失败，请检查网关 Images 上游和模型能力"
            else:
                hint = "；可重试 `--background` 走异步 Images 接口，或检查网关 Images 上游"
        if output_format != "gif" and not async_mode and candidate_index + 1 < len(candidates):
            print(
                f"Image model {model} failed with HTTP {response.status_code}; "
                "trying the next configured model.",
                file=sys.stderr,
            )
            continue
        fail(f"HTTP {response.status_code}: {error_text(response)}{hint}")

    fail(
        "No configured image model was accepted by "
        f"{cfg['base_url']}/images. Tried: {', '.join(candidates)}. "
        "Ask the provider for the exact image-generation model ID."
    )


def infer_format(out_path, fmt):
    if fmt:
        return fmt
    suffix = Path(out_path).suffix.lower().lstrip(".")
    if suffix in {"png", "jpeg", "jpg", "webp", "gif"}:
        return "jpeg" if suffix == "jpg" else suffix
    return "png"


def animation_integer(raw, minimum, maximum, name):
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer from {minimum} to {maximum}") from exc
    if not minimum <= value <= maximum:
        raise argparse.ArgumentTypeError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def gif_decoder():
    try:
        from PIL import Image
    except ImportError:
        fail("GIF output requires Pillow. Install it with: python3 -m pip install Pillow")
    return Image


def validate_generation_options(args, out_path, output_format, gif_profile="generic"):
    if output_format != "gif":
        if out_path.suffix.lower() == ".gif":
            fail("A .gif output path requires --format gif (or omit --format to infer it)")
        if args.frame_count is not None or args.frame_duration_ms is not None:
            fail("--frame-count and --frame-duration-ms require GIF output")
        if getattr(args, "gif_profile", None) is not None:
            fail("--gif-profile requires GIF output")
        return

    if out_path.suffix and out_path.suffix.lower() != ".gif":
        fail("GIF output requires a .gif file extension (or a path without an extension)")
    if gif_profile == "cx" and args.frame_count is not None and args.frame_count > 16:
        fail("The cx GIF profile supports 2-16 frames (default 6)")
    if args.image:
        if gif_profile != "cx":
            fail("Reference-image GIF output requires --gif-profile cx and a supporting gateway")
        if is_http_url(args.image):
            fail("The cx GIF profile requires a local --image file uploaded as image[]")
        if not Path(args.image).expanduser().is_file():
            fail(f"Reference image not found: {args.image}")
    if args.stream:
        fail("GIF output does not support --stream")
    if args.compression is not None:
        fail("GIF output does not support --compression")
    if args.image_size != "auto":
        fail("GIF output uses --size WIDTHxHEIGHT or auto, not --image-size")
    if args.size and args.size != "auto":
        try:
            width, height = (int(part) for part in args.size.split("x"))
        except (ValueError, TypeError):
            fail("GIF --size must be auto or WIDTHxHEIGHT with positive multiples of 16")
        if width <= 0 or height <= 0 or width % 16 or height % 16:
            fail("GIF --size dimensions must be positive multiples of 16")
    # Check the optional dependency before a potentially billable submission.
    gif_decoder()


def validate_gif_structure(raw):
    """Check block boundaries that Pillow's tolerant GIF decoder can ignore."""
    if len(raw) < 14 or raw[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("Invalid GIF header")
    packed = raw[10]
    offset = 13 + (3 * (2 << (packed & 7)) if packed & 128 else 0)
    while offset < len(raw):
        marker = raw[offset]
        offset += 1
        if marker == 0x3B:
            if offset != len(raw):
                raise ValueError("Unexpected data after GIF trailer")
            return
        if marker == 0x21:
            # Extensions have a label followed by length-prefixed sub-blocks.
            offset += 1
        elif marker == 0x2C:
            if offset + 9 > len(raw):
                raise ValueError("Truncated GIF image descriptor")
            packed = raw[offset + 8]
            offset += 9 + (3 * (2 << (packed & 7)) if packed & 128 else 0)
            offset += 1  # LZW minimum code size; Pillow validates image data.
        else:
            raise ValueError("Invalid GIF block marker")
        while True:
            if offset >= len(raw):
                raise ValueError("Unterminated GIF sub-blocks")
            count = raw[offset]
            offset += 1
            if count == 0:
                break
            offset += count
            if offset > len(raw):
                raise ValueError("Truncated GIF sub-block")
    raise ValueError("Missing GIF trailer")


def validate_gif_output(raw):
    image_module = gif_decoder()
    reason = ""
    try:
        validate_gif_structure(raw)
        with image_module.open(io.BytesIO(raw)) as image:
            if image.format != "GIF" or image.n_frames < 2:
                reason = "the result is not a multi-frame GIF"
            else:
                for frame in range(image.n_frames):
                    image.seek(frame)
                    image.load()
    except (OSError, ValueError, EOFError, image_module.DecompressionBombError):
        reason = "the result is invalid or truncated"
    if reason:
        fail(
            f"Expected an animated GIF, but {reason}. Output was not replaced. "
            "The generation already completed; do not resubmit automatically."
        )


def compression_value(raw):
    """Parse the documented JPEG/WebP compression range (0 through 100)."""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("compression must be an integer from 0 to 100") from exc
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("compression must be an integer from 0 to 100")
    return value


def default_output_path(fmt):
    output_format = fmt or "png"
    output_dir = Path.cwd() / "generated"
    filename = f"image-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{output_format}"
    return output_dir / filename


def command_config(args):
    api_key = args.key or getpass.getpass("API key: ").strip()
    if not api_key:
        fail("API key is required")
    save_config(
        args.base,
        api_key,
        args.model,
        args.responses_model,
        args.endpoint_mode,
        args.gif_profile,
    )


def command_test(args):
    cfg = load_config()
    print(f"Config: {CONFIG_PATH}")
    print(f"Base URL: {cfg['base_url']}")
    print(f"Responses URL: {cfg['responses_base_url']}")
    print(f"API Key: {mask_key(cfg['api_key'])}")
    probe_generation_route = getattr(args, "probe_generation_route", False)
    if not probe_generation_route and not args.select:
        result = read_only_check(cfg, args.timeout)
        print(f"Read-only check: {result['detail']}")
        print(f"Configured endpoint mode: {cfg['endpoint_mode']} (config unchanged)")
        return

    probe_results = probe_endpoints(cfg, args.timeout)
    print(f"Generation-route diagnostics: {', '.join(format_probe_results(probe_results))}")
    if not args.select:
        print(f"Configured endpoint mode: {cfg['endpoint_mode']} (config unchanged)")
        return
    if cfg["endpoint_mode"] != "auto":
        print(f"Configured endpoint mode is explicit: {cfg['endpoint_mode']} (config unchanged)")
        return

    endpoint_mode, _ = select_endpoint_mode(cfg, args.timeout, probe_results)
    save_endpoint_mode(endpoint_mode)
    print(f"Selected diagnostic endpoint: {endpoint_mode} (generation unverified)")


def command_generate(args):
    out_path = Path(args.out).expanduser() if args.out else default_output_path(args.format)
    output_format = infer_format(str(out_path), args.format)
    cfg = load_config()
    args.resolved_gif_profile = resolve_gif_profile(cfg, args)
    validate_generation_options(args, out_path, output_format, args.resolved_gif_profile)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    endpoint_mode = cfg["endpoint_mode"]
    if output_format == "gif":
        if endpoint_mode == "responses":
            fail("GIF output requires the images endpoint; configure --endpoint-mode images or auto")
        endpoint_mode = "images"
    if args.response_format:
        if endpoint_mode == "responses":
            fail("--response-format requires the images endpoint")
        endpoint_mode = "images"
    if endpoint_mode == "auto":
        endpoint_mode, _ = select_endpoint_mode(cfg, args.timeout)

    if endpoint_mode == "images":
        if getattr(args, "stream", False):
            fail("--stream requires the responses endpoint, but images was selected")
    elif getattr(args, "background", False) and getattr(args, "stream", False):
        fail("--background and --stream cannot be used together")

    available_models = set()
    if not args.model and (output_format == "gif" or not cfg.get("model_cache_is_current")):
        available_models = fetch_available_models(cfg, args.timeout)

    if endpoint_mode == "responses":
        raw, used_model = generate_with_responses(
            cfg, args, args.timeout, output_format, available_models
        )
        used_mode = "responses-edit" if args.image else "responses"
    else:
        raw, used_model, used_mode = generate_with_images(
            cfg, args, args.timeout, output_format, available_models
        )

    out_path.write_bytes(raw)
    print(f"Saved image: {out_path}")
    print(f"Bytes: {len(raw)}")
    print(f"Model: {used_model}")
    print(f"Mode: {used_mode}")
    if output_format == "gif":
        print(f"GIF profile: {args.resolved_gif_profile}")


def build_parser():
    parser = argparse.ArgumentParser(description="OpenAI-compatible image gateway helper")
    sub = parser.add_subparsers(dest="command", required=True)

    config_parser = sub.add_parser("config", help="Save base URL, API key, and model preferences")
    config_parser.add_argument(
        "--base",
        required=True,
        help="Exact gateway API prefix (include /v1 only when the provider requires it)",
    )
    config_parser.add_argument("--key", help="API key; omit to enter it without echoing")
    config_parser.add_argument("--model", default="auto", help="Preferred model or auto")
    config_parser.add_argument(
        "--responses-model",
        default=None,
        help="Primary Responses API model; separate from the Images API model",
    )
    config_parser.add_argument(
        "--endpoint-mode",
        choices=ENDPOINT_MODES,
        default="auto",
        help="Use images or responses directly, or auto-detect once during generation.",
    )
    config_parser.set_defaults(func=command_config)
    config_parser.add_argument(
        "--gif-profile", choices=GIF_PROFILES, default=None,
        help="GIF contract: auto detects the documented CX host; cx enables reference GIFs (2-16 frames, default 6)",
    )

    test_parser = sub.add_parser("test", help="Diagnose image endpoint reachability without generating")
    test_parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    test_parser.add_argument(
        "--select",
        action="store_true",
        help="When endpoint_mode is auto, save the first reachable diagnostic candidate",
    )
    test_parser.add_argument(
        "--probe-generation-route",
        action="store_true",
        help="POST zero-prompt diagnostics to image routes; does not generate an image",
    )
    test_parser.set_defaults(func=command_test)

    gen_parser = sub.add_parser("generate", help="Generate an image to a target path")
    gen_parser.add_argument("--prompt", required=True, help="Prompt text")
    gen_parser.add_argument("--out", help="Target file path; defaults to ./generated/image-YYYYMMDD-HHMMSS.<format>")
    gen_parser.add_argument("--image", help="Reference image path for edit mode; GIF references require the cx profile")
    gen_parser.add_argument("--size", help="Image size/ratio or auto; defaults to 1024x1024, or auto for GIF")
    gen_parser.add_argument(
        "--image-size",
        default="auto",
        choices=["auto", "1K", "2K", "4K"],
        help="Resolution tier for ratio sizes on the Images endpoint",
    )
    gen_parser.add_argument("--quality", default="low", choices=["auto", "low", "medium", "high"])
    gen_parser.add_argument("--format", choices=["png", "jpeg", "webp", "gif"], help="Output format")
    gen_parser.add_argument(
        "--gif-profile", choices=GIF_PROFILES, default=None,
        help="Override the configured GIF contract: cx supports local reference GIFs, 2-16 frames, default 6",
    )
    gen_parser.add_argument(
        "--response-format", choices=["url", "b64_json"],
        help="Images API return transport; cx GIF defaults to url (local output still uses --format)",
    )
    gen_parser.add_argument(
        "--frame-count",
        type=lambda raw: animation_integer(raw, 2, 64, "frame-count"),
        help="GIF frames: generic 2-64/default 9; cx 2-16/default 6",
    )
    gen_parser.add_argument(
        "--frame-duration-ms",
        type=lambda raw: animation_integer(raw, 20, 2000, "frame-duration-ms"),
        help="GIF delay per frame in milliseconds (20-2000; default 200; API rounds to 10 ms)",
    )
    gen_parser.add_argument(
        "--compression",
        type=compression_value,
        default=None,
        help="Compression for jpeg/webp (only sent when explicitly set)",
    )
    gen_parser.add_argument("--model", help="Override model")
    gen_parser.add_argument(
        "--idempotency-key",
        help="Reuse this key to safely replay an ambiguous --background Images submission",
    )
    gen_parser.add_argument(
        "--background",
        action="store_true",
        help="Use the async Images/Responses route and poll until completion.",
    )
    gen_parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream responses-endpoint events and save only the final image.",
    )
    gen_parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="HTTP timeout and async task polling budget in seconds",
    )
    gen_parser.set_defaults(func=command_generate)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except requests.Timeout:
        fail("Request timed out. The gateway may have completed generation; do not retry automatically.")
    except requests.RequestException as exc:
        fail(f"Network request failed: {exc}")


if __name__ == "__main__":
    main()
