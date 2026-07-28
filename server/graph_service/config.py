from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore


def _blank_to_none(value: str | None) -> str | None:
    """Treat an env var that is set but empty as unset."""
    return value or None


# A .env file and a dashboard UI both make it easy to define a variable with an empty
# value, which is not the same thing as leaving it out: '' arrives here as a real
# setting and gets passed on to the clients. An empty openai_base_url is the sharp
# edge — the OpenAI SDK accepts it and then issues requests with no host at all.
OptionalStr = Annotated[str | None, BeforeValidator(_blank_to_none)]


class Settings(BaseSettings):
    openai_api_key: str
    openai_base_url: OptionalStr = Field(None)
    model_name: OptionalStr = Field(None)
    embedding_model_name: OptionalStr = Field(None)
    neo4j_uri: OptionalStr = Field(None)
    neo4j_user: OptionalStr = Field(None)
    neo4j_password: OptionalStr = Field(None)
    falkordb_host: OptionalStr = Field(None)
    falkordb_port: int | None = Field(None)
    falkordb_username: OptionalStr = Field(None)
    falkordb_password: OptionalStr = Field(None)
    falkordb_database: OptionalStr = Field(None)
    db_backend: str = Field('neo4j')

    model_config = SettingsConfigDict(env_file='.env', extra='ignore')


@lru_cache
def get_settings():
    return Settings()  # type: ignore[call-arg]


ZepEnvDep = Annotated[Settings, Depends(get_settings)]
