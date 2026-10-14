"""数据面 JWT 认证（MCP OAuth 2.1 resource server 角色，2025-06-18 及之后 spec 方向）。

两种模式（DataPlaneAuthn.mode="jwt" 时二选一，由 jwks_url 是否配置决定）：

1. **外部 IdP 模式**（jwks_url + issuer）：网关只做 resource server——按 JWKS 公钥
   验签（RS256/ES256…），校验 iss/aud/exp。授权服务器是企业已有 IdP（Keycloak/
   Auth0/…），Claude Code 等客户端可通过 RFC 9728 发现走完整 OAuth 2.1 流程。
2. **内置签发模式**（无 jwks_url）：网关用 `OPENAPI_MCP_HUB_JWT_SECRET`（HS256，
   仅存在于网关进程环境）签发短期 token（管理面 POST /services/{name}/token）。
   aud 绑定服务名，到期自动失效；适合不想引入 IdP 的内网部署。

mode="static"（默认）维持既有 X-Api-Key 行为，jwt 模式下 key 认证关闭。

安全边界：密钥/私钥永不落盘——外部模式只存 JWKS URL（公钥可拉取），
内置模式密钥只在环境变量。
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import jwt
from jwt import PyJWKClient

from .errors import RegistrationError

# 内置签发模式的 claims 约定
BUILTIN_ISSUER = "openapi-mcp-hub"
DEFAULT_ALGORITHMS = ("RS256", "ES256")
BUILTIN_ALGORITHM = "HS256"

# PyJWKClient 的 JWKS 缓存秒数（公钥可缓存；轮换靠 kid 变化触发重拉）
JWKS_CACHE_SECONDS = 300

JWT_SECRET_ENV = "OPENAPI_MCP_HUB_JWT_SECRET"

# 签发上限：宁可让运维频繁续，也不发长命 token
MAX_TTL_S = 24 * 3600
DEFAULT_TTL_S = 3600


class JwtAuthnError(ValueError):
    """token 无效（401 语义，附 error code 供 WWW-Authenticate）。"""


def builtin_secret() -> str | None:
    """内置签发模式的网关级 HMAC 密钥（环境变量，永不落盘）。"""
    return os.environ.get(JWT_SECRET_ENV) or None


def issue_builtin_token(service_name: str, *, ttl_s: int = DEFAULT_TTL_S) -> tuple[str, int]:
    """内置模式签发短期 token：aud 绑定服务名，iss 固定，带 jti 便于日志关联。

    返回 (token, 实际生效秒数)——ttl 会被夹在 [60, 24h] 区间。
    """
    secret = builtin_secret()
    if not secret:
        raise RegistrationError(
            f"内置 JWT 签发需要网关环境变量 {JWT_SECRET_ENV}（>=32 字节随机串，仅存在于进程环境）"
        )
    ttl = max(60, min(int(ttl_s or DEFAULT_TTL_S), MAX_TTL_S))
    now = int(time.time())
    claims = {
        "iss": BUILTIN_ISSUER,
        "aud": service_name,
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(claims, secret, algorithm=BUILTIN_ALGORITHM), ttl


def _get_jwk_client(jwks_url: str) -> PyJWKClient:
    """按 URL 复用 PyJWKClient（每个外部 IdP 模式服务一个实例，进程级缓存 JWKS）。"""
    client = _JWK_CLIENTS.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=JWKS_CACHE_SECONDS)
        if len(_JWK_CLIENTS) > 64:  # 上限防注册滥用撑爆内存
            _JWK_CLIENTS.clear()
        _JWK_CLIENTS[jwks_url] = client
    return client


_JWK_CLIENTS: dict[str, PyJWKClient] = {}


def verify_token(
    token: str,
    *,
    service_name: str,
    authn: Any,  # DataPlaneAuthn（pydantic 模型；避免循环导入用鸭子类型）
) -> dict[str, Any]:
    """校验数据面 Bearer token；通过则返回 claims，失败抛 JwtAuthnError。

    内置模式：HS256 + 网关环境密钥，iss/aud 强校验。
    外部模式：JWKS 验签 + issuer/audience 校验（audience 未配置时只验签名与 exp）。
    """
    if getattr(authn, "jwks_url", None):
        try:
            client = _get_jwk_client(authn.jwks_url)
            key = client.get_signing_key_from_jwt(token).key
            options = {"require": ["exp", "iat"]}
            kwargs: dict[str, Any] = {"algorithms": list(authn.algorithms or DEFAULT_ALGORITHMS)}
            if getattr(authn, "issuer", None):
                kwargs["issuer"] = authn.issuer
                options["verify_iss"] = True
            if getattr(authn, "audience", None):
                kwargs["audience"] = authn.audience
                options["verify_aud"] = True
            else:
                options["verify_aud"] = False
            return jwt.decode(token, key, options=options, **kwargs)
        except jwt.PyJWTError as exc:
            raise JwtAuthnError(str(exc)) from exc
    # 内置模式
    secret = builtin_secret()
    if not secret:
        raise JwtAuthnError(f"网关未配置 {JWT_SECRET_ENV}，无法校验内置 token")
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[BUILTIN_ALGORITHM],
            issuer=BUILTIN_ISSUER,
            audience=service_name,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise JwtAuthnError(str(exc)) from exc


def www_authenticate_header(service_name: str, *, error: str = "invalid_token") -> str:
    """401 响应的 WWW-Authenticate 头（RFC 9728 资源元数据指针，供客户端发现授权服务器）。"""
    return (
        f'Bearer error="{error}", '
        f'resource_metadata="/.well-known/oauth-protected-resource/mcp/{service_name}"'
    )
