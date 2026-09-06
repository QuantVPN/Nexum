"""Domain exceptions. The API layer maps these to HTTP status codes."""


class NexumError(Exception):
    """Base class for domain errors."""

    status_code = 400


class NotFoundError(NexumError):
    status_code = 404


class ConflictError(NexumError):
    status_code = 409


class ValidationError(NexumError):
    status_code = 422


class PermissionDeniedError(NexumError):
    status_code = 403


class AuthenticationError(NexumError):
    status_code = 401
