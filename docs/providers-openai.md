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

Use `type: openrouter`. The adapter uses OpenRouter's fixed Chat Completions
endpoint and supports Kimi's existing text, image-input, client function-tool,
and explicitly requested inline image-output transport. Browser, search, code,
workspace, and `generate_image` tools remain local ToolRegistry operations, so
OpenRouter cannot bypass Kimi's authorization or sandbox.

An OpenRouter profile must set `api_key_env`, cannot use `keyless`, and cannot
override `base_url`. It adds these profile fields:

- **`provider_routing`**: a structured OpenRouter provider-routing object,
  validated at startup and serialized into the request's `provider` field.
- **`service_tier`**: optional `flex` or `priority`, sent as the top-level
  OpenRouter service tier. An empty value leaves OpenRouter's default unchanged.
- **`timeout_seconds`**: the SDK transport timeout for the OpenRouter request.
- **`app_name`**: the same provider identity described above. OpenRouter
  receives it as `X-OpenRouter-Title` and the legacy `X-Title` header.
- **`app_url`**: the attribution header, unset by default.

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

The routing schema supports `order`, `only`, `ignore`, `allow_fallbacks`,
`require_parameters`, `data_collection`, `zdr`, `enforce_distillable_text`,
`quantizations`, `sort`, `max_price`, `preferred_min_throughput`, and
`preferred_max_latency`. Unknown keys, duplicate/blank provider names,
contradictory `only`/`ignore` filters, unsupported enum values, and invalid
numeric bounds fail configuration loading. Provider names are not checked
against a hard-coded catalog because OpenRouter can add or rename them.
`sort` accepts either `price`, `throughput`, or `latency` directly, or an object
with one of those values in `by` and an optional `partition` of `model` or
`none` for advanced sorting across model fallbacks.

All routing and privacy members are optional. In particular, omitted `zdr` and
`data_collection` values are not sent, so the upstream default is preserved;
an explicit `false` is retained. See OpenRouter's current
[provider-routing documentation](https://openrouter.ai/docs/guides/routing/provider-selection)
for the semantics of each field and its
[service-tier documentation](https://openrouter.ai/docs/guides/features/service-tiers)
for tier behavior.

Every request opts into bounded router metadata. Each provider call in the turn
event reports the served model, configured pricing model, selected upstream
provider, returned service tier, OpenRouter account charge, and BYOK flag when
OpenRouter returns them. Unknown metadata fields are ignored and raw router
metadata never enters conversation state. The adapter reads the BYOK flag from
`openrouter_metadata.is_byok` and the provider from the selected entry in
`openrouter_metadata.endpoints.available`. `usage.cost` is named
`openrouter_charge_usd` because it is the amount charged to the OpenRouter
account; a BYOK response can therefore differ from the upstream provider's own
invoice. See OpenRouter's
[usage accounting documentation](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

This exact charge is response/turn-event telemetry only. Kimi's `/usage`
command continues to use the static rate card in model configuration and may
differ from the OpenRouter charge; this integration does not change the usage
ledger schema.

One thing to keep straight: OpenRouter's own upstream fallback and Kimi's
`<role>_fallbacks` chain are independent layers. OpenRouter can silently move
you between upstreams inside a single request, while the Kimi chain only
engages when the whole OpenRouter call fails with a transient availability
error. Using both is fine, but remember that a reply attributed to the
OpenRouter model entry may have been served by any upstream that routing chose.
The turn event's `provider_calls` field makes that selection visible when the
router supplies metadata.
