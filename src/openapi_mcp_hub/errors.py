"""统一异常体系。"""


class ApiToMcpError(Exception):
    """项目内异常基类。"""


class SpecError(ApiToMcpError):
    """openapi 规范不合法 / 不受支持 / 无法解析。"""

    def __init__(self, message: str, issues: list[str] | None = None):
        super().__init__(message)
        self.issues = issues or []


class RegistrationError(ApiToMcpError):
    """注册请求不合法（名称、base_url、工具数超限等）。"""

    def __init__(self, message: str, issues: list[str] | None = None):
        super().__init__(message)
        self.issues = issues or []


class ServiceConflictError(ApiToMcpError):
    """服务名已存在。"""


class ServiceNotFoundError(ApiToMcpError):
    """服务不存在。"""
