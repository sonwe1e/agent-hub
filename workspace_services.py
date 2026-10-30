"""Compatibility alias for agent_hub.workspace.services."""

import sys

from agent_hub.workspace import services as _implementation

sys.modules[__name__] = _implementation
