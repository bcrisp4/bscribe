"""Bearer-token authentication.

Every job endpoint depends on :func:`require_token`; the token table is
checked per request, so revocation via the admin CLI takes effect
immediately, with no restart (docs/design.md — Admin CLI, Security).

Presented token values are never logged at any level; only the resolved
token's label may appear in logs (docs/design.md — Privacy).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bscribe.domain.tokens import hash_secret

if TYPE_CHECKING:
    from bscribe.domain.models import Token
    from bscribe.domain.ports import TokenStorePort

# auto_error=False: missing-header, wrong-scheme, and unknown-token requests
# all fall through to the single _unauthorized() below, so the three cases
# are byte-identical on the wire — no oracle for probing callers.
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    # Detail is deliberately generic: never echo anything token-derived.
    return HTTPException(
        status_code=401,
        detail="Invalid or missing bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> Token:
    """Resolve the presented bearer token to its principal.

    Deliberately a sync ``def``: FastAPI runs it on the threadpool, keeping
    the blocking SQLite read off the event loop (docs/adr/0002).

    Args:
        request: Current request; the token store rides on ``app.state``.
        credentials: Parsed Authorization header, if one was presented.

    Returns:
        The authenticated token principal (job ownership key from M2).

    Raises:
        HTTPException: 401 with ``WWW-Authenticate: Bearer`` when the header
            is missing, malformed, or matches no stored token.
    """
    if credentials is None:
        raise _unauthorized()

    # app.state attributes are typed Any (Starlette State.__getattr__);
    # the declared annotation narrows it statically at zero runtime cost.
    store: TokenStorePort = request.app.state.token_store

    token = store.find_by_secret_hash(hash_secret(credentials.credentials))
    if token is None:
        raise _unauthorized()
    return token
