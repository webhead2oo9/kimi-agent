"""The Discord boundary: everything that speaks the platform's API.

The core platform modules:

- `io`: reading and writing Discord messages, including the eligibility and
  mention gates, chunking, attachments, embeds, and the live activity surface.
- `gateway`: pulling extra live context out of Discord (channel history,
  member lookup) for the tools that ask for it.
- `lifecycle`: the background sweepers started once the gateway is ready.

Alongside them, `module_actions`, `module_events`, and `module_interactions` are
the module-API ports at this boundary: the `DiscordActions`/`TrustLookup`
implementations, gateway event normalization, and the interaction router.

Named `discord_adapter` rather than `discord` because that would shadow
discord.py, and kept out of `app/` because the composition root and the
platform boundary are different jobs. `bot.py` stays the entry point.

Nothing is re-exported here: the modules here have distinct roles and
importing one should not drag in the others.
"""

from __future__ import annotations
