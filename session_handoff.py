"""Compatibility alias for agent_hub.sessions.handoff."""

import sys

from agent_hub.sessions import handoff as _implementation

sys.modules[__name__] = _implementation
