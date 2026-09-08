"""
Segurança de autenticação, de acesso e RBAC.

- Hash de senhas com bcrypt via passlib (nunca em texto puro).
- Tokens JWT (HS256) via python-jose; segredo/expiração vêm de `config.settings`.
- Dependência `get_current_user` autentica (401) exigindo `Authorization: Bearer <token>`.
- Dependência `require_role(papel)` aplica RBAC validando o claim `role`
  do payload do token (403 quando o papel é insuficiente).
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

import models
from config import settings
from database import get_db

logger = logging.getLogger("orion.security")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

#: tokenUrl aponta para o endpoint de login (contrato exibido na documentação)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")


# ---------------------------------------------------------------------------
# Senhas (bcrypt)
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """Gera o hash bcrypt de uma senha em texto puro."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """
    Verifica uma senha contra o hash armazenado (timing-safe).

    Se o valor armazenado NÃO for um hash válido (ex.: senhas legadas
    gravadas em texto puro em bases antigas), retorna False em vez de
    propagar `UnknownHashError` — a migração transparente acontece no
    endpoint de login.
    """
    try:
        return pwd_context.verify(plain, hashed)
    # UnknownHashError (subclasse de ValueError) é lançado quando o valor
    # armazenado não é um hash identificável (ex.: senha legada em texto puro).
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def create_access_token(user: models.User) -> str:
    """
    Emite um token HS256 para o usuário.

    Claims: `sub` (id), `exp` (expiração), `email` e `role` — o claim
    `role` é o validado pelo RBAC (`require_role`) a cada request.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": str(user.id),
        "exp": expire,
        "email": user.email,
        "role": user.role,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


# ---------------------------------------------------------------------------
# Contexto de autenticação (usuário + payload do token)
# ---------------------------------------------------------------------------
@dataclass
class AuthContext:
    """Usuário autenticado + payload bruto do JWT (claims como `role`)."""

    user: models.User
    payload: dict


def _credentials_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciais inválidas ou expiradas.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_token(token: str) -> dict:
    """Decodifica/valida a assinatura e a expiração do token ({} em falha)."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except (JWTError, ValueError, TypeError):
        logger.debug("Token rejeitado (decode falhou).")
        return {}


def get_auth_context(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> AuthContext:
    """
    Dependência base de autenticação: decodifica o Bearer token e carrega
    o usuário. Qualquer falha (ausente, malformado, expirado, sub
    inexistente) resulta em `401 Unauthorized`.
    """
    payload = _decode_token(token)
    if not payload:
        raise _credentials_error()
    try:
        user_id = int(payload.get("sub", 0))
    except (ValueError, TypeError):
        raise _credentials_error()
    if user_id <= 0:
        raise _credentials_error()

    user = db.get(models.User, user_id)
    if user is None:
        raise _credentials_error()
    return AuthContext(user=user, payload=payload)


def get_current_user(ctx: AuthContext = Depends(get_auth_context)) -> models.User:
    """Dependência de autenticação simples: devolve o usuário logado."""
    return ctx.user


# ---------------------------------------------------------------------------
# RBAC — Controle de Acesso Baseado em Papéis
# ---------------------------------------------------------------------------
def _role_level(role: Optional[str]) -> int:
    """
    Nível do papel segundo `settings.role_hierarchy`.

    Fail-closed: papel ausente, vazio ou desconhecido cai no nível de
    "user" (o mais restritivo disponível na hierarquia padrão).
    """
    user_level = settings.role_hierarchy.get("user", 1)
    if not role:
        return user_level
    return settings.role_hierarchy.get(role.strip().lower(), user_level)


def require_role(required_role: str) -> Callable[..., models.User]:
    """
    Fábrica de dependência RBAC.

    Valida o campo `role` extraído do PAYLOAD DO TOKEN JWT do usuário
    atual (claim capturado no login) contra a hierarquia configurável:

    - nível do usuário >= nível de `required_role` → acesso liberado;
    - o papel de nível máximo (ex.: ``"admin"``) tem acesso GLOBAL a
      qualquer rota protegida por papéis específicos;
    - papel insuficiente → `403 Forbidden` com mensagem clara.

    Uso:
        @app.delete("/api/farms/{farm_id}")
        def delete_farm(..., user: models.User = Depends(require_role("admin"))):
            ...
    """
    def checker(ctx: AuthContext = Depends(get_auth_context)) -> models.User:
        user_level = _role_level(ctx.payload.get("role", "user"))
        required_level = _role_level(required_role)
        if user_level < required_level:
            actual_role = ctx.payload.get("role", "user")
            logger.info("RBAC: acesso negado — usuário %s (role '%s') em rota que exige '%s'.",
                        ctx.user.email, actual_role, required_role)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Acesso negado: a rota exige o papel '{required_role}' "
                    f"(seu papel atual: '{actual_role}')."
                ),
            )
        return ctx.user

    checker.__name__ = f"require_role_{required_role.strip().lower().replace(' ', '_')}"
    return checker
