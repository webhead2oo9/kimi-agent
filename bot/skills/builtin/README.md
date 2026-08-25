# Built-in skills

This directory contains instruction-only skills shipped with the bot. Each child
directory is a globally available, read-only skill containing `SKILL.md` and an
optional `reference/` tree.

Built-in skills must not declare executable tools, secrets, or guild scoping.
Deployment-owned skills belong in the private `SKILLS_DIR` store instead.

`{{bot_name}}` is the only supported template placeholder. It is replaced in a
built-in skill's description and body with the configured bot name. Unknown
placeholders fail startup validation; private skills are never templated.

The shipped catalog is deliberately small and deployment-neutral:

- `bot-info` - identity, capability honesty, invocation, and privacy basics;
- `browser` - routing and evidence-driven operation of the optional browser;
- `coding-work` - public repository import and routing among file tools,
  `run_code`, and durable coding tasks;
- `embed` - composing one rich Discord embed;
- `start-thread` - managed thread creation and lifecycle controls;
- `workspace` - inspecting, changing, extracting, packaging, and returning files.
