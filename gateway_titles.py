"""Compatibility alias for agent_hub.sessions.gateway_titles."""

import sys

from agent_hub.sessions import gateway_titles as _implementation

sys.modules[__name__] = _implementation
