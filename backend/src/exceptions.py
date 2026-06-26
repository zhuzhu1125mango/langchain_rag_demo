"""全局业务异常定义。

所有异常均继承自 FastAPI 的 HTTPException，便于统一返回标准 HTTP 响应，
同时通过 error_code 字段提供前端可识别的错误码。
"""

from fastapi import HTTPException, status


class AppException(HTTPException):
    """应用基础异常。"""

    def __init__(
        self,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail: str = "Internal Server Error",
        error_code: str = "INTERNAL_ERROR",
        headers: dict = None
    ):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code


class ResourceNotFoundException(AppException):
    """资源不存在异常（404）。"""

    def __init__(self, detail: str = "Resource not found"):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
            error_code="NOT_FOUND"
        )


class ValidationException(AppException):
    """参数校验失败异常（422）。"""

    def __init__(self, detail: str = "Validation error"):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
            error_code="VALIDATION_ERROR"
        )


class AuthenticationException(AppException):
    """认证失败异常（401）。"""

    def __init__(self, detail: str = "Authentication required"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            error_code="AUTHENTICATION_ERROR"
        )


class AuthorizationException(AppException):
    """权限不足异常（403）。"""

    def __init__(self, detail: str = "Permission denied"):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail,
            error_code="AUTHORIZATION_ERROR"
        )


class BadRequestException(AppException):
    """请求参数错误异常（400）。"""

    def __init__(self, detail: str = "Bad request"):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
            error_code="BAD_REQUEST"
        )