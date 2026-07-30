# graph-service

Graph service is a fast api server implementing the [graphiti](https://github.com/getzep/graphiti) package.

**Deploying to Render?** Start at the [root README](../README.md) — it covers the one-click
Blueprint, the FalkorDB pairing, and the env vars `render.yaml` sets for you. This file is only
about running the API on its own.

## Authentication

Every endpoint except `/healthcheck` requires a bearer token, and there is no way to switch that
off: `GRAPHITI_API_KEY` must be set to at least 16 printable-ASCII characters or the server exits
at startup. That is deliberate — the API writes to a shared graph and spends the deployment's
`OPENAI_API_KEY`, and an open service and one that 401s everything both look healthy to a health
check.

```
Authorization: Bearer <GRAPHITI_API_KEY>
```

`/docs`, `/redoc` and `/openapi.json` stay public so Swagger UI is browsable; use its
**Authorize** button to call anything. A wrong or missing key gets `401`, and after 10 rejections
in a minute the server answers `429` instead.

## Container Releases

Upstream publishes the FastAPI server to Docker Hub as `zepai/graphiti` when a new
`graphiti-core` version is released to PyPI (linux/amd64 and linux/arm64, tagged `latest` and by
version; pre-releases are skipped).

**That image is upstream's and does not include this fork's authentication**, so it is not what
the instructions below use. Build from this repo's root `Dockerfile` instead — that is also what
`render.yaml` and `docker-compose.yml` do, so all three stay in step on one `graphiti-core` pin.

## Running Instructions

1. Ensure you have Docker and Docker Compose installed on your system.

2. Copy [`.env.example`](.env.example) to `server/.env` and fill in `OPENAI_API_KEY`.

3. The quickest path is the compose stack in the repo root, which builds this service and starts a
   database beside it:

   ```bash
   docker compose up                      # API on :8000 with Neo4j
   docker compose --profile falkordb up   # API on :8001 with FalkorDB, mirroring render.yaml
   ```

   It defaults `GRAPHITI_API_KEY` to `insecure-local-dev-key`, so the examples in the root README
   work unchanged.

4. To wire it into your own compose file, build from the repo root and pass the variables below.
   This service needs access to a Neo4j instance — add a Neo4j image beside it as here, or point
   `NEO4J_URI` at Neo4j Aura or a desktop install.

   ```yml
   services:
     graph:
       build:
         context: .          # the repo root, not server/
       # Bound to 127.0.0.1, because the key below is a committed placeholder and protects
       # nothing. Drop the prefix to reach this from another host, and set a real key in the
       # same edit.
       ports:
         - "127.0.0.1:8000:8000"
       environment:
         - OPENAI_API_KEY=${OPENAI_API_KEY}
         # Required. Startup fails without it; 16+ printable-ASCII characters.
         - GRAPHITI_API_KEY=${GRAPHITI_API_KEY:-insecure-local-dev-key}
         - NEO4J_URI=bolt://neo4j:${NEO4J_PORT:-7687}
         - NEO4J_USER=${NEO4J_USER:-neo4j}
         - NEO4J_PASSWORD=${NEO4J_PASSWORD:-password}
     neo4j:
       image: neo4j:5.26.2
       # Localhost-bound as above, with more at stake: the password defaults to `password` and
       # Bolt is unmediated write access.
       ports:
         - "127.0.0.1:7474:7474"                              # HTTP
         - "127.0.0.1:${NEO4J_PORT:-7687}:${NEO4J_PORT:-7687}" # Bolt
       volumes:
         - neo4j_data:/data
       environment:
         - NEO4J_AUTH=${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD:-password}

   volumes:
     neo4j_data:
   ```

5. Once you start the service, it will be available at `http://localhost:8000` (or the port you
   have specified in the docker compose file). `GET /healthcheck` is the one endpoint that takes
   no key.

6. You may access the swagger docs at `http://localhost:8000/docs`. You may also access redocs at
   `http://localhost:8000/redoc`. Click **Authorize** and paste your `GRAPHITI_API_KEY` before
   using **Try it out**.

7. You may also access the neo4j browser at `http://localhost:7474` (the port depends on the neo4j
   instance you are using).
