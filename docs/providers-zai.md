# Z.AI GLM Coding Plan

Kimi can use Z.AI's GLM Coding Plan through its OpenAI-compatible Chat
Completions API. No Z.AI-specific provider or SDK is required. This is technical
compatibility; Z.AI's current plan eligibility does not list Kimi. See
[Subscription use](#subscription-use) before enabling the route.

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
  zai-coding:
    type: openai_compat
    base_url: https://api.z.ai/api/coding/paas/v4
    api_key_env: ZAI_API_KEY
    failure_adapter: zai
    circuit_breaker:
      quota_cooldown_seconds: 18000
```

If you set `reasoning_effort`, use a value accepted by the served model's current
documentation. Omitting the field uses the provider default. Separate profiles
may share one served model ID when they need different valid defaults. See
Z.AI's [Chat Completion
reference](https://docs.z.ai/api-reference/llm/chat-completion).

Add the models you want to use under `models:` as described in
[Providers](providers.md). This guide does not maintain a model catalog because
IDs, context windows, and capabilities change independently of the API
transport. Verify each model before declaring `tool_calling` or `image_input`.

Add a model entry's local name to `selectable_chat_models` if it should appear
in the owner-only `/models` menu.

The `zai` failure adapter reads structured API error codes and translates them
into the same model/account circuits used by every general provider. A valid HTTP
`Retry-After` value sets the cooldown. Without one, identified five-hour and
seven-day limits use those windows. Other recognized errors use the profile's
quota or outage cooldown according to the code-specific mapping. Reset timestamps
that appear only in provider message text are not parsed. Routing and
persistence contain no Z.AI-specific branches.
See [Provider resilience](provider-resilience.md).

## Use it for coding tasks

To route only the durable coding agent through the configured model entry:

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

To verify a chat route after restarting:

1. Add the entry to `selectable_chat_models`, then open `/models` and select it.
2. Send a short text prompt.
3. Ask for an action that requires a Kimi tool.
4. If the model declares `image_input`, attach an image and ask a simple
   question about it.

For a coding-only route, start a coding task instead and confirm that its status
and final report complete; `/models` changes the chat primary, not `roles.coding`.

A `401` response usually means the key is missing, invalid, or inactive. If a
request uses API balance instead of Coding Plan quota, confirm that the provider
uses the `/api/coding/paas/v4` URL shown above.

## Subscription use

Z.AI limits Coding Plan benefits to the subscriber and its [officially
supported tools](https://docs.z.ai/devpack/tool/others), prohibits multi-user
access, and does not currently list Kimi. Use the standard pay-as-you-go API for
Kimi unless Z.AI confirms this deployment is eligible. See the current [Usage
Policy](https://docs.z.ai/devpack/usage-policy).

For the general profile and routing concepts used here, see
[Providers](providers.md).
