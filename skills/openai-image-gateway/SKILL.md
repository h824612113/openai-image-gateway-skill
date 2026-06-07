---
name: openai-image-gateway
description: Use an OpenAI-compatible image gateway for text-to-image generation with one-time local config. Trigger when the user asks to generate an image through a configured gateway, test gateway connectivity, update gateway settings, or save generated output to a specified local path.
---

# OpenAI Image Gateway

Use this skill when the user wants a reusable local image-generation workflow backed by an **OpenAI-compatible** gateway.

## What this skill does

- Stores `base_url`, `api_key`, and default `model` once in a local config file
- Tests gateway connectivity by querying `/v1/models`
- Generates an image from text and saves it to a user-specified local path

## Common Chinese invocations

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
  --key 'YOUR_KEY' \
  --model gpt-image-2
```

Connectivity test:

```bash
python3 /Users/hanhao/.codex/skills/openai-image-gateway/scripts/openai_image_gateway.py test
```

Generate to a target path:

```bash
python3 /Users/hanhao/.codex/skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --prompt "一只西瓜在跳舞" \
  --out /Users/hanhao/Downloads/output_images/watermelon.png
```

Optional generation overrides:

- `--size 1024x1024`
- `--quality low|medium|high|auto`
- `--format png|jpeg|webp`
- `--compression 0-100`
- `--model MODEL_NAME`
- `--timeout SECONDS`

## Workflow

1. If `local_config.json` is missing or incomplete, run `config`.
2. Run `test` when the user asks to verify the gateway.
3. Run `generate` when the user gives a prompt and target path.
4. If generation fails, report the upstream error or timeout directly.

## Notes

- The script normalizes `base_url` so both `https://host` and `https://host/v1` work.
- The script supports both `b64_json` responses and URL-based image responses.
