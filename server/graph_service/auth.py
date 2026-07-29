"""Bearer-token auth for the graph endpoints.

The API writes to a shared graph and spends the deployment's OpenAI key on every
episode it ingests, so on Render it ships closed: render.yaml declares GRAPHITI_API_KEY
with generateValue, and Render mints a random value when it first creates the variable.
That value then persists — it is not re-rolled on later deploys, so rotating it is a
deliberate edit in the Dashboard rather than something that happens on its own.

Auth is still switchable, because the same code has to run where there is nothing to
protect. Leave GRAPHITI_API_KEY unset — as `docker compose up` does — and the dependency waves
every request through, so local development needs no header. Set it and every graph
endpoint requires one. There is no third state: a key that is present but blank is
treated as unset by the OptionalStr validator in config, not as a key equal to ''.

Keep the key ASCII, as the generated one is. HTTP header values are latin-1 on the wire
and clients encode non-ASCII bytes inconsistently, so a key with an accent in it may
never match what arrives here no matter what the caller sends — it fails closed, which
locks you out rather than letting anyone in.

/healthcheck is deliberately not covered. Render calls it to decide whether the
service is live, and it carries no graph data.
"""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from graph_service.config import Settings, get_settings

# auto_error=False so this returns None instead of raising on a missing or non-Bearer
# Authorization header. Both cases have to reach the body below, which is the only place
# that knows whether auth is switched on at all — letting the scheme raise would 403
# every unauthenticated request even on a deployment with no GRAPHITI_API_KEY set.
_bearer = HTTPBearer(auto_error=False, description='The GRAPHITI_API_KEY set on this service.')


# Settings are injected as a spelled-out Annotated rather than via config's ZepEnvDep
# alias, which is what the routers use. Deliberate: it keeps this module's only dependency
# on config the settings class itself, so auth does not break if that alias is retired.
async def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    if settings.graphiti_api_key is None:
        return

    # compare_digest rather than ==, so a wrong key takes the same time to reject
    # whatever its first differing byte is, and can't be recovered a character at a
    # time. It needs both sides to exist, hence the guard.
    #
    # Compared as bytes, not str: compare_digest rejects str arguments that aren't
    # ASCII-only, and both sides can be. Starlette decodes headers as latin-1, so a raw
    # high byte in the header arrives as a non-ASCII str, and nothing stops an operator
    # from rotating GRAPHITI_API_KEY to a value with an accent in it. As str, either case
    # raised TypeError inside this dependency and returned 500 — for the configured-key
    # case, on every request including the ones sending the right key.
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), settings.graphiti_api_key.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or missing API key. Send it as: Authorization: Bearer <GRAPHITI_API_KEY>',
            # Required on a 401 by RFC 9110, and it is what tells a client which scheme
            # to retry with rather than leaving it to guess.
            headers={'WWW-Authenticate': 'Bearer'},
        )
