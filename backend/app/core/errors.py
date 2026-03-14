"""
Standardized error responses.

All API errors follow this format:
{
    "error": {
        "code": "BUSINESS_SUSPENDED",
        "message": "This business account has been suspended.",
        "details": {}
    }
}
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class AppError(Exception):
    """Base application error that maps to a structured API response."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


# ── Common errors ────────────────────────────────────────────────────────────

class NotFoundError(AppError):
    def __init__(self, resource: str, resource_id: str | None = None):
        super().__init__(
            code=f"{resource.upper()}_NOT_FOUND",
            message=f"{resource} not found" + (f": {resource_id}" if resource_id else ""),
            status_code=404,
        )


class BusinessSuspendedError(AppError):
    def __init__(self, reason: str | None = None):
        super().__init__(
            code="BUSINESS_SUSPENDED",
            message="This business account has been suspended.",
            status_code=403,
            details={"reason": reason} if reason else None,
        )


class RateLimitError(AppError):
    def __init__(self, limit_type: str):
        super().__init__(
            code="RATE_LIMIT_EXCEEDED",
            message=f"Rate limit exceeded: {limit_type}",
            status_code=429,
        )


class DailyLimitError(AppError):
    def __init__(self, limit_type: str, limit: int):
        super().__init__(
            code="DAILY_LIMIT_EXCEEDED",
            message=f"Daily {limit_type} limit reached ({limit})",
            status_code=429,
            details={"limit_type": limit_type, "limit": limit},
        )


class InvalidTransitionError(AppError):
    def __init__(self, current: str, requested: str):
        super().__init__(
            code="INVALID_STATUS_TRANSITION",
            message=f"Cannot transition from {current} to {requested}",
            status_code=422,
            details={"current_status": current, "requested_status": requested},
        )


class DuplicateError(AppError):
    def __init__(self, resource: str, field: str):
        super().__init__(
            code=f"{resource.upper()}_DUPLICATE",
            message=f"A {resource} with this {field} already exists",
            status_code=409,
        )


# ── Exception handlers (register on FastAPI app) ────────────────────────────

def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI app."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "details": {"errors": exc.errors()},
                }
            },
        )
