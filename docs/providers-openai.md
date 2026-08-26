# OpenAI and OpenRouter

Both of these are reached through the generic transports described in
[providers.md](providers.md). Neither needs a dedicated provider class; what
they need is a few profile fields that only make sense on their endpoints, and
this page covers those.

## OpenAI

OpenAI models can be routed through either `openai_compat` (Chat Completions) or
`openai_responses` (the Responses API). Pick the one the model actually
supports. If you need reasoning with encrypted replay, image output, or
`store=false` local history, the Responses transport is the one that carries
them.

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

Use `type: openrouter`. It supports text, multimodal input, tool calling, and
image output models, and it adds three profile fields of its own:

- **`provider_routing`**: a structured OpenRouter provider-routing object,
  serialized into the request. This is where you express upstream preferences,
  ordering, or exclusions.
- **`app_name`**: the same provider identity described above. OpenRouter
  also receives it as its attribution title.
- **`app_url`**: the attribution header, unset by default.

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

One thing to keep straight: OpenRouter's own upstream fallback and Kimi's
`<role>_fallbacks` chain are independent layers. OpenRouter can silently move
you between upstreams inside a single request, while the Kimi chain only
engages when the whole OpenRouter call fails with a transient availability
error. Using both is fine, but remember that a reply attributed to the
OpenRouter model entry may have been served by any upstream that routing chose.
