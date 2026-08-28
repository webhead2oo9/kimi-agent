"""Per-guild settings for the kudos module.

Deployment settings (``settings.py``) are the operator's. Guild settings are
the guild's: staff of each server edit them, and the host stores them as
frontmatter in ``<CONFIG_DIR>/guild-modules/<guild_id>/reference_kudos.md``.

The host validates each guild's document against this schema and hands the
module a typed snapshot through ``ctx.guild_settings``. The module does not parse
the file itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from kimi_agent_module_api import GuildSettingsSchema
from kimi_agent_module_api.contracts import GuildSettingField

# Names are referenced from several places (commands, the digest job, tests),
# so they are constants.
FIELD_DIGEST_CHANNEL = "digest_channel_id"
FIELD_GIVER_TIER = "giver_min_tier"
FIELD_ALLOW_SELF_THANKS = "allow_self_thanks"


def _validate(values: Mapping[str, Any]) -> Sequence[str]:
    """Cross-field rules the per-field kinds cannot express.

    Returning a non-empty sequence marks the guild document invalid; the host
    then applies ``invalid_policy`` and surfaces the messages in ``/modules status``.
    """
    errors: list[str] = []
    # A digest is pointless when the guild has locked giving down to staff only:
    # the point of the digest is to celebrate member-to-member recognition.
    if values.get(FIELD_GIVER_TIER) == "staff" and values.get(FIELD_DIGEST_CHANNEL):
        errors.append("a digest channel makes no sense when only staff may give kudos")
    return errors


GUILD_SETTINGS = GuildSettingsSchema(
    fields=(
        GuildSettingField(
            name=FIELD_DIGEST_CHANNEL,
            kind="id",  # a Discord snowflake, normalized to int by the host
            help="Channel that receives the periodic kudos digest. Unset disables the digest.",
        ),
        GuildSettingField(
            name=FIELD_GIVER_TIER,
            kind="enum",
            choices=("member", "regular", "staff"),
            default="member",
            help="Lowest trust tier that may give kudos in this guild.",
        ),
        GuildSettingField(
            name=FIELD_ALLOW_SELF_THANKS,
            kind="bool",
            default=False,
            help="Whether the 'Thank back' button may be used by the original giver.",
        ),
    ),
    # The default policy, ``disable_guild``, takes the whole guild out of the
    # bot's active set when this document is broken. That is right for an
    # enforcement module that must fail closed; for a cosmetic feature it is
    # far too drastic, so a broken kudos document only disables kudos there.
    invalid_policy="disable_module",
    validate=_validate,
)

__all__ = [
    "FIELD_ALLOW_SELF_THANKS",
    "FIELD_DIGEST_CHANNEL",
    "FIELD_GIVER_TIER",
    "GUILD_SETTINGS",
]
