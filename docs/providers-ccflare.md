# Claude subscription via ccflare

[ccflare](https://github.com/snipeship/ccflare) is a separately deployed
multi-provider proxy that holds Claude OAuth accounts and load-balances across
them. Routing Claude-subscription models through it means those turns are
covered by the subscription rather than by metered API billing.

This is a [subscription-backed
route](providers.md#subscription-backed-routes): it points the bot at a personal
Claude subscription rather than a metered API key, which suits an instance you
run for yourself or a small trusted server. That section explains what the
choice implies; this page covers the route itself. For profiles, roles, and
failover in general, see [providers.md](providers.md).

## Before you start

Run ccflare on a private host at `http://<ccflare-host>:<port>`, from a
deployment-owned checkout such as `<path-to-ccflare>`, following its upstream
instructions.

**ccflare has no authentication and stores tokens in plaintext SQLite.** Keep it
on a trusted network, because anything that can reach the port can spend the
subscription.

## The profile

Route Claude-subscription models through ccflare's *compatibility* endpoint,
using `anthropic_compat`:

```yaml
providers:
  ccflare:
    type: anthropic_compat
    base_url: http://<ccflare-host>:<port>/v1/ccflare/anthropic
    keyless: true

models:
  claude-subscription:
    provider: ccflare
    model: anthropic/<served-model-id>
    context_window: <verified-context-window>
    capabilities: [text, tool_calling]
```

Set the context window and capabilities from the served model's documentation.
Add `image_input` only when that model supports it.

Four details decide whether this works:

- **The base URL must be the `/v1/ccflare/anthropic` prefix.** The provider
  POSTs to `{base_url}/messages`, which is exactly the route ccflare parses.
- **The model id needs the `anthropic/` family prefix.** ccflare strips the prefix and dispatches to the
  Claude Code or API-key accounts that deployment has configured.
- **The profile is `keyless: true`.** ccflare replaces the client credential
  with the selected account's credential: a Claude Code account gets
  `Authorization: Bearer <oauth token>`, while an Anthropic API-key account gets
  its `x-api-key`. It also adds the `anthropic-version` and `anthropic-beta`
  headers. Supplying a Kimi-side key would be pointless, and the startup
  credential gate is satisfied without one.
- **The native `/v1/anthropic/*` prefix will not work here.** It forwards
  straight to api.anthropic.com and demands a real API key, which defeats the
  entire purpose of the route.

## Cost and quota

Model entries on this route carry no `pricing`. Usage is covered by the Claude
subscription, so these turns contribute nothing to `/usage`, just like Codex
models. The quota is shared with any other Claude Code usage on the same
account, so a busy bot and a busy terminal compete for it.

## Prompt caching

`anthropic_compat` marks the last content block of the last message with
`cache_control: {"type": "ephemeral"}`. The profile field `prompt_caching`
controls this and defaults to on.

The cached prefix is everything *before* that block: the system prompt, the tool
schemas, and the whole transcript. Each ReAct iteration therefore reads the
cached prefix instead of paying to write it again. Example accounting for a
5.3k-token system prompt through ccflare:

| Iteration | Cache write | Cache read |
|---|---|---|
| 1 | 5351 | 0 |
| 2 | 102 (the delta) | 5351 |
| 3 | -- | 5453 |

Two implementation details matter here:

- **The breakpoint rides the message list, not `system`.** A breakpoint placed
  inside the `system` array is silently ignored on ccflare's claude-code route.
  Repeated identical requests then report no cache creation without returning
  an error, making the failure easy to miss and potentially expensive.
- **The marked block and its containing list are copied first.** An assistant
  message's content list is shared with the stored `raw_provider_data`. Writing
  a breakpoint back into it would replay in every later turn and eventually blow
  past Anthropic's four-breakpoint limit.

Nothing upstream does any of this for you: ccflare passes the claude-code body
through verbatim and never injects `cache_control` itself. Set
`prompt_caching: false` on a profile whose gateway rejects the field, or that
bills cache writes at a rate that outweighs the reads.

## Reasoning effort on this route

The reasoning rail is provider-neutral: `agent/core.py` sets
`ProviderRequest.reasoning_effort` and `anthropic_compat` maps a supported value
to `output_config.effort`. Tool-triggered escalation is monotonic within a turn
and resets on the next Discord message.

Anthropic's accepted ladder is narrower than the agent's internal one. An
escalation that would land outside it is dropped rather than forwarded, because
forwarding it produces a deterministic 400, and a deterministic error never
fails over, so it would kill the turn.

## Extended thinking on this route

ccflare sends the claude-code beta header set, which turns extended thinking on.
Responses arrive with signed `thinking` blocks ahead of the text, and that has
two consequences:

- `_blocks_to_data` preserves `thinking` and `redacted_thinking` blocks in the
  raw assistant history, mirroring the native `anthropic` provider, so a
  tool-use continuation echoes them back unmodified. They are signed, so they
  must go back exactly as they came.
- The cache breakpoint skips a trailing thinking block and lands on the last
  block that is not one, for the same reason.

Thinking tokens are drawn from `max_tokens`, so a small budget can return a
response containing *only* a thinking block and no text at all.
`REACT_MAX_TOKENS` (65536) leaves ample room, but it is worth knowing if you are
hand-testing with a small `max_tokens`.
