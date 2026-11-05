# OpenAI Image Gateway Skill

Standalone Codex skill for generating images through an OpenAI-compatible gateway with one-time local configuration.

## What this repository contains

This repository publishes one standalone skill:

- `skills/openai-image-gateway`

The skill lets a user:

- save `base_url`, `api_key`, and default `model` once
- resolve and cache a usable image model when the provider exposes model aliases or fallback versions
- diagnose endpoint reachability and preserve an explicit endpoint choice
- generate an image from text and save it to a local file path
- generate a new image from a reference image and prompt
- generate an animated GIF from text or a local reference on supported GPT Image 2.6 gateways

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

## Gateway request shapes

### Animated GIFs

Install the GIF validation dependency once:

```bash
python3 -m pip install Pillow
```

GIF behavior follows the selected gateway contract:

| GIF profile | Frame count | Default frames | Reference-image GIF |
| --- | --- | --- | --- |
| `cx` | 2–16 | 6 | Local file uploaded as `image[]` |
| `generic` | 2–64 | 9 | Not enabled |
| `auto` (configuration default) | Selected by host | Selected by host | Uses `cx` for the exact documented CX host, otherwise `generic` |

The documented host is `api.www-cx-api.cn.mt`. A configured `gif_profile` or
per-generation `--gif-profile` overrides automatic host selection. To use the
same contract through a different domain, choose `--gif-profile cx` explicitly.
Existing configurations without a `gif_profile` use `auto`; saving config
without this option preserves any previously configured profile.

From the repository root, generate a text-to-GIF animation:

```bash
python3 skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --gif-profile cx \
  --model gpt-image-2.6-flare \
  --prompt "A red ball bounces above a gray line on a white background. Keep the camera still and loop smoothly." \
  --format gif \
  --frame-count 6 \
  --frame-duration-ms 200 \
  --size auto \
  --out ./generated/bouncing-ball.gif
```

Generate a GIF from a local reference image:

```bash
python3 skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --gif-profile cx \
  --model gpt-image-2.6-flare \
  --image ./reference.png \
  --prompt "Animate the blue ball bouncing up and down. Keep the reference background and colors, with a smooth loop." \
  --format gif \
  --frame-count 4 \
  --frame-duration-ms 240 \
  --out ./generated/reference-animation.gif
```

The first example requests 1.2 seconds (6 × 200 ms); the second requests
0.96 seconds (4 × 240 ms). The CX profile sends text requests as JSON to
`<base>/images/generations`, and reference requests as multipart form data to
`<base>/images/edits`, using the literal `image[]` field. Its defaults include
`size=auto`, `stream=false`, and `response_format=url`. Use a key whose group
has Image 2.6 GIF access; the model list alone does not prove GIF permission.

- GIF uses Images only. Explicit `responses` mode, `--stream`, `--compression`,
  and `--image-size` are rejected before generation. In `auto` endpoint mode,
  GIF goes directly to Images without a generation probe or config change.
- `--image` accepts one local reference file for CX GIF. Download URL references
  to a local file yourself before use; no undocumented URL-edit request is
  inferred. Other profiles retain the text-only GIF restriction.
- `.gif` output paths infer GIF format. `--frame-count` and
  `--frame-duration-ms` are GIF-only; the frame delay range is 20–2000 ms and
  defaults to 200. `generic` retains its previous 9-frame default and 64-frame
  client limit; the gateway may enforce stricter access or pixel limits.
- GIF defaults to automatic frame size. Explicit dimensions must be positive
  multiples of 16. The provider may choose a different final canvas; use the
  actual output dimensions when assessing the result.
- `--response-format url|b64_json` is an optional Images-only request parameter.
  It controls the server's return transport, while `--format` controls the
  actual image format. CX GIF defaults to URL; generic/static requests omit
  this field unless selected. Base64 transport must also be supported by the
  provider. Both returned URLs and Base64 are downloaded/decoded and saved.
- Explicit model names and provider aliases stay pinned. Automatic GIF model
  selection only considers GPT Image 2.6 Sunburst/Flare. Fallback happens only
  after definitive model rejection; ambiguous failures never switch GIF models.
- GIF validation checks block boundaries, termination, multiple frames, and
  decodability before caching success or replacing output. Returned bytes are
  preserved without re-encoding. Missing Pillow fails before submission.
- `--background` retains the existing optional gateway async route and
  same-body/key retry behavior. The supplied CX documentation establishes
  synchronous non-streaming generation; async GIF support is provider-dependent
  and has only offline verification here.

