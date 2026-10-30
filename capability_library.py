"""Compatibility alias for agent_hub.capabilities.library."""

import sys

from agent_hub.capabilities import library as _implementation

sys.modules[__name__] = _implementation
