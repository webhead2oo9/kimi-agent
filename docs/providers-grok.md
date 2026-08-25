# xAI Grok

xAI serves an OpenAI-compatible Chat Completions API, so Grok needs no
Grok-specific code at all. The generic `openai_compat` connector handles text,
tool calling, image input (jpg and png), `reasoning_content`, and cached-token
usage reporting.

See [providers.md](providers.md) for how profiles, model entries, and roles fit
together.

## Setup

Point an `openai_compat` profile at the xAI endpoint:

```yaml
providers:
  grok:
    type: openai_compat
    base_url: https://api.x.ai/v1
    api_key_env: GROK_API_KEY
```

Put the key in `.env` as `GROK_API_KEY`. Then declare the model entries you
actually want to route, and assign them to roles as usual.

## A note on values

Model ids, context windows, prices, reasoning defaults, and role assignments all
change independently of the transport, and they change faster than this
repository does. Keep the deployment's verified values in the ignored
`config/models.yaml` only. The tracked `config/models.example.yaml` uses
non-routable placeholders on purpose: a template carrying real ids and prices
would be stale documentation that looks authoritative, and every instance would
inherit whatever was true on the day it was written.
