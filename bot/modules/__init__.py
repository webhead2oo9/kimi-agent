"""Runtime implementations of the module API services.

Contracts live in ``kimi_agent_module_api``; this package implements them and
is composed by ``app``. External modules never import it; module tests may
use ``modules.testing`` once it exists.
"""

from __future__ import annotations
