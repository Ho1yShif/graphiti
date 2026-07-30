"""Bearer-token auth for the graph endpoints.

Every endpoint except /healthcheck requires GRAPHITI_API_KEY as a bearer token. It is not
optional and there is no switch to turn it off: the API writes to a shared graph and spends
the deployment's OpenAI key on every episode. The key is required at startup (see
config.py), so this dependency can assume there is one — Render generates the value when it
first creates the service, and compose supplies a local one.

Keep the key ASCII, as the generated one is. Header values are latin-1 on the wire and
clients encode anything outside it inconsistently, so a key with an accent in it may never
match what arrives here — it fails closed, locking you out rather than letting anyone in.

/healthcheck stays public: Render polls it to decide the service is live, and it exposes
nothing about the graph.
"""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from graph_service.config import ZepEnvDep

# auto_error=False so a missing or non-Bearer header arrives here as None instead of the
# scheme raising: on its own it answers 403, and the header being absent is a 401 with
# WWW-Authenticate — the response that tells a client what to send instead of just no.
_bearer = HTTPBearer(auto_error=False, description='The GRAPHITI_API_KEY set on this service.')


async def require_api_key(
    settings: ZepEnvDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    # compare_digest rather than ==, so a wrong key can't be recovered a byte at a time
    # from how long it takes to reject. It needs both sides to exist, hence the guard.
    #
    # Compared as bytes: compare_digest raises TypeError on a non-ASCII str, and either
    # side can be one — Starlette decodes headers as latin-1, and nothing stops an
    # operator rotating the key to a value with an accent in it. Encoding first makes both
    # a 401 rather than a 500.
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), settings.graphiti_api_key.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or missing API key. Send it as: Authorization: Bearer <GRAPHITI_API_KEY>',
            # Required on a 401 by RFC 9110, and it tells the client which scheme to retry.
            headers={'WWW-Authenticate': 'Bearer'},
        )
