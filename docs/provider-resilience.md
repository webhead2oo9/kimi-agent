# Provider resilience

Kimi combines ordered fallback with persistent provider cooldowns. Both are
provider-neutral; optional adapters only translate structured provider errors.

## Fallback within a turn

New turns begin at the primary. Connection, timeout, and server errors receive
one retry; rate limits and clear access failures advance immediately.

Fallback is forward-only and sticky within a logical turn. Tool iterations and
resumed coding tasks continue from the last serving backend instead of retrying
earlier links.

## Provider cooldowns

When a backend remains unavailable, Kimi stores a circuit in SQLite. Open
circuits are skipped across turns and restarts.

After cooldown, one request probes the backend while concurrent requests keep
using fallbacks. Success closes the circuit; failure reopens it.

There are two scopes:

- **Model:** transport, server, missing-model, and model-access failures.
- **Account:** authentication, shared rate limits, and subscription quotas.

Only opaque keys, safe labels, and normalized reasons are stored—never secrets
or raw provider responses.

## Cooldown selection

Profiles default to 30 minutes for outages and quota failures:

```yaml
providers:
  primary:
    type: openai_compat
    base_url: https://gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
    circuit_breaker:
      outage_cooldown_seconds: 1800
      quota_cooldown_seconds: 1800
```

A valid `Retry-After` always wins, even below 30 minutes. Subscription profiles
can use a longer quota default:

```yaml
    circuit_breaker:
      outage_cooldown_seconds: 1800
      quota_cooldown_seconds: 18000
```

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
