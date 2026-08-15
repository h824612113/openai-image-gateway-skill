# Errors

## [ERR-20260815-001] openai-image-gateway-generation

**Logged**: 2026-08-15T00:00:00+08:00
**Priority**: high
**Status**: pending
**Area**: infra

### Summary

The verified Responses client received an immediate upstream 503 during a real image-generation smoke test.

### Error

```text
HTTP 503: Service temporarily unavailable
```

### Context

- The default `test` command completed a read-only `GET /v1/models` health check with HTTP 200.
- The request used the configured Responses endpoint, a complete non-explicit prompt, 1024x1024 PNG output, medium quality, and background polling.
- The client correctly made no retry or endpoint fallback after the ambiguous 503.

### Suggested Fix

Restore upstream generation capacity, then submit a new user-authorized generation request. Keep the client no-retry policy for 5xx responses.

### Metadata

- Reproducible: yes
- Related Files: `skills/openai-image-gateway/scripts/openai_image_gateway.py`

---
