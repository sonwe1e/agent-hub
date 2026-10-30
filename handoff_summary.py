"""Compatibility alias for agent_hub.sessions.summary."""

import sys

from agent_hub.sessions import summary as _implementation

sys.modules[__name__] = _implementation
