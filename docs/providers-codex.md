# Codex

The `codex` provider talks to the ChatGPT Codex backend over WebSocket
Responses. It supports text, image input, function tools, guarded
`previous_response_id` continuation, and provider-native image generation.

Like the [ccflare route](providers-ccflare.md), this is a [subscription-backed
route](providers.md#subscription-backed-routes): the bot runs on a personal
ChatGPT subscription instead of a metered API key, which fits an instance you
run for yourself or a small trusted server. Its model entries carry no `pricing`
and its turns contribute nothing to `/usage`. See [providers.md](providers.md)
for the general routing model.

## Configuration

```yaml
providers:
  codex:
    type: codex
    reasoning_effort: low

models:
  codex-chat:
    provider: codex
    model: <codex-model-id>
    context_window: 200000
    capabilities: [text, tool_calling, image_input]
```

`api_key_env` stays blank: Codex authenticates from a token file, not an
environment key. `reasoning_effort` on the profile overrides
`CODEX_REASONING_EFFORT` for the models routed through it, which is how one
Codex backend can serve two model entries at different depths.

The remaining Codex transport settings live in `.env` rather than
`models.yaml`, because they are operational rather than routing decisions:
`CODEX_TOKEN_FILE`, `CODEX_REASONING_EFFORT`, `CODEX_IMAGE_QUALITY`,
`CODEX_IMAGE_FORMAT`, `CODEX_WS_IDLE_TIMEOUT`, `CODEX_WS_READ_TIMEOUT`, and
`CODEX_VERBOSE`. `CODEX_MODEL` is the transport's fallback when a caller
supplies no model at all; bot chat always uses the YAML model entry.

## Authentication

```bash
uv run python scripts/codex_auth.py --token-file secrets/codex-auth.json
```

The helper runs the Codex OAuth device flow and writes the token file
atomically with owner-only permissions.

Startup validates Codex auth when a reachable enabled role needs Codex. A
revoked token fails fast, with a `uv run python scripts/codex_auth.py` hint in
the message. Transient network errors during that check are tolerated and
retried on first use: a flaky network at boot should not be indistinguishable
from a dead credential.

Refresh is careful about concurrency. Before refreshing, the runtime reloads a
same-account token that another process may have written, so two Kimi
instances sharing a token file do not fight over it. A WebSocket 401 forces that
guarded reload-and-refresh once and retries the handshake, without ever exposing
the bearer token in logs.

## The originator header

The WebSocket handshake sends `originator: codex_cli_rs`
(`codex/transport.py:CODEX_ORIGINATOR`). This is not cosmetic. The backend
resolves bare model ids against a per-client bucket, and without a recognized
originator, newer models (`gpt-5.6-sol` or `gpt-5.6-terra`, for instance) fail
with a misleading `Model not found <model>-free-1p-...` even though the account
is perfectly entitled to serve them.

If you see that error for a model you know you have, check the originator before
you check anything else.

## Reasoning and continuation

When `reasoning_after_tools` changes effort between Codex iterations, the
WebSocket request signature changes with it. Continuation reuse is skipped for
that call and the transport sends the full input instead, which keeps the tool
result and the prior provider-native output intact under the new effort. The
alternative, reusing a continuation across an effort change, would ask the
backend to continue a response that was produced under different settings.

## Provider-native output

Codex output items are preserved in stored assistant messages so later turns can
replay provider-native items, including `function_call` and
`image_generation_call`. Image generation results normalize to `GeneratedAsset`
and are attached through the same generated-file path every other provider-native
image output uses.
