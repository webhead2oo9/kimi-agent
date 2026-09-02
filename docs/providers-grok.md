# xAI Grok

Kimi supports two xAI model transports:

- `openai_compat` uses xAI's Chat Completions endpoint with `GROK_API_KEY`.
  This remains backward compatible and is appropriate for existing deployments.
- `xai` uses the Responses API at the fixed `https://api.x.ai/v1` origin and
  supports xAI OAuth, `GROK_API_KEY`, or explicit OAuth-first fallback.

Neither route is enabled by the presence of credentials. A model becomes
reachable only when the ignored deployment `config/models.yaml` declares it
and assigns or exposes that model through normal routing.

## Native Responses provider

Authenticate once with xAI's device flow:

```bash
.venv/bin/python scripts/xai_auth.py --token-file secrets/xai-oauth.json
```

The helper prints the xAI verification URL and code, polls until authorization,
and atomically writes the rotating OAuth credentials. Set a different location
with `XAI_OAUTH_TOKEN_FILE` and pass the same path to `--token-file`.

Then add an opt-in profile and model to the deployment's untracked
`config/models.yaml`:

```yaml
providers:
  grok-subscription:
    type: xai
    auth_mode: oauth
    reasoning_effort: high

models:
  grok-chat:
    provider: grok-subscription
    model: grok-4.6
    context_window: 500000
    capabilities: [text, tool_calling, image_input]
```

`auth_mode` is deliberately strict:

- `oauth` is the default and uses only `XAI_OAUTH_TOKEN_FILE`. It rejects an
  `api_key_env` setting and never falls back to an API key.
- `api_key` requires `api_key_env: GROK_API_KEY` and never reads OAuth.
- `auto` tries OAuth first. Add `api_key_env: GROK_API_KEY` to permit fallback
  when OAuth is missing, revoked, or rejected by a recognized entitlement
  response. Rate limits, timeouts, server errors, and generic policy responses
  do not switch billing sources.

The `xai` provider fixes the inference origin in code, sends `store: false`, and
uses the same stateless Responses conversation replay as other Kimi providers.
Refresh-token rotation is serialized across tasks and processes sharing the
token file. A `401` forces one guarded refresh before the request fails or an
explicit `auto` profile uses its API-key fallback.

OAuth login does not guarantee that the selected xAI account tier is entitled
to every model or hosted tool. A strict OAuth profile fails with an actionable
provider error rather than silently consuming `GROK_API_KEY`.

## Existing Chat Completions setup

The existing API-key route remains valid:

```yaml
providers:
  grok-api:
    type: openai_compat
    base_url: https://api.x.ai/v1
    api_key_env: GROK_API_KEY
```

Put the secret in `.env` as `GROK_API_KEY`, then declare and route the model
entries normally. This transport supports the generic client-side function
tools Kimi sends, but it does not expose xAI's provider-hosted tools inside the
main model request.

## X search

The separate [`x_search` tool](x-search.md) can use either credential path and
works with any tool-calling main model, including Grok. It always runs through
Kimi's local tool registry instead of receiving special native injection into a
Grok chat request, so policy, budgets, fallback, and evidence checks stay
identical across providers.

The example uses xAI's current `grok-4.6` model, which supports text, image
input, function calling, and a 500,000-token context window. Verify those live
details and account eligibility against the
[official model page](https://docs.x.ai/developers/models/grok-4.6) before a
production rollout; model availability and commercial terms can change
independently of this transport.
