"""Compatibility alias for agent_hub.gateway.protocol."""

import sys

from agent_hub.gateway import protocol as _implementation

sys.modules[__name__] = _implementation
