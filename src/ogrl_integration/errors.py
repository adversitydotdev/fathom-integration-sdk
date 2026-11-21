"""Errors an integration raises. Each maps to an RFC 0005 section 7 error kind."""


class IntegrationError(Exception):
    kind = "vendor_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def data(self) -> dict[str, object]:
        return {"kind": self.kind}


class AuthFailed(IntegrationError):
    """The vendor rejected the credential."""

    kind = "auth_failed"


class PermissionDenied(IntegrationError):
    """The credential works but lacks a permission. Name it so the operator can grant it."""

    kind = "permission_denied"

    def __init__(self, message: str, permission: str | None = None) -> None:
        super().__init__(message)
        self.permission = permission

    def data(self) -> dict[str, object]:
        return {"kind": self.kind, **({"permission": self.permission} if self.permission else {})}


class RateLimited(IntegrationError):
    """The vendor asked to slow down. The host retries the operation after `retry_after`."""

    kind = "rate_limited"

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    def data(self) -> dict[str, object]:
        return {"kind": self.kind, **({"retry_after": self.retry_after} if self.retry_after else {})}


class VendorError(IntegrationError):
    kind = "vendor_error"


class InvalidConfig(IntegrationError):
    kind = "invalid_config"


class Incomplete(Exception):
    """Raise from a resource to stop early. Items already yielded are kept, marked incomplete."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
