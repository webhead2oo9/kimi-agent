# OpenAI and OpenRouter

Both are reached through the generic transports described in
[providers.md](providers.md). Neither needs a dedicated provider class; what
they need is a few profile fields that only make sense on their endpoints.

## OpenAI

OpenAI models can be routed through either `openai_compat` (Chat Completions) or
`openai_responses` (the Responses API). Pick the one the model actually
supports; the Responses transport is what carries reasoning with encrypted
replay, image output, and `store=false` local history.

Three profile fields matter on OpenAI-family transports:

- **`service_tier`**: the OpenAI service tier, such as `flex`. It is sent only
  when the profile's `base_url` is `https://api.openai.com` or is unset (the SDK
  default endpoint). On any other gateway it is silently dropped, for both
  `openai_compat` and `openai_responses`, because a gateway that has never heard
  of the field would reject the whole request.
- **`timeout_seconds`**: the per-call timeout for OpenAI-family providers. It
  is honored by `openai_responses` (along with `anthropic` and
  `anthropic_compat`) and ignored by `openai_compat`, which relies on the stall
  watchdog instead.
- **`app_name`**: an optional provider-facing identity override. When it is
  omitted, the profile inherits `BOT_NAME`. OpenAI-compatible transports send
  the resolved identity as their `User-Agent`.

The reasoning rules for `openai_responses` (when a `reasoning` parameter is sent
at all, and why `include: ["reasoning.encrypted_content"]` is mandatory once it
is) are on the [reasoning effort](providers.md#reasoning-effort) section of the
main page, since they interact with the provider-neutral rail.

## OpenRouter

Use `type: openrouter`. It supports text, multimodal input, tool calling, and
image output models, and adds three profile fields:

- **`provider_routing`**: a structured OpenRouter provider-routing object,
  serialized into the request. This is where you express upstream preferences,
  ordering, or exclusions.
- **`app_name`**: the shared provider identity described above. OpenRouter
  also sends it as its attribution title.
- **`app_url`**: attribution header, unset by default.

```yaml
providers:
  openrouter:
    type: openrouter
    base_url: https://openrouter.ai/api/v1
    api_key_env: MODEL_API_KEY
    # Optional: defaults to BOT_NAME.
    app_name: Community Assistant
    provider_routing:
      order: [primary-upstream, secondary-upstream]
      allow_fallbacks: true
```

OpenRouter's own upstream fallback and Kimi's `<role>_fallbacks` chain are
independent layers. OpenRouter can silently move you between upstreams inside a
single request; the Kimi chain only engages when the whole OpenRouter call
fails with a transient availability error. If you want both, that is fine, but
remember that a reply attributed to the OpenRouter model entry may have been
served by any upstream that routing chose.
