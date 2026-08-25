# Kimi documentation

This directory is the canonical documentation set for the repository. Runtime
source, configuration templates, deployment files, and tests live under
`../bot/`; links from these pages point there explicitly.

New to the project? Read [Setup and boot](setup.md), then
[Architecture](architecture.md), then the [developer map](../CLAUDE.md).
That is enough to get a bot running and to find your way around the source.
Everything below is reference material. Reach for a page when you touch the
subsystem it covers.

## Start here

- [Setup and boot](setup.md): initial setup and verification.
- [Architecture](architecture.md): system shape and package map.
- [Configuration](configuration.md): every deployment setting and live fragment
  surface.
- [Development](development.md): isolated local/test instances.
- [Public source and private instance data](instance-data.md): what belongs in
  Git and what stays deployment-owned.

## Agent capabilities

- [Tool catalog](tools.md): all built-in model-callable tools and gates.
- [Providers](providers.md): model profiles, routing, fallback, image capability.
  Backend guides: [xAI Grok](providers-grok.md),
  [Claude via ccflare](providers-ccflare.md), [Codex](providers-codex.md),
  [OpenAI and OpenRouter](providers-openai.md).
- [Memory](memory.md): Hindsight-backed user and community memory.
- [Workspace tools](workspace.md): per-(user, guild) file operations and
  containment.
- [Code execution](code-exec.md): Linux sandbox boundary, network modes,
  deployment, quotas, and operator verification.
- [Durable coding agent](coding-agent.md): background planning, managed jobs,
  progress delivery, steering, recovery, and cancellation.
- [Persistent browser](browser.md): BetterWright profiles, isolation, host/VPN
  modes, deployment, privacy, and upgrades.
- [Personal skills](personal-skills.md): user-owned reusable instructions.
- [Persona overrides](persona.md): per-user compiled response styles.
- [Discord embeds](embeds.md): rich reply construction.
- [Thread handoff](thread-handoff.md): managed conversation threads.
- [Learning](learning.md): staff-taught facts and procedures, the **Teach Kimi**
  menu, and its injection posture.
- [Context compaction](compaction.md): in-turn context-window management.

## Operations, safety, and evaluation

- [Database](database.md): initial SQLite schema and operational safeguards.
- [Privacy](privacy.md): data flow, retention, deletion, operator access.
- [Privacy policy](privacy-policy.md): member-facing plain-language policy.
- [Application modules](modules.md): versioned optional commands, listeners,
  schema, background work, and LLM tools.
- [Observability](observability.md): structured local event output.
- [Operator plugins](plugins.md): deployment-owned Python extensions.
- [Evals](evals.md): offline model qualification and cassette behavior.

## Component-local guides

- [Hindsight deployment](../bot/deploy/hindsight/README.md): the optional
  Compose stack and its storage/network boundary.
- [Code-exec netns templates](../bot/deploy/code-exec-netns/README.md): generic
  privileged-helper and sudoers provisioning boundary.
- [Shared skill stores](../bot/skills/README.md): shipped built-ins, private
  provisioning and backup, and executable-skill trust.
- [Full prompt overrides](../bot/config/prompts/README.md): resolution and
  authoring for complete prompt layouts.
- [Developer map](../CLAUDE.md): source boundaries and maintenance
  conventions.
- [Changelog](../bot/changelog.md): member- and staff-visible behavior changes.
