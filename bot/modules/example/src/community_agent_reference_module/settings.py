"""Deployment-wide settings for the kudos module.

Module settings are ordinary ``pydantic-settings`` models. The host reads them
from the same dotenv file as its own settings, so every field is available as
``REFERENCE_KUDOS_<FIELD>`` in the environment. On top of that, the host lets
the operator override the fields listed in ``exposed`` from a Markdown file at
``<CONFIG_DIR>/modules/reference_kudos.md`` without a restart-and-redeploy cycle.

Two rules the host enforces when it validates this definition at startup:

1. Every field on the model must be classified: either in ``exposed`` or in
   ``environment_only``. An unclassified field fails startup, so a new secret
   can never leak into the operator-editable file by omission.
2. Exposed fields may not look like credentials, endpoints, or paths, and an
   exposed numeric field must declare a ``minimum``.
"""

from __future__ import annotations

from kimi_agent_module_api import ModuleSetting, ModuleSettingsDefinition
from pydantic_settings import BaseSettings, SettingsConfigDict


class KudosSettings(BaseSettings):
    """Values that apply to every guild this deployment serves."""

    # ``extra="ignore"`` matters: the shared dotenv holds the host's variables
    # and every other module's, and pydantic would otherwise reject them.
    model_config = SettingsConfigDict(env_prefix="REFERENCE_KUDOS_", extra="ignore")

    # How many kudos one person may give per guild in a rolling 24 hours.
    daily_limit: int = 5
    # Rows shown by the leaderboard tool, the ``/kudos top`` command, and the digest.
    board_size: int = 10
    # How often the scheduled digest job runs. Weekly by default.
    digest_interval_seconds: int = 7 * 24 * 60 * 60


SETTINGS = ModuleSettingsDefinition(
    # Must equal ``ModuleSpec.name``; it names the override file and the env prefix's owner.
    name="reference_kudos",
    label="Kudos",
    model=KudosSettings,
    # Every field is safe to hand to an operator, so all of them are exposed.
    # A field holding an API token would instead go in ``environment_only``.
    exposed=(
        ModuleSetting(
            field="daily_limit",
            label="Daily limit",
            help="Kudos one member may give per guild in any 24-hour window.",
            minimum=1,
        ),
        ModuleSetting(
            field="board_size",
            label="Leaderboard size",
            help="How many members the leaderboard and digest list.",
            minimum=1,
        ),
        ModuleSetting(
            field="digest_interval_seconds",
            label="Digest interval (seconds)",
            help="How often the kudos digest is posted to each guild's digest channel.",
            minimum=60,
        ),
    ),
)

__all__ = ["SETTINGS", "KudosSettings"]
