# Kimi documentation

This directory is the canonical documentation set for the repository. The
runtime source, configuration templates, deployment files, and tests all live
under `../bot/`, and links from these pages point there explicitly.

If you're new to the project, read [Setup and boot](setup.md) first, then
[Architecture](architecture.md), then the [developer map](../CLAUDE.md).
That's enough to get a bot running and to find your way around the source.
The rest is reference material.

## Start here

- [Setup and boot](setup.md): initial setup and verification.
- [Architecture](architecture.md): the shape of the system and its package map.
- [Configuration](configuration.md): every deployment setting and every live
  fragment surface.
- [Development](development.md): running isolated local and test instances.
- [Public source and private instance data](instance-data.md): what belongs in
  Git and what stays deployment-owned.

## Agent capabilities

- [Tool catalog](tools.md): all built-in model-callable tools and their gates.
- [Providers](providers.md): model profiles, routing, fallback, and image
  capability. Backend guides: [Z.AI GLM Coding Plan](providers-zai.md),
  [xAI Grok](providers-grok.md), [Claude via ccflare](providers-ccflare.md),
  [Codex](providers-codex.md), and
  [OpenAI and OpenRouter](providers-openai.md).
- [Provider resilience](provider-resilience.md): sticky fallback, persistent
  circuit breakers, cooldown policy, and owner recovery controls.
- [Memory](memory.md): Hindsight-backed user and community memory.
- [Workspace tools](workspace.md): file operations sandboxed per user and
  guild.
- [Video understanding](video-understanding.md): stateful Gemini analysis of
  public YouTube or streamed Discord/workspace videos, session scope, Files API
  lifecycle, caching, limits, and deletion.
- [Image generation](image-generation.md): the REGULAR-tier OpenAI generation
  and editing tool, OAuth/API-key modes, workspace references, limits, and the
  provider extension seam.
- [Code execution](code-exec.md): the Linux sandbox boundary, network modes,
  deployment, quotas, and operator verification.
- [Durable coding agent](coding-agent.md): background planning, managed jobs,
  progress delivery, steering, recovery, and cancellation.
- [Persistent browser](browser.md): BetterWright profiles, isolation, host and
  VPN modes, deployment, privacy, and upgrades.
- [Visual rendering](visual-rendering.md): one-call charts and constrained
  Mermaid diagrams through the ephemeral offline browser runtime.
- [Personal skills](personal-skills.md): reusable instructions owned by a user.
- [Persona overrides](persona.md): per-user compiled response styles.
- [Discord embeds](embeds.md): building rich replies.
- [Thread handoff](thread-handoff.md): managed conversation threads.
- [Learning](learning.md): staff-taught facts and procedures, the **Teach Kimi**
  menu, and its injection posture.
- [Context compaction](compaction.md): managing the context window within a
  turn.

## Operations, safety, and evaluation

- [Database](database.md): the initial SQLite schema and its operational
  safeguards.
- [Privacy](privacy.md): data flow, retention, deletion, and operator access.
- [Privacy policy](privacy-policy.md): the member-facing plain-language policy.
- [Application modules](modules.md): versioned optional commands, listeners,
  schema, background work, and LLM tools.
- [Observability](observability.md): structured local event output.
- [Operator plugins](plugins.md): deployment-owned Python extensions.
- [Evals](evals.md): offline model qualification and cassette behavior.

## Component-local guides

- [Hindsight deployment](../bot/deploy/hindsight/README.md): the optional
  Compose stack and its storage and network boundary.
- [Code-exec netns templates](../bot/deploy/code-exec-netns/README.md): the
  generic privileged-helper and sudoers provisioning boundary.
- [Shared skill stores](../bot/skills/README.md): shipped built-ins, private
  provisioning and backup, and executable-skill trust.
- [Full prompt overrides](../bot/config/prompts/README.md): resolution and
  authoring for complete prompt layouts.
- [Developer map](../CLAUDE.md): source boundaries and maintenance
  conventions.
