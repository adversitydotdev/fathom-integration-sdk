"""Write Fathom integrations (RFC 0005). Standard library only, so packages ship no dependencies."""

__version__ = "0.1.0a13"

from fathom_integration.app import Context, Integration  # noqa: E402
from fathom_integration.errors import (  # noqa: E402
    AuthFailed,
    Incomplete,
    IntegrationError,
    InvalidConfig,
    PermissionDenied,
    RateLimited,
    VendorError,
)
from fathom_integration.http import Http, Response  # noqa: E402
from fathom_integration.outcomes import Identity, Outcome, Subject, error, fail, pass_  # noqa: E402

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
