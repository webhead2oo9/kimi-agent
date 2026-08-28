# Community Agent Module API

This package contains the stable, host-independent contracts used to build
trusted, installed assistant modules. It deliberately contains no bot runtime,
Discord client, database implementation, or module loader.

Modules expose a `ModuleSpec` through the `community_agent.modules` Python entry
point group. See the main repository's reference module for a complete example.
