# OpenAI Image Gateway Skill

Standalone Codex skill for generating images through an OpenAI-compatible gateway with one-time local configuration.

## What this repository contains

This repository publishes one standalone skill:

- `skills/openai-image-gateway`

The skill lets a user:

- save `base_url`, `api_key`, and default `model` once
- test connectivity through `/v1/models`
- generate an image from text and save it to a local file path

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
  --key 'YOUR_KEY' \
  --model gpt-image-2
```

Test connectivity:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py test
```

Generate an image:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --prompt "cyberpunk city at night" \
  --out ~/Downloads/city.png
```

## Natural language usage

After install and config, users can invoke it in Codex with prompts like:

- `用 openai-image-gateway 生图，保存到 /path/to/file.png`
- `用图片网关生图，输出到 /path/to/file.png`
- `用 openai-image-gateway 测一下连接`

## Requirements

- Python 3
- `requests`

## Security

- This repository does not contain any real gateway URL or API key
- Users must create their own local config after installation
- Local secrets should stay in `local_config.json`, which should not be committed
