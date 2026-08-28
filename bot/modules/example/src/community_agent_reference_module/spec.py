"""The ``ModuleSpec``: identity, declarations, and load-time wiring.

This is the object the ``kimi_agent.modules`` entry point resolves to. The
host reads it in two phases:

1. **Preflight**, before any module code runs. It validates the declarations
   below (permissions, services, guild-settings schema, settings definition)
   and aborts startup with a named reason if anything is malformed.
2. **Load**, which calls ``create()`` with a ``ModuleLoadContext``. That is the
   moment to read prepared settings and register LLM tools. The returned
   object is started later, once the database is migrated.

Declaring less than you use fails at runtime (``UndeclaredDiscordAction``,
``EventTopicError``); declaring more than you use is merely misleading in
``/modules manifest``. Keep the two in step.
"""

from __future__ import annotations

from kimi_agent_module_api import (
    AppModule,
    ModuleLoadContext,
    ModulePermissions,
    ModuleSpec,
    ServiceDeclaration,
    TrustTier,
)
from kimi_agent_module_api.events import TOPIC_MEMBER_REMOVE

from community_agent_reference_module.guild_settings import GUILD_SETTINGS
from community_agent_reference_module.module import (
    MODULE_NAME,
    SERVICE_NAME,
    SERVICE_VERSION,
    TOOL_GIVE,
    TOOL_LEADERBOARD,
    KudosModule,
)
from community_agent_reference_module.settings import SETTINGS, KudosSettings

# What the LLM sees. Parameters are JSON Schema; keep ``additionalProperties``
# false so a hallucinated argument is rejected instead of silently ignored.
GIVE_PARAMETERS = {
    "type": "object",
    "properties": {
        "user": {
            "type": "string",
            "description": "The recipient's Discord user id, or an @mention such as <@123>.",
        },
        "reason": {
            "type": "string",
            "description": "One sentence saying what they did (under 200 characters).",
        },
    },
    "required": ["user", "reason"],
    "additionalProperties": False,
}

LEADERBOARD_PARAMETERS = {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "description": "Window in days, 1-365. Defaults to 30.",
        }
    },
    "required": [],
    "additionalProperties": False,
}


def create(ctx: ModuleLoadContext) -> AppModule:
    """Build the lifecycle object and register the module's LLM tools."""
    # The host has already merged the environment, the dotenv, and the
    # operator override file into one validated instance of our model.
    settings = ctx.settings_for(KudosSettings)
    module = KudosModule(settings)

    # A core tool: visible to every qualifying tier as soon as the turn starts.
    ctx.registry.register(
        TOOL_GIVE,
        "Give kudos to a server member on behalf of the person you are talking to.",
        GIVE_PARAMETERS,
        module.tool_give,
        min_tier=TrustTier.MEMBER,
    )
    # A searchable tool: hidden until the model activates it with browse_tools,
    # which keeps rarely needed tools out of every prompt.
    ctx.registry.register(
        TOOL_LEADERBOARD,
        "Show which members received the most kudos recently.",
        LEADERBOARD_PARAMETERS,
        module.tool_leaderboard,
        min_tier=TrustTier.MEMBER,
        searchable=True,
    )
    # Gerund phrases shown in the "Kimi is ..." activity line while a tool runs.
    ctx.register_tool_labels(
        {
            TOOL_GIVE: "Giving kudos",
            TOOL_LEADERBOARD: "Checking the kudos leaderboard",
        }
    )
    return module


SPEC = ModuleSpec(
    name=MODULE_NAME,
    version="1.0.0",
    create=create,
    settings=SETTINGS,
    guild_settings=GUILD_SETTINGS,
    permissions=ModulePermissions(
        # The digest posts to a channel; nothing else touches Discord directly.
        discord_actions=frozenset({"send_message"}),
        # Core topics we subscribe to. Our own ``reference_kudos.*`` namespace
        # is implicit and must not be listed.
        event_topics=(TOPIC_MEMBER_REMOVE,),
    ),
    # Sibling modules can depend on us and call ``ctx.services.get("kudos.board", 1)``.
    provides=(ServiceDeclaration(SERVICE_NAME, SERVICE_VERSION),),
)

__all__ = ["GIVE_PARAMETERS", "LEADERBOARD_PARAMETERS", "SPEC", "create"]
