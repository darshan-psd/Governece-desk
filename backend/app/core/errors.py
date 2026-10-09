"""Typed API errors.

The Node backend encoded HTTP statuses as message prefixes (``"403:..."``). Here each status
has its own exception; every error is returned to the frontend as ``{"error": message}``.
"""
from __future__ import annotations


class ApiError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BadRequest(ApiError):
    status_code = 400


class Unauthorized(ApiError):
    status_code = 401


class Forbidden(ApiError):
    status_code = 403


class NotFound(ApiError):
    status_code = 404


class Conflict(ApiError):
    status_code = 409


class PayloadTooLarge(ApiError):
    status_code = 413
