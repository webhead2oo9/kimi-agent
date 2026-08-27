# Provider resilience

Kimi combines ordered fallback with persistent provider cooldowns. Both are
provider-neutral; optional adapters only translate structured provider errors.

## Fallback within a turn

New turns begin at the primary. Connection, timeout, server errors, and rate
limits without a `Retry-After` header receive one retry; rate limits that name
a `Retry-After` and clear access failures advance immediately.

Fallback is forward-only and sticky within a logical turn. Tool iterations and
resumed coding tasks continue from the last serving backend instead of retrying
earlier links.

## Provider cooldowns

When a backend remains unavailable, Kimi stores a circuit in SQLite. Open
circuits are skipped across turns and restarts.

After cooldown, one request probes the backend while concurrent requests keep
using fallbacks. Success closes the circuit; failure reopens it.

There are two scopes:

- **Model:** transport, server, missing-model, model-access, and generic
  rate-limit failures.
- **Account:** authentication and, through a structured adapter, shared
  rate limits and subscription quotas.

Only opaque keys, safe labels, and normalized reasons are stored—never secrets
or raw provider responses.

## Cooldown selection

Profiles default to 5 minutes for outages, 30 minutes for quota failures, and
1 minute for rate limits that carry no `Retry-After`:

```yaml
providers:
  primary:
    type: openai_compat
    base_url: https://gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
    circuit_breaker:
      outage_cooldown_seconds: 300
      quota_cooldown_seconds: 1800
      rate_limit_cooldown_seconds: 60
```

A valid `Retry-After` always wins, even below these defaults. Subscription
profiles can use a longer quota default:

```yaml
    circuit_breaker:
      quota_cooldown_seconds: 18000
```

While a probe request is in flight after a cooldown, concurrent requests still
skip that backend; on a single-model chain they fail with the cooling-down
message until the probe settles.

Select an optional structured-error adapter with `failure_adapter`; the default
is `generic`.

OpenAI-compatible terminal reasons with explicit meanings are normalized for
all such profiles: network failure opens an availability circuit, while context
overflow and policy termination stop without opening one.

## Operator controls

The owner-only `/models` view lists cooldowns and their next probe time. **Reset
all provider cooldowns** clears persisted and in-memory state without changing
model selection or fallback order. The prompt still names the configured
primary; response metadata records the backend that actually served.
