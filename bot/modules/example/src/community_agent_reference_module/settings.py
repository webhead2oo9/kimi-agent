"""Operator configuration owned by the reference module."""

from pydantic_settings import BaseSettings, SettingsConfigDict

from kimi_agent_module_api import ModuleSetting, ModuleSettingsDefinition


class ReferenceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REFERENCE_GREETER_", extra="ignore")

    greeting: str = "Hello"


SETTINGS = ModuleSettingsDefinition(
    name="reference_greeter",
    label="Reference greeter",
    model=ReferenceSettings,
    exposed=(
        ModuleSetting(
            field="greeting",
            label="Greeting",
            help="The opening word used by reference_greet.",
        ),
    ),
)

__all__ = ["SETTINGS", "ReferenceSettings"]
