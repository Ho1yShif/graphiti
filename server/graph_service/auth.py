"""Bearer-token auth for the graph endpoints.

Every endpoint except /healthcheck requires GRAPHITI_API_KEY as a bearer token. It is not
optional and there is no switch to turn it off: the API writes to a shared graph and spends
the deployment's OpenAI key on every episode. The key is required at startup (see
config.py), so this dependency can assume there is one — Render generates the value when it
first creates the service, and compose supplies a local one.

The configured key is constrained to printable ASCII by config.py, because header values go
over the wire as latin-1 and clients encode anything outside it inconsistently — a key with
an accent in it would authenticate for some clients and not others. What arrives in the
header is still arbitrary, though, which is why _digest below encodes both sides itself.

/healthcheck stays public: Render polls it to decide the service is live, and it exposes
nothing about the graph.

Rejections are also budgeted, so the key cannot be brute-forced and the endpoint cannot be
used as a free scanner target — see MAX_FAILED_AUTH.
"""

import hashlib
import secrets
import time
from collections import deque
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from graph_service.config import ZepEnvDep

# auto_error=False so a missing or non-Bearer header arrives here as None instead of the
# scheme raising: on its own it answers 403, and the header being absent is a 401 with
# WWW-Authenticate — the response that tells a client what to send instead of just no.
_bearer = HTTPBearer(auto_error=False, description='The GRAPHITI_API_KEY set on this service.')

# How many rejections this service will issue in a window before answering 429 instead.
#
# Deliberately not per-IP. On Render this process sits behind the platform's proxy, so the
# peer address every request arrives with is the proxy's — bucketing by it puts the whole
# internet in one bucket and calls it per-client. The alternative, trusting the
# client-supplied X-Forwarded-For, hands the attacker the bucket key: rotate the header and
# the limit never applies. There is no address here worth keying on, so this keys on nothing
# and budgets rejections globally.
#
# The usual objection to a global limiter is that one attacker can lock out everyone else.
# It does not apply, because a request carrying the right key is answered before the budget
# is consulted — see require_api_key. Only wrong keys compete for this, so the worst an
# attacker achieves is that other wrong keys get a 429 where they would have got a 401.
#
# In-process, so a service scaled to N instances allows N times this. That is fine at any N:
# 10 wrong keys a minute against a 16-character minimum keyspace is not a strategy. Keeping
# it in memory is the point — a shared counter would put FalkorDB on the auth path, so the
# graph store being slow would become the API being closed.
MAX_FAILED_AUTH = 10
FAILED_AUTH_WINDOW_SECONDS = 60

# The timestamps of the last MAX_FAILED_AUTH rejections, oldest first. maxlen does the
# eviction, so this is a sliding window that costs no bookkeeping: the deque is full and its
# oldest entry is still inside the window exactly when the budget is spent.
#
# monotonic, not wall clock, so an NTP correction can't retroactively widen or collapse the
# window. No lock: every read and write below happens between awaits on a single event loop,
# so no request observes a half-updated deque.
_recent_rejections: deque[float] = deque(maxlen=MAX_FAILED_AUTH)


def _seconds_until_budget_frees(now: float) -> float | None:
    """How long until this service will issue another rejection, or None if it will now.

    Returns a positive number only when the budget is spent, which is what makes it usable
    as both the check and the Retry-After value.
    """
    if len(_recent_rejections) < MAX_FAILED_AUTH:
        return None
    elapsed = now - _recent_rejections[0]
    return FAILED_AUTH_WINDOW_SECONDS - elapsed if elapsed < FAILED_AUTH_WINDOW_SECONDS else None


def _digest(value: str) -> bytes:
    """A fixed-width, constant-time-comparable stand-in for the key.

    Encoded before hashing because Starlette decodes header values as latin-1, so what
    arrives can be any str — and hashlib, like compare_digest, takes bytes. The configured
    side is printable ASCII by validation; the header is whatever a caller chose to send.

    Hashed rather than compared raw so both sides are always 32 bytes: compare_digest is
    constant-time in the *content* of two equal-length inputs but returns immediately on a
    length mismatch, which leaks the key's length to anyone willing to time the rejections.
    Length is not much of a secret next to the key itself, and Render's generated value is
    long — but this closes the oracle for a hand-rotated key at the cost of one line.
    """
    return hashlib.sha256(value.encode()).digest()


async def require_api_key(
    settings: ZepEnvDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    # Checked before the budget, not after: a caller holding the right key is never rate
    # limited, whatever anyone else is doing to this service. That is what makes a global
    # budget safe to run — see MAX_FAILED_AUTH.
    #
    # compare_digest rather than ==, so a wrong key can't be recovered a byte at a time
    # from how long it takes to reject. It needs both sides to exist, hence the guard.
    if credentials is not None and secrets.compare_digest(
        _digest(credentials.credentials), _digest(settings.graphiti_api_key)
    ):
        return

    now = time.monotonic()
    retry_after = _seconds_until_budget_frees(now)
    if retry_after is not None:
        # Not recorded in the deque. Counting a request the budget already refused would
        # keep the window open for as long as the traffic lasts, so a wrong key would never
        # get its 401 back — and the operator who mistyped theirs could not tell a rejected
        # key from a service under attack.
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
