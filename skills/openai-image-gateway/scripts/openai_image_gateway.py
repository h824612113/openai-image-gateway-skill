#!/usr/bin/env python3
import argparse
import base64
import json
import mimetypes
import os
import sys
from datetime import datetime
from pathlib import Path

import requests


SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "local_config.json"
DEFAULT_MODEL = "gpt-image-2"


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
    model = (data.get("model", "") or DEFAULT_MODEL).strip()
    responses_model = (data.get("responses_model", "") or "").strip()
    if not base_url or not api_key:
        fail(f"Config incomplete: {CONFIG_PATH}")
    return {
        "raw_base_url": raw_base_url,
        "base_url": base_url,
        "responses_base_url": responses_endpoint(raw_base_url),
        "api_key": api_key,
        "model": model,
        "responses_model": responses_model,
    }


def save_config(base_url, api_key, model):
    payload = {
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model": (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
    }
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
    print(f"Saved config: {CONFIG_PATH}")
    print(f"Images Base URL: {normalize_base_url(payload['base_url'])}")
    print(f"Responses URL: {responses_endpoint(payload['base_url'])}")
    print(f"API Key: {mask_key(payload['api_key'])}")
    print(f"Model: {payload['model']}")


def effective_responses_model(cfg, override_model=None):
    if override_model:
        return override_model
    if cfg.get("responses_model"):
        return cfg["responses_model"]
    return cfg["model"]


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


def request_models(base_url, api_key, timeout):
    resp = requests.get(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        return resp
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        fail(f"Non-JSON response: {resp.text[:500]}")
    data = payload.get("data")
    if not isinstance(data, list):
        fail(f"Unexpected models payload: {payload}")
    return data


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


def should_fallback_to_responses(resp):
    if resp.status_code != 404:
        return False
    body = resp.text or ""
    markers = ("Not Found", "Page not found", "接口不存在")
    return any(marker in body for marker in markers)


def encode_image_data_url(image_path):
    path = Path(image_path).expanduser()
    if not path.is_file():
        fail(f"Reference image not found: {path}")
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{raw}"


def generate_with_responses(cfg, args, timeout):
    model = effective_responses_model(cfg, args.model)
    content = [{"type": "input_text", "text": args.prompt}]
    if args.image:
        content.append({"type": "input_image", "image_url": encode_image_data_url(args.image)})
    responses_payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "tools": [{"type": "image_generation"}],
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
    if resp.status_code != 200:
        fail(f"HTTP {resp.status_code}: {error_text(resp)}")
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        fail(f"Non-JSON response: {resp.text[:500]}")
    return extract_image_bytes_from_responses(payload), model


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
    save_config(args.base, args.key, args.model)


def command_test(args):
    cfg = load_config()
    print(f"Config: {CONFIG_PATH}")
    print(f"Base URL: {cfg['base_url']}")
    print(f"Responses URL: {cfg['responses_base_url']}")
    print(f"API Key: {mask_key(cfg['api_key'])}")

    models = request_models(cfg["base_url"], cfg["api_key"], args.timeout)
    if isinstance(models, requests.Response):
        probe = requests.post(
            cfg["responses_base_url"],
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": effective_responses_model(cfg),
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
                "tools": [{"type": "image_generation"}],
            },
            timeout=args.timeout,
        )
        if probe.status_code == 200:
            print("Responses gateway OK")
            return
        probe_text = probe.text or ""
        if probe.status_code == 400 and ("模型" in probe_text or "model" in probe_text.lower()):
            print("Responses gateway reachable, but the configured default model is not accepted there.")
            print("The standard v1 image path remains the default. Responses fallback may need --model override.")
            return
        fail(f"HTTP {probe.status_code}: {error_text(probe)}")

    ids = [m.get("id", "") for m in models if isinstance(m, dict)]
    print("Models:")
    for model_id in ids:
        print(f"- {model_id}")


def command_generate(args):
    cfg = load_config()
    out_path = Path(args.out).expanduser() if args.out else default_output_path(args.format)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    output_format = infer_format(str(out_path), args.format)
    model = args.model or cfg["model"]
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

    used_mode = "generate"
    used_model = model
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
                timeout=args.timeout,
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
            timeout=args.timeout,
        )

    if resp.status_code == 200:
        try:
            response_payload = resp.json()
        except json.JSONDecodeError:
            fail(f"Non-JSON response: {resp.text[:500]}")
        raw = extract_image_bytes(response_payload, cfg["api_key"], args.timeout)
    elif should_fallback_to_responses(resp):
        raw, used_model = generate_with_responses(cfg, args, args.timeout)
        used_mode = "responses-edit" if args.image else "responses"
    else:
        fail(f"HTTP {resp.status_code}: {error_text(resp)}")

    out_path.write_bytes(raw)
    print(f"Saved image: {out_path}")
    print(f"Bytes: {len(raw)}")
    print(f"Model: {used_model}")
    print(f"Mode: {used_mode}")


def build_parser():
    parser = argparse.ArgumentParser(description="OpenAI-compatible image gateway helper")
    sub = parser.add_subparsers(dest="command", required=True)

    config_parser = sub.add_parser("config", help="Save base URL, API key, and default model")
    config_parser.add_argument("--base", required=True, help="Gateway base URL, with or without /v1")
    config_parser.add_argument("--key", required=True, help="API key")
    config_parser.add_argument("--model", default=DEFAULT_MODEL, help="Default model")
    config_parser.set_defaults(func=command_config)

    test_parser = sub.add_parser("test", help="Test connectivity via /models")
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
    args.func(args)


if __name__ == "__main__":
    main()
