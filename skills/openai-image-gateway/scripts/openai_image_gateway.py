#!/usr/bin/env python3
import argparse
import base64
import json
import os
import sys
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
    if raw.endswith("/v1"):
        return raw
    return f"{raw}/v1"


def mask_key(key):
    key = key or ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def load_config():
    if not CONFIG_PATH.exists():
        fail(f"Config not found: {CONFIG_PATH}")
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"Invalid config JSON: {exc}")

    base_url = normalize_base_url(data.get("base_url", ""))
    api_key = (data.get("api_key", "") or "").strip()
    model = (data.get("model", "") or DEFAULT_MODEL).strip()
    if not base_url or not api_key:
        fail(f"Config incomplete: {CONFIG_PATH}")
    return {"base_url": base_url, "api_key": api_key, "model": model}


def save_config(base_url, api_key, model):
    payload = {
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model": (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
    }
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
    print(f"Saved config: {CONFIG_PATH}")
    print(f"Base URL: {normalize_base_url(payload['base_url'])}")
    print(f"API Key: {mask_key(payload['api_key'])}")
    print(f"Model: {payload['model']}")


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
    return resp.text[:1000]


def request_models(base_url, api_key, timeout):
    resp = requests.get(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        fail(f"HTTP {resp.status_code}: {error_text(resp)}")
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


def infer_format(out_path, fmt):
    if fmt:
        return fmt
    suffix = Path(out_path).suffix.lower().lstrip(".")
    if suffix in {"png", "jpeg", "jpg", "webp"}:
        return "jpeg" if suffix == "jpg" else suffix
    return "png"


def command_config(args):
    save_config(args.base, args.key, args.model)


def command_test(args):
    cfg = load_config()
    models = request_models(cfg["base_url"], cfg["api_key"], args.timeout)
    ids = [m.get("id", "") for m in models if isinstance(m, dict)]
    print(f"Config: {CONFIG_PATH}")
    print(f"Base URL: {cfg['base_url']}")
    print(f"API Key: {mask_key(cfg['api_key'])}")
    print("Models:")
    for model_id in ids:
        print(f"- {model_id}")


def command_generate(args):
    cfg = load_config()
    out_path = Path(args.out).expanduser()
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

    resp = requests.post(
        f"{cfg['base_url']}/images/generations",
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=args.timeout,
    )
    if resp.status_code != 200:
        fail(f"HTTP {resp.status_code}: {error_text(resp)}")

    try:
        response_payload = resp.json()
    except json.JSONDecodeError:
        fail(f"Non-JSON response: {resp.text[:500]}")

    raw = extract_image_bytes(response_payload, cfg["api_key"], args.timeout)
    out_path.write_bytes(raw)
    print(f"Saved image: {out_path}")
    print(f"Bytes: {len(raw)}")
    print(f"Model: {model}")


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
    gen_parser.add_argument("--out", required=True, help="Target file path")
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
