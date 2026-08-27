# Z.AI GLM Coding Plan

Kimi can use Z.AI's GLM Coding Plan through its OpenAI-compatible Chat
Completions API. No Z.AI-specific provider or SDK is required.

## Before you start

The Coding Plan and the standard API use different URLs:

| Product | Base URL |
|---|---|
| GLM Coding Plan | `https://api.z.ai/api/coding/paas/v4` |
| Standard pay-as-you-go API | `https://api.z.ai/api/paas/v4` |

Use the first URL to consume Coding Plan quota. The second URL charges the
account's API balance instead.

## Configure the model

Add the API key to the dotenv file used by Kimi:

```dotenv
ZAI_API_KEY=your-zai-key
```

Keep the key out of `config/models.yaml`. Add the provider there:

```yaml
providers:
  zai-coding-medium:
    type: openai_compat
    base_url: https://api.z.ai/api/coding/paas/v4
    api_key_env: ZAI_API_KEY
    reasoning_effort: medium
    failure_adapter: zai
    circuit_breaker:
      outage_cooldown_seconds: 1800
      quota_cooldown_seconds: 18000
  zai-coding-xhigh:
    type: openai_compat
    base_url: https://api.z.ai/api/coding/paas/v4
    api_key_env: ZAI_API_KEY
    reasoning_effort: xhigh
    failure_adapter: zai
    circuit_breaker:
      outage_cooldown_seconds: 1800
      quota_cooldown_seconds: 18000
```

Separate profiles may share the same served model ID when chat and coding need
different reasoning defaults. Give their local model entries distinct names,
such as `zai-glm-medium` and `zai-glm-xhigh`, while keeping the upstream `model`
value unchanged.

Add the models you want to use under `models:` as described in
[Providers](providers.md). This guide does not maintain a model catalog because
IDs, context windows, and capabilities change independently of the API
transport. Verify each model before declaring `tool_calling` or `image_input`.

Add a model entry's local name to `selectable_chat_models` if it should appear
in the owner-only `/models` menu.

The `zai` failure adapter reads structured API error codes and translates them
into the same model/account circuits used by every provider. Coding Plan quota
defaults to a five-hour cooldown when the API supplies no exact reset time;
`Retry-After` and explicit longer plan-limit windows take precedence. Routing
and persistence contain no Z.AI-specific branches. See
[Provider resilience](provider-resilience.md).

## Use it for coding tasks

To route only the durable coding agent through the new entry:

```yaml
roles:
  coding: your-zai-model-entry
  coding_fallbacks: []
```

The `coding` role is independent of normal chat. Coding tasks also require the
code sandbox and `CODING_TASKS_ENABLED=true`; see
[Durable coding agent](coding-agent.md).

Restart Kimi after changing `.env` or `config/models.yaml`. Model selection
through `/models` takes effect immediately and does not require another
restart.

## Verify the setup

After restarting:

1. Open `/models` and select the Z.AI model entry.
2. Send a short text prompt.
3. Ask for an action that requires a Kimi tool.
4. If the model declares `image_input`, attach an image and ask a simple
   question about it.

A `401` response usually means the key is missing, invalid, or inactive. If a
request uses API balance instead of Coding Plan quota, confirm that the provider
uses the `/api/coding/paas/v4` URL shown above.

## Subscription use

Z.AI limits Coding Plan benefits to supported tools and the plan subscriber.
Before exposing this model through a shared bot, review Z.AI's current tool and
account-usage rules. The API being compatible does not by itself make every
deployment eligible for subscription quota.

For the general profile and routing concepts used here, see
[Providers](providers.md).
