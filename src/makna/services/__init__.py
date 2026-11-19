"""The service layer: the use cases every interface calls (ARCHITECTURE sections 2, 4, 11, 14).

`Services` offers `search`, `get_definition`, `list_related`, `compile_query`, and `resolve`. Each
takes a `Caller` and applies the permission checks of section 11, validates its input, and
writes one `tool_call_log` row. The MCP server (WP-13) and the web app (WP-16 onwards) call these
and hold no logic of their own.
"""

from makna.resolve import Resolution
from makna.services.caller import CHANNELS, ROLES, Caller
from makna.services.errors import (
    LifecycleErrorCode,
    PermissionDenied,
    ServiceError,
    ServiceErrorCode,
)
from makna.services.factory import build_services
from makna.services.service import (
    Services,
    authenticate_bearer_token,
    compile_on_snapshot,
    require_catalog,
    resolve_on_snapshot,
)

__all__ = [
    "CHANNELS",
    "ROLES",
    "Caller",
    "LifecycleErrorCode",
    "PermissionDenied",
    "Resolution",
    "ServiceError",
    "ServiceErrorCode",
    "Services",
    "authenticate_bearer_token",
    "build_services",
    "compile_on_snapshot",
    "require_catalog",
    "resolve_on_snapshot",
]
