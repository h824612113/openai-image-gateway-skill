#!/usr/bin/env python3
import argparse
import base64
import getpass
import hashlib
import json
import mimetypes
import os
import sys
from datetime import datetime
from pathlib import Path

import requests


SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "local_config.json"
DEFAULT_MODEL_CANDIDATES = (
    "gpt-image-2",
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image",
)
ENDPOINT_MODES = ("auto", "images", "responses")
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
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith("/responses"):
        raw = raw[: -len("/responses")].rstrip("/")
    if raw.endswith("/v1"):
        return raw
    return f"{raw}/v1"


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
    cached_fingerprint = (data.get("endpoint_mode_fingerprint", "") or "").strip()
    if not base_url or not api_key:
        fail(f"Config incomplete: {CONFIG_PATH}")
    if endpoint_mode not in ENDPOINT_MODES:
        fail(f"Invalid endpoint_mode in {CONFIG_PATH}: {endpoint_mode}")
    current_fingerprint = endpoint_fingerprint(raw_base_url, api_key)
    resolved_endpoint_mode = (
        data.get("resolved_endpoint_mode") or endpoint_mode
    ).strip().lower()
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
            and resolved_endpoint_mode == endpoint_mode
            and endpoint_mode in ("images", "responses")
        ),
        "endpoint_mode": endpoint_mode,
        "endpoint_mode_is_current": cached_fingerprint == current_fingerprint,
        "endpoint_fingerprint": current_fingerprint,
    }


def save_config(base_url, api_key, model, endpoint_mode):
    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            pass
    fingerprint = endpoint_fingerprint(base_url, api_key)
    cached_mode = (existing.get("endpoint_mode", "") or "").strip().lower()
    cached_fingerprint = (existing.get("endpoint_mode_fingerprint", "") or "").strip()
    if endpoint_mode == "auto" and cached_mode in ("images", "responses") and cached_fingerprint == fingerprint:
        endpoint_mode = cached_mode

    payload = {
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model": (model or "auto").strip() or "auto",
        "endpoint_mode": endpoint_mode,
    }
    if endpoint_mode in ("images", "responses"):
        payload["endpoint_mode_fingerprint"] = fingerprint
    if existing.get("responses_model"):
        payload["responses_model"] = existing["responses_model"]
    if isinstance(existing.get("model_candidates"), list):
        payload["model_candidates"] = existing["model_candidates"]
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
    print(f"Saved config: {CONFIG_PATH}")
    print(f"Images Base URL: {normalize_base_url(payload['base_url'])}")
    print(f"Responses URL: {responses_endpoint(payload['base_url'])}")
    print(f"API Key: {mask_key(payload['api_key'])}")
    print(f"Model: {payload['model']}")
    print(f"Endpoint mode: {payload['endpoint_mode']}")


def save_endpoint_mode(endpoint_mode, fingerprint):
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    data["endpoint_mode"] = endpoint_mode
    data["endpoint_mode_fingerprint"] = fingerprint
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


def save_resolved_model(cfg, endpoint_mode, model):
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    data["resolved_model"] = model
    data["resolved_endpoint_mode"] = endpoint_mode
    data["model_fingerprint"] = model_fingerprint(
        cfg["raw_base_url"], cfg["api_key"], endpoint_mode
    )
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


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


