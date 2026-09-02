# Provider resilience

When a general model provider fails, Kimi does two things: it moves on to the
next model in the role's fallback chain for the rest of the turn, and it
remembers that the failing backend is unhealthy so later turns skip it for a
while. Both mechanisms work across the general provider types constructed by
the provider manager; optional adapters only translate structured error codes
into the shared vocabulary. Lifecycle-owned specialized profiles such as
`gemini_interactions` do not participate in these fallback chains or circuits;
their retry and recovery rules are documented with the feature that owns them.

## Fallback within a turn

Every new turn starts at the role's primary model. Under the generic failure
policy, a connection error, timeout, server error, or rate limit without a
`Retry-After` header gets one retry on the same backend before moving on. A rate
limit that names a `Retry-After`, or a clear authentication failure, moves on
immediately. Structured adapters can also move on immediately when a provider
error identifies a shared account limit or quota without that header.

Within one turn, fallback only ever moves forward and then sticks: once a turn
has moved to the second model, its later tool iterations (and a resumed coding
task) keep using that model rather than retrying the first one each time.

## Provider cooldowns

When a backend stays unavailable, Kimi opens a "circuit" for it and stores
that in SQLite. An open circuit means later turns skip that backend without
trying it, and because it is stored, that survives a restart.

When the cooldown ends, one request is allowed through to probe the backend
while any concurrent requests keep using the fallbacks. If the probe succeeds
the circuit closes and normal routing resumes; if it fails the circuit reopens.

There are two scopes:

- **Model:** transport, server, missing-model, model-access, and generic
  rate-limit failures.
- **Account:** authentication and, through a structured adapter, shared
  rate limits and subscription quotas.

Stored circuit rows contain an opaque scope key, a safe label, the scope and a
normalized reason, optional HTTP and provider error codes, and timestamps. They
never contain secrets or raw provider responses.

## Cooldown selection

Each provider profile has three cooldown lengths, defaulting to 5 minutes for
outages, 30 minutes for quota failures, and 1 minute for rate limits. The
generic policy uses the outage setting for availability and missing-model
failures, the quota setting for HTTP 401, and the rate-limit setting for a bare
429. Structured provider adapters may map their own error codes onto these
settings differently:

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

For generic HTTP failures, a valid `Retry-After` sets the cooldown on 429,
408, 425, and 5xx responses, even when it is shorter than the configured
default. Generic 401 and 404 responses ignore the header and use their mapped
settings. Structured provider adapters can also honor `Retry-After` when they
recognize an account limit or quota. Subscription profiles can use a longer
quota default:

```yaml
    circuit_breaker:
      quota_cooldown_seconds: 18000
```

While a probe request is in flight, concurrent requests still skip that
backend. If the role has no fallback model, those requests fail with the
cooling-down message until the probe settles.

Select an optional structured-error adapter with `failure_adapter`; the default
is `generic`.

For every OpenAI-compatible profile, some end-of-response reasons are handled
the same way: a network failure opens an availability circuit, while a context
overflow or a content-policy stop ends the turn without opening one, because
retrying elsewhere would not help.

## Operator controls

The owner-only `/models` view lists open cooldowns and when each will next be
probed. **Reset all provider cooldowns** clears the stored and in-memory
circuits without changing model selection or fallback order. The system prompt
still names the configured primary model; the response metadata and usage
ledger record which backend actually served each call.
