"""Write OGRL integrations (RFC 0005). Standard library only, so packages ship no dependencies."""

__version__ = "0.1.0a8"

from ogrl_integration.app import Context, Integration  # noqa: E402
from ogrl_integration.errors import (  # noqa: E402
    AuthFailed,
    Incomplete,
    IntegrationError,
    InvalidConfig,
    PermissionDenied,
    RateLimited,
    VendorError,
)
from ogrl_integration.http import Http, Response  # noqa: E402
from ogrl_integration.outcomes import Identity, Outcome, Subject, error, fail, pass_  # noqa: E402

__all__ = [
    "AuthFailed",
    "Context",
    "Http",
    "Identity",
    "Incomplete",
    "Integration",
    "IntegrationError",
    "InvalidConfig",
    "Outcome",
    "PermissionDenied",
    "RateLimited",
    "Response",
    "Subject",
    "VendorError",
    "error",
    "fail",
    "pass_",
]
