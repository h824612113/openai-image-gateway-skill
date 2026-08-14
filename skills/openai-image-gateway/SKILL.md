---
name: openai-image-gateway
description: Use an OpenAI-compatible image gateway for text-to-image or reference-image generation with one-time local config. Trigger when the user asks to generate an image through a configured gateway, use a reference image, test gateway connectivity, update gateway settings, or save generated output to a specified local path.
---

# OpenAI Image Gateway

Use this skill when the user wants a reusable local image-generation workflow backed by an **OpenAI-compatible** gateway.

## What this skill does

- Stores `base_url`, `api_key`, and default `model` once in a local config file
- Keeps Images and Responses model preferences separate
- Resolves a usable model from the provider model list or conservative image-model candidates
- Diagnoses endpoint reachability without changing explicit endpoint choices
- Records `last_successful_mode` only after a real image is returned
- Generates an image from text and saves it to a user-specified local path
- Generates a new image from a reference image and prompt

## Common Chinese invocations

- `用 openai-image-gateway 生成图片，输出到 /path/to/file.png`
- `用 /path/to/reference.png 做参考图，生成白底商品渲染图，保存到 /path/to/product.png`
- `编辑这张图 /path/to/reference.png，改成赛博朋克风格，保存到 /path/to/output.png`

- `用 openai-image-gateway 生图，输出到 /path/to/file.png`
- `用图片网关生图，保存到 /path/to/file.png`
- `生成图片并输出到 /path/to/file.png`
- `用 openai-image-gateway 测一下连接`
- `用 openai-image-gateway 重新配置 url 和 key`

## Files

- Config: `local_config.json`
- Example config: `local_config.example.json`
- Script: `scripts/openai_image_gateway.py`

## Rules

- Do not print the full API key in chat.
- Keep real keys only in `local_config.json`.
- Save outputs only to paths the user asked for or clearly approved.

## Commands

First-time config:

```bash
python3 /Users/hanhao/.codex/skills/openai-image-gateway/scripts/openai_image_gateway.py config \
  --base https://example.com/ \
  --model gpt-image-2 \
  --responses-model gpt-5.4 \
  --endpoint-mode images
```

Omit `--key` to enter it through a hidden terminal prompt. Use `endpoint_mode: images|responses` for an operator override. Use `auto` only when automatic diagnostic selection is wanted.

Connectivity test:

```bash
python3 /Users/hanhao/.codex/skills/openai-image-gateway/scripts/openai_image_gateway.py test
```

`test` is read-only by default. It reports reachability and never changes `endpoint_mode`. To convert `auto` into the first reachable diagnostic candidate, opt in explicitly:

```bash
python3 /Users/hanhao/.codex/skills/openai-image-gateway/scripts/openai_image_gateway.py test --select
```

`--select` never overrides an explicit `images` or `responses` setting. A diagnostic selection is not proof that image generation works.

Generate to a target path:

```bash
python3 /Users/hanhao/.codex/skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --prompt "一只西瓜在跳舞" \
  --out /Users/hanhao/Downloads/output_images/watermelon.png
```

Optional generation overrides:

- `--image /path/to/reference.png`
- `--size 1024x1024`
- `--quality low|medium|high|auto`
- `--format png|jpeg|webp`
- `--compression 0-100`
- `--model MODEL_NAME`
- `--timeout SECONDS`
- `--background` (responses endpoint only; poll a long generation instead of holding the request open)
- `--stream` (responses endpoint only; stream progress and save only the final image)

## Workflow

1. If `local_config.json` is missing or incomplete, run `config` and choose `images`, `responses`, or `auto` deliberately.
2. Run `test` for read-only diagnostics. Treat HTTP 400/422 as endpoint reachability only, not image-generation capability.
3. Run `generate` when the user gives a prompt and target path. Explicit endpoint modes are always honored.
4. In `auto`, prefer a fingerprint-matched `last_successful_mode`; otherwise probe once and use one reachable candidate without caching it as successful.
5. After real image bytes are extracted, cache `last_successful_mode` and the accepted model.
6. Add `--image /path/to/reference.png` when the user wants to use a reference image.
7. If a provider explicitly rejects a model (`model_not_found`, `unsupported_model`, or an equivalent 400/404 response), try the next candidate. Do not retry or switch endpoints after timeouts, rate limits, 5xx responses, or any ambiguous response because generation may already have started.
8. If no candidate is accepted, report the endpoint and attempted models.

## Notes

- The script normalizes `base_url` so both `https://host` and `https://host/v1` work.
- The script supports both `b64_json` responses and URL-based image responses.
- `test` sends an empty request without a prompt, model, or image-generation tool, so it cannot initiate image generation.
- `test` does not write configuration unless `--select` is present, and explicit endpoint modes are immutable to testing.
- HTTP 400/422 from a safe probe means the route exists; it never means the route can generate images.
- `endpoint_mode` stores operator intent. `last_successful_mode` is runtime-owned evidence written only after a real generation succeeds.
- A generation call uses one endpoint only and never falls back after HTTP 502/503 or another ambiguous failure.
- Model discovery is read-only when `/models` is available. Model fallback only continues after a definitive model rejection; it never retries uncertain generation states.
- The first accepted model is cached with a configuration fingerprint and reused until the base URL, API key, or endpoint mode changes.
- `responses_model` is tried before the Images `model` when the Responses endpoint is selected.
- Success caches are bound to a SHA-256 fingerprint of the configured base URL and API key.
- `--background` and `--stream` work only on the responses endpoint; on the images endpoint the script rejects them before sending any request.
- Partial preview images from `--stream` are never written to disk. If a stream ends before the final image, the script fails instead of saving an unfinished picture.
