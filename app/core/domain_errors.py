"""Domain errors. Each carries an HTTP status and a stable machine-readable code."""


class DomainError(Exception):
    code: str = "DOMAIN_ERROR"
    status_code: int = 400
    default_message: str = "The request could not be processed."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class ValidationDomainError(DomainError):
    code = "VALIDATION_ERROR"
    status_code = 422
    default_message = "Request validation failed."


class InvalidAmountError(ValidationDomainError):
    code = "INVALID_AMOUNT"
    default_message = "Amount is invalid."


class NotFoundError(DomainError):
    code = "RESOURCE_NOT_FOUND"
    status_code = 404
    default_message = "Resource not found."


class PermissionDeniedError(DomainError):
    code = "PERMISSION_DENIED"
    status_code = 403
    default_message = "You are not allowed to perform this action."


class AccountDisabledError(PermissionDeniedError):
    code = "ACCOUNT_DISABLED"
    default_message = "This account is disabled."


class InvalidTransitionError(DomainError):
    code = "INVALID_TRANSITION"
    status_code = 409
    default_message = "This status change is not allowed."


class ConcurrencyConflictError(DomainError):
    code = "CONCURRENT_MODIFICATION"
    status_code = 409
    default_message = "The resource was changed by another request. Reload and try again."


class InvariantViolationError(DomainError):
    code = "INVARIANT_VIOLATION"
    status_code = 422
    default_message = "The request violates a business rule."


class AuthenticationError(DomainError):
    code = "AUTHENTICATION_FAILED"
    status_code = 401
    default_message = "Authentication failed."


class ServiceNotConfiguredError(DomainError):
    code = "SERVICE_NOT_CONFIGURED"
    status_code = 503
    default_message = "This service is not configured."


class IdempotencyKeyError(DomainError):
    code = "IDEMPOTENCY_KEY_INVALID"
    status_code = 400
    default_message = (
        "Idempotency-Key header is required: 8 to 128 characters, letters, digits, '.', '_', ':' or '-'."
    )


class IdempotencyKeyConflictError(DomainError):
    code = "IDEMPOTENCY_KEY_REUSED"
    status_code = 409
    default_message = "This Idempotency-Key was already used with a different request."


class IdempotencyDuplicateError(DomainError):
    code = "IDEMPOTENCY_IN_PROGRESS"
    status_code = 409
    default_message = "A request with this Idempotency-Key is being processed. Retry shortly."


class PaymentProviderError(DomainError):
    code = "PAYMENT_PROVIDER_UNAVAILABLE"
    status_code = 503
    default_message = "The payment provider is temporarily unavailable. Retry with the same Idempotency-Key."


class UnsupportedOperationError(DomainError):
    code = "UNSUPPORTED_OPERATION"
    status_code = 501
    default_message = "This operation is not supported by the configured payment provider."


class PayloadTooLargeError(DomainError):
    code = "PAYLOAD_TOO_LARGE"
    status_code = 413
    default_message = "The request body is too large."
