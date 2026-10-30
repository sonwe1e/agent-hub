"""Compatibility alias for agent_hub.gateway.observation."""

import sys

from agent_hub.gateway import observation as _implementation

sys.modules[__name__] = _implementation
