"""Compatibility alias for agent_hub.gateway.transport."""

import sys

from agent_hub.gateway import transport as _implementation

sys.modules[__name__] = _implementation
