# openai-image-gateway

Standalone Codex skill for generating images through an OpenAI-compatible gateway with one-time local configuration.

## Marketplace submission type

Submit this repository to Codex Marketplace as:

- `SKILL` for the whole repo, or
- `owner/repo/skills/openai-image-gateway` for the single skill path

## Install

Example:

```bash
npx codex-marketplace add owner/repo/skills/openai-image-gateway --skill
```

## What it does

- Save `base_url`, `api_key`, and default `model` once
- Test connectivity with `/v1/models`
- Generate an image to a local file path

## Files

```text
skills/
  openai-image-gateway/
    SKILL.md
    local_config.example.json
    scripts/openai_image_gateway.py
```

## User setup after install

Users must provide their own gateway URL and API key:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py config \
  --base 'YOUR_URL' \
  --key 'YOUR_KEY' \
  --model gpt-image-2
```

Then test:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py test
```

Generate:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --prompt "cyberpunk city at night" \
  --out ~/Downloads/city.png
```

## Requirements

- Python 3
- `requests`

## Security

- This repository does not contain any real gateway URL or API key
- Users must create their own local config after installation
