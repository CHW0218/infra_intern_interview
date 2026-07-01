class ProviderError(Exception):
    """Base for all normalized provider failures. Always names its provider."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        self.message = message
        super().__init__(f"[{provider}] {message}")


class AuthError(ProviderError): ...
class NotFoundError(ProviderError): ...
class CapacityError(ProviderError): ...
class ReservedInstanceError(ProviderError): ...
class InvalidArgumentError(ProviderError): ...
class PreconditionError(ProviderError): ...
class UnsupportedOperationError(ProviderError): ...


class FleetUnfulfilledError(Exception):
    """Fleet request could not be satisfied; rollback already attempted."""

    def __init__(self, message: str, rolled_back: int, undestroyable: list):
        self.rolled_back = rolled_back
        self.undestroyable = undestroyable
        super().__init__(message)
