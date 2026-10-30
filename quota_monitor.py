"""Compatibility alias for agent_hub.quota.monitor."""

import sys

from agent_hub.quota import monitor as _implementation

sys.modules[__name__] = _implementation
