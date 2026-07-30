from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import Depends
from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore


def _blank_to_none(value: Any) -> Any:
    """Treat an env var that is set but empty as unset."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _strip(value: Any) -> Any:
    """Trim surrounding whitespace from an env var that has to be present."""
    return value.strip() if isinstance(value, str) else value


# A .env file and a dashboard UI both make it easy to define a variable with an empty
# value, which is not the same thing as leaving it out: '' arrives here as a real setting
# and gets passed on to the clients. A blank model_name would be sent to OpenAI as the
# model to use, and a blank falkordb_port would fail validation before the app can boot.
#
# One case this cannot reach: the OpenAI SDK falls back to reading OPENAI_BASE_URL from
# the environment when it is handed base_url=None, so a blank OPENAI_BASE_URL still ends
# up as '' inside the client, and requests go out with no host. Leave it unset, not empty.
OptionalStr = Annotated[str | None, BeforeValidator(_blank_to_none)]
OptionalInt = Annotated[int | None, BeforeValidator(_blank_to_none)]

# The same idea for a setting that has to be present: strip first, so whitespace fails the
# min_length check on the fields below rather than passing as a one-space secret. Stripping
# also survives a key pasted with a trailing newline, which the OpenAI SDK would otherwise
# put in a header and fail on.
RequiredStr = Annotated[str, BeforeValidator(_strip)]


class Settings(BaseSettings):
    # Rejected when blank rather than defaulted, so a missing key fails the deploy at
    # startup instead of turning every background ingestion into a 401 nobody is watching.
    openai_api_key: RequiredStr = Field(min_length=1)
    openai_base_url: OptionalStr = None
    model_name: OptionalStr = None
    embedding_model_name: OptionalStr = None
    neo4j_uri: OptionalStr = None
    neo4j_user: OptionalStr = None
    neo4j_password: OptionalStr = None
    falkordb_host: OptionalStr = None
    falkordb_port: OptionalInt = None
    falkordb_username: OptionalStr = None
    falkordb_password: OptionalStr = None
    falkordb_database: OptionalStr = None
    # Only these two backends are wired up in zep_graphiti, so a typo should be a startup
    # error naming the valid values, not a silent fall-through to the Neo4j branch.
    db_backend: Literal['neo4j', 'falkordb'] = 'neo4j'
    # Bearer token for every endpoint except /healthcheck. Required, and required with no
    # way to switch off: this API writes to a shared graph and spends openai_api_key on
    # every episode, so a deployment that boots open is never what anyone wanted. A missing
    # key fails at startup instead — an open service and a service that 401s everything
    # both look healthy to Render. Render generates the value; compose supplies a dev one.
    graphiti_api_key: RequiredStr = Field(min_length=1)

    model_config = SettingsConfigDict(env_file='.env', extra='ignore')


@lru_cache
def get_settings():
    return Settings()  # type: ignore[call-arg]


ZepEnvDep = Annotated[Settings, Depends(get_settings)]
