"""The service layer: the use cases every interface calls (ARCHITECTURE sections 2, 4, 11, 14).

`Services` offers `search`, `get_definition`, `list_related`, and `compile_query`. Each takes a
`Caller` and applies the permission checks of section 11, validates its input, and writes one
`tool_call_log` row. The MCP server (WP-13) and the web app (WP-16 onwards) call these and hold
no logic of their own.
"""

from makna.services.caller import CHANNELS, ROLES, Caller
from makna.services.errors import PermissionDenied, ServiceError, ServiceErrorCode
from makna.services.factory import build_services
from makna.services.service import Services

__all__ = [
    "CHANNELS",
    "ROLES",
    "Caller",
    "PermissionDenied",
    "ServiceError",
    "ServiceErrorCode",
    "Services",
    "build_services",
]