def fetch_available_models(cfg, timeout):
    try:
        response = requests.get(
            f"{cfg['base_url']}/models",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException:
        return set()

    if response.status_code != 200:
        return set()
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return set()

    data = payload.get("data")
    if not isinstance(data, list):
        return set()
    return {
        item["id"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


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


def extract_image_bytes(payload, api_key, timeout):
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
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                fail(f"Image download failed HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.content
    fail("No image data returned")


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


def preferred_endpoint_modes(cfg):
    if cfg["raw_base_url"].endswith("/responses"):
        return ("responses", "images")
    return ("images", "responses")


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
            },
            json={},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, str(exc)

    return response.status_code in (200, 400, 422), str(response.status_code)


def select_endpoint_mode(cfg, timeout):
    results = []
    preferred = preferred_endpoint_modes(cfg)
    if cfg["endpoint_mode_is_current"] and cfg["endpoint_mode"] in ("images", "responses"):
        preferred = (cfg["endpoint_mode"],) + tuple(
            mode for mode in preferred if mode != cfg["endpoint_mode"]
        )

    for endpoint_mode in preferred:
        available, detail = probe_endpoint(cfg, endpoint_mode, timeout)
        results.append(f"{endpoint_mode}={detail}")
        if available:
            save_endpoint_mode(endpoint_mode, cfg["endpoint_fingerprint"])
            return endpoint_mode, results

    fail("No usable image endpoint found (" + ", ".join(results) + ")")


def encode_image_data_url(image_path):
    path = Path(image_path).expanduser()
    if not path.is_file():
        fail(f"Reference image not found: {path}")
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{raw}"


def image_generation_tool(args, output_format):
    tool = {"type": "image_generation"}
    if args.size != "auto":
        tool["size"] = args.size
    if args.quality != "auto":
        tool["quality"] = args.quality
    tool["output_format"] = output_format
    if output_format in {"jpeg", "webp"}:
        tool["output_compression"] = args.compression
    return tool


def generate_with_responses(cfg, args, timeout, output_format, available_models=None):
    content = [{"type": "input_text", "text": args.prompt}]
    if args.image:
        content.append({"type": "input_image", "image_url": encode_image_data_url(args.image)})

    candidates = model_candidates(cfg, "responses", args.model, available_models)
    for model in candidates:
        responses_payload = {
            "model": model,
            "input": [{"role": "user", "content": content}],
            "tools": [image_generation_tool(args, output_format)],
        }
        resp = requests.post(
            cfg["responses_base_url"],
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json=responses_payload,
            timeout=timeout,
        )
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except json.JSONDecodeError:
                fail(f"Non-JSON response: {resp.text[:500]}")
            raw = extract_image_bytes_from_responses(payload)
            save_resolved_model(cfg, "responses", model)
            return raw, model
        if is_model_rejection(resp):
            continue
        fail(f"HTTP {resp.status_code}: {error_text(resp)}")

    fail(
        "No configured image model was accepted by "
        f"{cfg['responses_base_url']}. Tried: {', '.join(candidates)}. "
        "Ask the provider for the exact image-generation model ID."
    )


def generate_with_images(cfg, args, timeout, output_format, available_models=None):
    candidates = model_candidates(cfg, "images", args.model, available_models)
    for model in candidates:
        payload = {
            "model": model,
            "prompt": args.prompt,
            "n": 1,
            "quality": args.quality,
            "output_format": output_format,
        }
        if args.size != "auto":
            payload["size"] = args.size
        if output_format in {"jpeg", "webp"}:
            payload["output_compression"] = args.compression

        if args.image:
            image_path = Path(args.image).expanduser()
            if not image_path.is_file():
                fail(f"Reference image not found: {image_path}")
            mime_type = mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
            with image_path.open("rb") as image_file:
                resp = requests.post(
                    f"{cfg['base_url']}/images/edits",
                    headers={"Authorization": f"Bearer {cfg['api_key']}"},
                    data={key: str(value) for key, value in payload.items()},
                    files={"image": (image_path.name, image_file, mime_type)},
                    timeout=timeout,
                )
            used_mode = "edit"
        else:
            resp = requests.post(
                f"{cfg['base_url']}/images/generations",
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            used_mode = "generate"

        if resp.status_code == 200:
            try:
                response_payload = resp.json()
            except json.JSONDecodeError:
                fail(f"Non-JSON response: {resp.text[:500]}")
            raw = extract_image_bytes(response_payload, cfg["api_key"], timeout)
            save_resolved_model(cfg, "images", model)
            return raw, model, used_mode
        if is_model_rejection(resp):
            continue
        fail(f"HTTP {resp.status_code}: {error_text(resp)}")

    fail(
        "No configured image model was accepted by "
        f"{cfg['base_url']}/images. Tried: {', '.join(candidates)}. "
        "Ask the provider for the exact image-generation model ID."
    )


def infer_format(out_path, fmt):
    if fmt:
        return fmt
    suffix = Path(out_path).suffix.lower().lstrip(".")
    if suffix in {"png", "jpeg", "jpg", "webp"}:
        return "jpeg" if suffix == "jpg" else suffix
    return "png"


def default_output_path(fmt):
    output_format = fmt or "png"
    output_dir = Path.cwd() / "generated"
    filename = f"image-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{output_format}"
    return output_dir / filename


def command_config(args):
    api_key = args.key or getpass.getpass("API key: ").strip()
    if not api_key:
        fail("API key is required")
    save_config(args.base, api_key, args.model, args.endpoint_mode)


def command_test(args):
    cfg = load_config()
    print(f"Config: {CONFIG_PATH}")
    print(f"Base URL: {cfg['base_url']}")
    print(f"Responses URL: {cfg['responses_base_url']}")
    print(f"API Key: {mask_key(cfg['api_key'])}")
    endpoint_mode, results = select_endpoint_mode(cfg, args.timeout)
    print(f"Selected endpoint mode: {endpoint_mode}")
    print(f"Safe probes: {', '.join(results)}")


def command_generate(args):
    cfg = load_config()
    out_path = Path(args.out).expanduser() if args.out else default_output_path(args.format)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    output_format = infer_format(str(out_path), args.format)
    endpoint_mode = cfg["endpoint_mode"] if cfg["endpoint_mode_is_current"] else "auto"
    if endpoint_mode == "auto":
        endpoint_mode, _ = select_endpoint_mode(cfg, args.timeout)

    available_models = set()
    if not cfg.get("model_cache_is_current"):
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


def build_parser():
    parser = argparse.ArgumentParser(description="OpenAI-compatible image gateway helper")
    sub = parser.add_subparsers(dest="command", required=True)

    config_parser = sub.add_parser("config", help="Save base URL, API key, and model preferences")
    config_parser.add_argument("--base", required=True, help="Gateway base URL, with or without /v1")
    config_parser.add_argument("--key", help="API key; omit to enter it without echoing")
    config_parser.add_argument("--model", default="auto", help="Preferred model or auto")
    config_parser.add_argument(
        "--endpoint-mode",
        choices=ENDPOINT_MODES,
        default="auto",
        help="Use images or responses directly, or auto-detect once during generation.",
    )
    config_parser.set_defaults(func=command_config)

    test_parser = sub.add_parser("test", help="Safely detect and save the usable image endpoint")
    test_parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    test_parser.set_defaults(func=command_test)

    gen_parser = sub.add_parser("generate", help="Generate an image to a target path")
    gen_parser.add_argument("--prompt", required=True, help="Prompt text")
    gen_parser.add_argument("--out", help="Target file path; defaults to ./generated/image-YYYYMMDD-HHMMSS.<format>")
    gen_parser.add_argument("--image", help="Reference image path for edit mode")
    gen_parser.add_argument("--size", default="1024x1024", help="Image size or auto")
    gen_parser.add_argument("--quality", default="auto", choices=["auto", "low", "medium", "high"])
    gen_parser.add_argument("--format", choices=["png", "jpeg", "webp"], help="Output format")
    gen_parser.add_argument("--compression", type=int, default=100, help="Compression for jpeg/webp")
    gen_parser.add_argument("--model", help="Override model")
    gen_parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds")
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
