"""Compatibility alias for agent_hub.sessions.titles."""

import sys

from agent_hub.sessions import titles as _implementation

sys.modules[__name__] = _implementation
