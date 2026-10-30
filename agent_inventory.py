"""Compatibility alias for agent_hub.agents.inventory."""

import sys

from agent_hub.agents import inventory as _implementation

sys.modules[__name__] = _implementation