CX behavior follows the gateway request examples supplied for this project;
GIF edits are not assumed to be supported by every OpenAI-compatible provider.
The generic text-to-GIF shape follows the
[OpenAI GIF guide](https://developers.openai.com/api/docs/guides/image-generation).

### Static images and async gateway routes

When `--endpoint-mode images` is configured, the CLI uses Image2's synchronous
route by default (`POST <base>/images/generations` or
`POST <base>/images/edits`). The default request matches the working Image2
gateway contract: one image (`n=1`), `quality=low`, and PNG output. A `200`
response is decoded from `data[0].url` or a compatible Base64 field.

Pass `--background` to opt into Image2's async route (`?async=true`). It accepts
the documented `202` task response, waits at least five seconds between polls,
and reads the completed image from `result.data[0].url`. `429` and polling-time
`5xx` responses retry the same task; the client never submits a second
generation request for those errors.

After a task is marked complete, the signed result URL is fetched without the
gateway Bearer token. Temporary `429`/`5xx` or transport failures are retried
against that same URL at most twice. A failed CDN download is reported as a
post-completion download error and never re-submits the billable image request.

- Text-to-image sends JSON with `model`, `prompt`, `n=1`, `size`,
  `quality=low`, and `output_format=png` by default. `--quality`, `--format`,
  `--compression`, and ratio `--image-size` override the corresponding fields.
- Image-to-image accepts either a local file (`multipart/form-data`, `image`)
  or a public HTTP(S) URL (`images`). URL arrays are retried in the
  `{"image_url": "..."}` object shape when a compatible gateway explicitly
  asks for that form.
- Synchronous static Images generation continues through configured image-model
  candidates after HTTP or transport failures; async submissions stay bound
  to one idempotency key and task.
- `--format` controls both the requested `output_format` and the local file
  extension. `--quality` and `--compression` are forwarded when supported by
  the selected Images model.

`--background` enables the async route for both Images and Responses endpoints.
Unless the CX GIF profile or `--response-format` selects a transport, Images
requests omit `response_format`; the client accepts either URL
or Base64 responses, matching the gateway's native response shape.
For an async submission that ends in a connection reset or transient `429`/
`5xx`, the client performs one replay with the same `Idempotency-Key`. If the
outcome is still ambiguous, it stops and prints a replay hint; pass
`--idempotency-key` with the printed key and keep the request body identical.

Long generations on the responses endpoint:

```bash
python3 ~/.agents/skills/openai-image-gateway/scripts/openai_image_gateway.py generate \
  --prompt "a highly detailed illustrated map" \
  --out ~/Downloads/map.png \
  --background
```

`--background` polls until the response completes instead of holding one long
request open. `--stream` follows progress events and saves only the final image;
partial previews are discarded rather than written to disk. Streaming remains
Responses-only.

## Natural language usage

After install and config, users can invoke it in Codex with prompts like:

- `Use openai-image-gateway to generate a cyberpunk city and save it to /path/to/city.png`
- `Use /path/to/reference.png as a reference image, turn it into a clean product render, and save to /path/to/product.png`
- `用 /path/to/reference.png 做参考图，生成白底商品渲染图，保存到 /path/to/product.png`

- `用 openai-image-gateway 生图，保存到 /path/to/file.png`
- `用图片网关生图，输出到 /path/to/file.png`
- `用 openai-image-gateway 测一下连接`

If all candidates are rejected, the skill reports the endpoint and attempted model IDs and asks the provider for the exact image-generation model. An initial transient `429/502/503/504` or network reset gets one idempotent replay; a second ambiguous result is reported with a request fingerprint instead of being retried indefinitely. After an async task ID exists, transient poll errors continue against that same task.

## Requirements

- Python 3
- `requests`
- `Pillow` for GIF validation and the GIF regression tests (optional for static images)

Run the offline regression tests with `python3 -m unittest discover -v`.

## Endpoint Selection

`endpoint_mode` stores operator intent. A real successful generation records `last_successful_mode` with a SHA-256 fingerprint of the configured base URL and API key. Auto mode prefers that proven endpoint; safe probes remain diagnostic and never masquerade as successful generation. Async Images submissions use one same-body idempotent replay for transient failures, then stop without endpoint or model fallback.

## Security

- This repository does not contain any real gateway URL or API key
- Users must create their own local config after installation
- Local secrets should stay in `local_config.json`, which should not be committed
