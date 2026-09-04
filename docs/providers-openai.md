# OpenAI and OpenRouter

OpenAI uses the generic transports described in [providers.md](providers.md).
OpenRouter has a small wrapper around the generic Chat Completions transport for
its routing, attribution, and image-output fields. This page covers the profile
settings that matter on those endpoints.

## OpenAI

OpenAI models can be routed through either `openai_compat` (Chat Completions) or
`openai_responses` (the Responses API). Pick the one the model actually
supports. If you need reasoning with encrypted replay or `store=false` local
history, the Responses transport carries them. Kimi's Responses wrapper does
not expose provider-native image output; normal Discord image creation uses the
[`generate_image` tool](image-generation.md).

Three profile fields matter on OpenAI-family transports:

- **`service_tier`**: the OpenAI service tier, such as `flex`. It is sent only
  when the profile's `base_url` is `https://api.openai.com` or is unset (the SDK
  default endpoint). On any other gateway it is silently dropped, for both
  `openai_compat` and `openai_responses`, because a gateway that has never heard
  of the field would reject the whole request.
- **`timeout_seconds`**: the SDK transport timeout for OpenAI-family providers. It
  is honored by `openai_compat` and `openai_responses` (along with `anthropic`
  and `anthropic_compat`). Streaming `openai_compat` calls also keep their
  shorter stall watchdog. These are transport/inactivity bounds, not a promise
  that a continuously streaming response finishes within one wall-clock deadline.
- **`app_name`**: an optional provider-facing identity override. When you leave
  it out, the profile inherits `BOT_NAME`. OpenAI-compatible transports send
  the resolved identity as their `User-Agent`.

The reasoning rules for `openai_responses` (when a `reasoning` parameter is sent
at all, and why `include: ["reasoning.encrypted_content"]` is mandatory once it
is) live in the [reasoning effort](providers.md#reasoning-effort) section of
the main page, since they interact with the provider-neutral rail rather than
being specific to OpenAI.

## OpenRouter

Use `type: openrouter`. The adapter talks to OpenRouter's fixed Chat
Completions endpoint, and carries the same inputs Kimi already supports:
text, image input, client-side function tools, and inline image output when
you ask for it explicitly. Tools like browser, search, code execution,
workspace tools, and `generate_image` are still local ToolRegistry work, so
routing through OpenRouter never gets around Kimi's authorization or
sandbox.

An OpenRouter profile must set `api_key_env`. It cannot use `keyless`, and
it cannot set `base_url`. On top of the generic profile fields it gains:

- **`provider_routing`**: a typed OpenRouter routing policy, validated at
  startup and sent as the request's `provider` field.
- **`service_tier`**: optional `flex` or `priority`, sent as the top-level
  service tier. Leave it empty to keep OpenRouter's default.
- **`timeout_seconds`**: the SDK transport timeout for OpenRouter requests.
- **`app_name`**: the provider-facing identity. Inherits `BOT_NAME` when
  unset, and is sent to OpenRouter as `X-OpenRouter-Title`.
- **`app_url`**: the `HTTP-Referer` attribution header, unset by default.

```yaml
providers:
  openrouter:
    type: openrouter
    api_key_env: MODEL_API_KEY
    service_tier: priority
    timeout_seconds: 180
    # Optional: defaults to BOT_NAME.
    app_name: Community Assistant
    app_url: https://assistant.example
    provider_routing:
      # Omitting privacy fields preserves the OpenRouter account/request default.
      zdr: true
      data_collection: deny
      require_parameters: true
      order: [anthropic, google]
      only: [anthropic, google]
      allow_fallbacks: false
      quantizations: [bf16, fp8]
      sort:
        by: throughput
        partition: none
      max_price:
        prompt: 0.50
        completion: 1.50
      preferred_min_throughput:
        p90: 40
      preferred_max_latency: 4
```

The routing policy accepts `order`, `only`, `ignore`, `allow_fallbacks`,
`require_parameters`, `data_collection`, `zdr`, `enforce_distillable_text`,
`quantizations`, `sort`, `max_price`, `preferred_min_throughput`, and
`preferred_max_latency`. Anything else is rejected. So are duplicate or
blank provider names, an `only` and `ignore` that overlap, an `order`
that names a provider outside `only`, values outside the supported enums,
and numbers that aren't in range. Provider names are not checked against a
hard-coded list because OpenRouter adds and renames them. Quantizations are
validated against an enum rather than a frozen list.

`sort` takes either the string `price`, `throughput`, or `latency`, or an
object with `by` set to one of those and an optional `partition` of `model`
or `none` for sorting across model fallbacks. Throughput and latency
preferences take either a single number or an
`OpenRouterPercentileThreshold` object with one or more of `p50`, `p75`,
`p90`, `p99`.

Every routing and privacy field is optional. If you leave `zdr` or
`data_collection` out, that field is not sent at all, so the OpenRouter
account or request default wins. Setting them to `false` is kept, since that
is an explicit choice. See OpenRouter's
[provider-routing guide](https://openrouter.ai/docs/guides/routing/provider-selection)
and its
[service-tier guide](https://openrouter.ai/docs/guides/features/service-tiers)
for what each one means.

### Response attribution

Every request opts in to bounded router metadata. The turn event records one
`provider_calls` row per completed model call, in call order. Each row
carries the served model, the configured `pricing_model`, the call `role`,
and any of `upstream_provider`, `service_tier`, `openrouter_charge_usd`,
and `is_byok` that OpenRouter sent back. Fields Kimi doesn't recognize
are dropped, and the full router payload never lands in conversation
history. The adapter reads the BYOK flag from
`openrouter_metadata.is_byok`, the upstream provider from the `selected`
entry in `openrouter_metadata.endpoints.available`, and the charge from
`usage.cost`.

The field is called `openrouter_charge_usd` because that is the amount
OpenRouter charged your account. A BYOK response can come back cheaper
than what you'd get from the upstream provider's own invoice, because the
two surfaces don't agree on who is paying whom. See OpenRouter's
[usage accounting guide](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
if the difference matters.

This charge lives in the turn event only. The `/usage` command keeps
pricing from your static rate card in model config, and it can come back
different from what OpenRouter reported. That is intentional: this change
does not touch the usage ledger schema.

One thing to keep separate: OpenRouter's own upstream fallback and Kimi's
`<role>_fallbacks` chain are two different layers. OpenRouter can quietly
re-route between upstreams inside one request. Kimi's chain only kicks in
when the whole OpenRouter call fails with a transient availability
error. Use both if you want; just remember that a reply attributed to the
OpenRouter model entry may have been served by any upstream the router
chose. The turn event's `provider_calls` field shows you which one when
the router tells us.
