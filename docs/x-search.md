# X search

`x_search` is an optional, member-tier searchable tool backed by xAI's hosted
Responses API `x_search` capability. It is independent of the active chat
provider: Claude, Kimi, OpenAI, Grok, or any other model with function-tool
support invokes the same local tool.

The tool defaults off. Enabling it does not select Grok as the chat model, and
signing into xAI does not enable the tool.

## Setup

For OAuth, first run the device flow described in [xAI Grok](providers-grok.md):

```bash
.venv/bin/python scripts/xai_auth.py --token-file secrets/xai-oauth.json
```

For API-key use, set `GROK_API_KEY`. Then configure the tool:

```dotenv
X_SEARCH_ENABLED=true
X_SEARCH_AUTH_MODE=auto
X_SEARCH_MODEL=grok-4.6
X_SEARCH_TIMEOUT_SECONDS=180
X_SEARCH_MAX_CALLS_PER_TURN=10
```

Authentication modes have exact boundaries:

- `oauth`: OAuth only. A present `GROK_API_KEY` is ignored and is never a
  fallback.
- `api_key`: API key only. OAuth is never read.
- `auto`: OAuth first. `GROK_API_KEY` may be used after missing/revoked OAuth,
  a recognized auth or entitlement rejection, or an OAuth response with no
  evidence that live X search ran. Transient network/server failures are
  retried without switching credentials. If that fallback call itself fails,
  the degraded OAuth answer is still returned rather than discarded.

If the flag is true but the selected mode has no usable credential, startup
continues with the tool unregistered. Because the tool is searchable, a model
discovers and loads it through `browse_tools`; its schema is not added to every
ordinary prompt.

## Request and output

The tool accepts a bounded query, optional `from_date`/`to_date`, up to 20
allowed or excluded handles, and optional image/video understanding. Allowed
and excluded handle filters are mutually exclusive. Dates must be
`YYYY-MM-DD`; the start may not be after the end or in the future.

Every request sends `store: false`. Output is marked as untrusted and contains:

- the synthesized answer;
- normalized top-level and inline URL citations;
- xAI's reported `x_search_calls` count when present;
- a `degraded` flag.

A response is degraded only when it contains neither citations nor a positive
`x_search_calls` count. Strict modes return that status as-is. In `auto`, a
degraded OAuth result is retried once with `GROK_API_KEY` when the key and call
budget are available. Both completed model calls are recorded in usage.

The default ten-call budget counts actual upstream HTTP attempts, including
retries and credential fallback, rather than only logical tool invocations.
This bounds both paid exposure and repeated subscription use.

## OAuth limitation

xAI's public X-search documentation primarily describes API-key access. An
OAuth bearer may successfully authorize `/v1/responses` while the account tier
does not execute the hosted X index, producing generic uncited prose instead.
Kimi therefore treats authentication success and verified live search as
separate facts. Operators should validate a real account by confirming both a
positive `x_search_calls` value and citations before relying on OAuth X search.
