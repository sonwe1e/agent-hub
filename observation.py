"""Compatibility alias for agent_hub.observability.store."""

import sys

from agent_hub.observability import store as _implementation

sys.modules[__name__] = _implementation
