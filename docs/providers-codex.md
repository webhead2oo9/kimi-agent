# Codex

The `codex` provider talks to the ChatGPT Codex backend over WebSocket
Responses. It supports text, image input, function tools, guarded
`previous_response_id` continuation, and provider-native image output for
explicit `ProviderRequest` callers. Normal Discord image creation uses the
separate [`generate_image` tool](image-generation.md), not text inference.

Like the [ccflare route](providers-ccflare.md), this is a [subscription-backed
route](providers.md#subscription-backed-routes): the bot runs on a personal
ChatGPT subscription instead of a metered API key, which fits an instance you
run for yourself or a small trusted server. Its model entries carry no `pricing`
and its turns contribute nothing to `/usage`. For the general routing model,
see [providers.md](providers.md).

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

`api_key_env` stays blank, because Codex authenticates from a token file rather
than an environment key. Setting `reasoning_effort` on the profile overrides
`CODEX_REASONING_EFFORT` for the models routed through it, which is how one
Codex backend can serve two model entries at different depths.

The remaining Codex transport settings live in `.env` rather than
`models.yaml`, since they are operational knobs rather than routing decisions:
`CODEX_TOKEN_FILE`, `CODEX_REASONING_EFFORT`, `CODEX_IMAGE_QUALITY`,
`CODEX_IMAGE_FORMAT`, `CODEX_WS_IDLE_TIMEOUT`, `CODEX_WS_READ_TIMEOUT`, and
`CODEX_VERBOSE`. `CODEX_MODEL` is only the transport's fallback when a caller
supplies no model at all; bot chat always uses the YAML model entry.

## Authentication

```bash
uv run python scripts/codex_auth.py --token-file secrets/codex-auth.json
```

The helper runs the Codex OAuth device flow and writes the token file
atomically with owner-only permissions.

Startup validates Codex auth whenever a reachable enabled model role needs
Codex. A revoked token then fails fast, and the message includes a
`uv run python scripts/codex_auth.py` hint so you know what to run. Transient
network errors during that check are tolerated and retried on first use,
because a flaky network at boot should not be indistinguishable from a dead
credential. The optional image tool validates OAuth on first use instead: a
stale image-only token leaves chat available and produces a concise
re-authentication error from the tool.

Refresh is careful about concurrency. Before refreshing, the runtime reloads a
same-account token that another process may have written, so two Kimi
instances sharing a token file don't fight over it. A WebSocket 401 forces that
guarded reload-and-refresh once and retries the handshake, without ever exposing
the bearer token in logs.

## The originator header

The WebSocket handshake sends `originator: codex_cli_rs`
(`codex/transport.py:CODEX_ORIGINATOR`). This is not cosmetic. The backend
resolves bare model ids against a per-client bucket. Without a recognized
originator, an entitled model can fail with a misleading
`Model not found <model>-free-1p-...` error.

So if you see that error for a model you know you have, check the originator
before you check anything else.

## Reasoning and continuation

When `reasoning_after_tools` changes the effort between Codex iterations, the
WebSocket request signature changes with it. Continuation reuse is skipped for
that call and the transport sends the full input instead, which keeps the tool
result and provider-native output intact under the selected effort. The
alternative, reusing a continuation across an effort change, would be asking
the backend to continue a response that was produced under different settings.

## Provider-native output

Codex output items are preserved in stored assistant messages so that later
turns can replay provider-native items, including `function_call` and
`image_generation_call`. Explicit provider-native image output still normalizes
to `GeneratedAsset` and uses the generated-asset attachment rail. Normal Discord
turns use `generate_image`, which calls the independent Images API backend and
saves reusable workspace output instead. See [Image generation](image-generation.md).
