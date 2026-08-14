# OpenAI Image Gateway Skill

Standalone Codex skill for generating images through an OpenAI-compatible gateway with one-time local configuration.

## What this repository contains

This repository publishes one standalone skill:

- `skills/openai-image-gateway`

The skill lets a user:

- save `base_url`, `api_key`, and default `model` once
- resolve and cache a usable image model when the provider exposes model aliases or fallback versions
- safely select and cache a usable image endpoint before generation
- generate an image from text and save it to a local file path
- generate a new image from a reference image and prompt

## Install

```bash
npx codex-marketplace add h824612113/openai-image-gateway-skill/skills/openai-image-gateway --skill
```

## Repository layout

```text
skills/
  openai-image-gateway/
    SKILL.md
    local_config.example.json
    scripts/openai_image_gateway.py
```

## User setup after install

Users must provide their own gateway URL and API key. No real credentials are included in this repository.

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py config \
  --base 'YOUR_URL' \
  --model gpt-image-2 \
  --responses-model gpt-5.4 \
  --endpoint-mode images
```

Omit `--key` to enter the key through a hidden terminal prompt. Explicit `images` or `responses` modes are operator overrides. Use `auto` only when diagnostic endpoint selection is desired.

Test connectivity:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py test
```

This command is read-only. It reports endpoint reachability, and HTTP 400/422 does not prove image generation works. Use `test --select` only to opt into saving a reachable candidate while `endpoint_mode` is `auto`; it never overrides an explicit mode.

Generate an image:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --prompt "cyberpunk city at night" \
  --out ~/Downloads/city.png
```

Generate from a reference image:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --image ~/Pictures/reference.png \
  --prompt "turn this into a clean product render on a white background" \
  --out ~/Downloads/product.png
```

Long generations on the responses endpoint:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --prompt "a highly detailed illustrated map" \
  --out ~/Downloads/map.png \
  --background
```

`--background` polls until the response completes instead of holding one long request open. `--stream` follows progress events and saves only the final image; partial previews are discarded rather than written to disk. Both flags require the responses endpoint and are rejected before any request when the images endpoint is selected.

## Natural language usage

After install and config, users can invoke it in Codex with prompts like:

- `Use openai-image-gateway to generate a cyberpunk city and save it to /path/to/city.png`
- `Use /path/to/reference.png as a reference image, turn it into a clean product render, and save to /path/to/product.png`
- `用 /path/to/reference.png 做参考图，生成白底商品渲染图，保存到 /path/to/product.png`

- `用 openai-image-gateway 生图，保存到 /path/to/file.png`
- `用图片网关生图，输出到 /path/to/file.png`
- `用 openai-image-gateway 测一下连接`

If all candidates are rejected, the skill reports the endpoint and attempted model IDs and asks the provider for the exact image-generation model. It never retries after a timeout or server error because the previous request may already have started generation.

## Requirements

- Python 3
- `requests`

## Endpoint Selection

`endpoint_mode` stores operator intent. A real successful generation records `last_successful_mode` with a SHA-256 fingerprint of the configured base URL and API key. Auto mode prefers that proven endpoint; safe probes remain diagnostic and never masquerade as successful generation. A 5xx response never triggers an automatic second generation request or endpoint fallback.

## Security

- This repository does not contain any real gateway URL or API key
- Users must create their own local config after installation
- Local secrets should stay in `local_config.json`, which should not be committed
