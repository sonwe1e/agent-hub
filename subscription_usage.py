"""Compatibility alias for agent_hub.quota.usage."""

import sys

from agent_hub.quota import usage as _implementation

sys.modules[__name__] = _implementation
