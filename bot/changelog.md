# Changelog

Kimi's user-facing release notes for server owners, staff, and members.

## Unreleased

### New

- Experimental owner-approved configuration control plane. Trusted modules can
  propose configuration changes; the bot owner reviews them with
  `/proposals list`, `/proposals show`, `/proposals approve`,
  `/proposals reject`, and stages secrets with `/proposals stage-secret`.
  Off by default (`CONTROL_PLANE_ENABLED`).
- Application modules install through a versioned public module API and can
  declare the control-plane capabilities they need.
- `/modules status` and `/modules manifest` (bot owner only) show each
  application module's health and what it declares it uses.
- Application modules now run on core-provided services (events, scheduled
  jobs, Discord actions with staff protection, slash commands, per-guild
  settings, and outbound HTTP limited to declared hosts). Per-guild module
  settings move to `config/guild-modules/<server>/<module>.md`; the old
  keys in `config/servers/<server>.md` keep working for one release and
  show up in `/modules status` until they are moved. A broken module
  setting takes only that server offline for enforcement modules instead
  of leaving it unmoderated.
- Long, tool-heavy conversations in shared channels can now receive a one-time
  suggestion to continue in a Discord thread. Server owners can tune the
  threshold with `THREAD_HANDOFF_SUGGEST_AFTER_TOOL_CALLS`, or set it to `0` to
  disable the suggestion.
- Thread handoff is available to the model without a preliminary tool-discovery
  step, improving explicit thread requests and multi-turn troubleshooting. It
  is hidden where Discord cannot create another local thread.
- Model evaluation runs now isolate identities, workspaces, attachments,
  browser profiles, generated artifacts, coding controls, and databases across
  models and repetitions. Reports also identify modified source trees, flag
  incomplete turns, and cover browser, internet search, URL download, vision,
  and workspace behavior more directly.
- Eval browser and runtime resources are closed before temporary state is
  removed, reducing leaked sessions and cross-run interference after failures.
- Replayed eval internet searches now preserve the live backend-call budget,
  keeping cassette runs aligned with production search limits.
- Eval runs can use one shared vision model to caption image fixtures once and
  give every candidate the same cached visual evidence, including text-only
  models, while reporting caption-assisted coverage explicitly.

## 1.0.0 (2026-08-24)

First public release.
