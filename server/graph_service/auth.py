"""Bearer-token auth for the graph endpoints.

Every endpoint except /healthcheck requires GRAPHITI_API_KEY as a bearer token, with no way to
switch it off: the API writes to a shared graph and spends the deployment's OpenAI key on every
episode. config.py requires the key at startup, so this module can assume there is one.

/healthcheck stays public — Render polls it to decide the service is live.
"""

import hashlib
import secrets
import time
from collections import deque
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from graph_service.config import ZepEnvDep

# auto_error=False so a missing or non-Bearer header arrives as None. On its own HTTPBearer
# answers 403; the right answer is a 401 with WWW-Authenticate, which tells the client what to
# send instead of just no.
_bearer = HTTPBearer(auto_error=False, description='The GRAPHITI_API_KEY set on this service.')

# Rejections this service will issue in a window before answering 429 instead, so the key can't
# be worked through and the URL isn't a free scanner target.
#
# Global rather than per-IP: behind Render's proxy every request arrives from the proxy's
# address, and trusting the client-supplied X-Forwarded-For would hand an attacker the bucket
# key. A global budget can't be used to lock a real client out, because require_api_key checks
# the key *before* the budget — only wrong keys compete for it.
#
# In-process, so N instances allow N times this. Fine at any N against a 16-character minimum
# keyspace, and the point of keeping it in memory: a shared counter would put FalkorDB on the
# auth path, so the graph store being slow would become the API being closed.
MAX_FAILED_AUTH = 10
FAILED_AUTH_WINDOW_SECONDS = 60

# Timestamps of the last MAX_FAILED_AUTH rejections, oldest first. maxlen does the eviction, so
# the budget is spent exactly when the deque is full and its oldest entry is still inside the
# window. monotonic, so an NTP correction can't resize the window. No lock: every access below
# happens between awaits on a single event loop.
_recent_rejections: deque[float] = deque(maxlen=MAX_FAILED_AUTH)


def _seconds_until_budget_frees(now: float) -> float | None:
    """How long until this service will issue another rejection, or None if it will now.

    Positive only when the budget is spent, which is what makes it usable as both the check and
    the Retry-After value.
    """
    if len(_recent_rejections) < MAX_FAILED_AUTH:
        return None
    elapsed = now - _recent_rejections[0]
    return FAILED_AUTH_WINDOW_SECONDS - elapsed if elapsed < FAILED_AUTH_WINDOW_SECONDS else None


def _digest(value: str) -> bytes:
    """A fixed-width, constant-time-comparable stand-in for the key.

    Encoded because Starlette decodes header values as latin-1, so what arrives can be any str,
    and hashlib takes bytes. Hashed so both sides are always 32 bytes: compare_digest returns
    immediately on a length mismatch, which would leak the key's length to anyone willing to
    time the rejections.
    """
    return hashlib.sha256(value.encode()).digest()


async def require_api_key(
    settings: ZepEnvDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    # Before the budget, so a caller holding the right key is never rate limited — see
    # MAX_FAILED_AUTH. compare_digest, so a wrong key can't be recovered a byte at a time from
    # how long it takes to reject; it needs both sides to exist, hence the guard.
    if credentials is not None and secrets.compare_digest(
        _digest(credentials.credentials), _digest(settings.graphiti_api_key)
    ):
        return

    now = time.monotonic()
    retry_after = _seconds_until_budget_frees(now)
    if retry_after is not None:
        # Not recorded: counting a request the budget already refused would hold the window
        # open for as long as the traffic lasts, so a wrong key would never get its 401 back.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many failed authentication attempts. Try again shortly.',
            # Ceil, so a sub-second remainder is never advertised as a 0-second wait.
            headers={'Retry-After': str(int(retry_after) + 1)},
        )

    _recent_rejections.append(now)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail='Invalid or missing API key. Send it as: Authorization: Bearer <GRAPHITI_API_KEY>',
        # Required on a 401 by RFC 9110, and it tells the client which scheme to retry.
        headers={'WWW-Authenticate': 'Bearer'},
    )
