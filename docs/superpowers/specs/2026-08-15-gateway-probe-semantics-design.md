# Gateway Probe Semantics Design

**Date:** 2026-08-15

## Goal

Prevent the default gateway health check from creating misleading `prompt is required` entries in the upstream image-generation failure log, without turning a health check into a billable image-generation request.

## Scope

Only the `test` command changes. `generate` continues to submit a required `prompt` to the selected image-generation endpoint exactly as it does today.

## Design

### Default health check

`test` will use a read-only `GET /v1/models` request to verify the configured base URL and API-key authentication. Its output will explicitly label the result as `read-only reachability`; it does not claim that an image-generation route has been verified.

### Explicit generation-route diagnostic

`test --probe-generation-route` will retain the existing zero-prompt POST diagnostic for Images and Responses routes. These requests will include `X-Image-Gateway-Probe: 1`, and the CLI will label their result as `generation-route diagnostic`.

The header enables gateway-side logs and alerting to classify these deliberately invalid, no-generation requests as health checks rather than image-generation failures. If a third-party gateway does not support this classification, the endpoint may still log the request as a 400/422 diagnostic, but it will never create or charge for an image.

### Endpoint selection

`test --select` remains valid only for `endpoint_mode: auto`. It performs the explicit route diagnostic, because read-only model discovery cannot prove which image route is usable. An explicit `images` or `responses` choice remains immutable to testing.

## Error Handling

- A read-only `GET /models` 2xx response is a successful default health check.
- A 401/403 response proves the service is reachable but reports authentication failure.
- Route diagnostics continue to treat 200, 400, and 422 as reachable; they do not prove successful image generation.
- Real generation responses remain unchanged; 5xx responses do not cause automatic retries or endpoint fallback.

## Tests

- Default `test` performs only a GET model-list request and never POSTs to a generation endpoint.
- Explicit route probing POSTs an empty diagnostic request with `X-Image-Gateway-Probe: 1`.
- `test --select` uses route diagnostics only in auto endpoint mode.
- Existing generation tests continue to prove the Images payload contains `prompt`.

## Non-goals

- Do not submit a dummy prompt during health checks.
- Do not change image generation payloads, models, retries, or billing behavior.
- Do not assume an arbitrary third-party gateway will suppress diagnostics without honoring the probe header.
