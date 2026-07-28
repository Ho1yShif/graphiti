# Graphiti on Render

Deploy [Graphiti](https://github.com/getzep/graphiti) on Render in one click. Get a hosted
temporal knowledge-graph API your agents can write memories to and search over time.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Ho1yShif/graphiti)

## What you get

Graphiti turns a stream of conversations or events into a knowledge graph, and keeps track of
_when_ each fact was true. When something changes — someone switches teams, a deadline moves —
it doesn't overwrite the old fact, it invalidates it and records the new one. So an agent can
ask what's true now, and also what was true last quarter.

This template deploys the backend for that: a REST API and the graph store behind it. **There
is no web UI.** The deliverable is a live URL like `https://graphiti-api.onrender.com` that you
call from your own agent code, a script, or a framework like LangGraph.

### Architecture

```
                       ┌───────────────────────────┐
   your agent /        │   graphiti-api (docker)   │        ┌──────────────┐
   script / app  ─────▶│   FastAPI · public HTTPS  │───────▶│  OpenAI API  │
      HTTPS            │   /messages  /search      │  facts └──────────────┘
                       └─────────────┬─────────────┘  entities
                                     │
                         Redis proto │ private network only
                                     ▼
                       ┌───────────────────────────┐
                       │ graphiti-falkordb (image) │
                       │ private service · :6379   │
                       │ 10 GB disk, append-only   │
                       └───────────────────────────┘
```

`graphiti-falkordb` is a **private service**: it has no public address and accepts traffic only
from `graphiti-api` over Render's private network.

## Deploy

1. Click **Deploy to Render** above. Render reads [`render.yaml`](render.yaml) and creates a
   `graphiti` project containing both services.
2. Render prompts for one secret on `graphiti-api`:

   | Variable         | What it's for                                                                                       | Where to get it                                                      |
   | ---------------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
   | `OPENAI_API_KEY` | Graphiti calls an LLM to pull entities and facts out of each episode, and to embed them for search. | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |

   If you use a **restricted** OpenAI key, this deployment needs exactly two of the
   permissions under **Model capabilities**, both write:

   | Permission     | Used for                                                                           |
   | -------------- | ---------------------------------------------------------------------------------- |
   | **Responses**  | Write access on `/v1/responses` — extracting entities and facts from each episode. |
   | **Embeddings** | Request access on /v1/embeddings` — embedding nodes, edges, and search queries.    |

   Everything else is set for you in `render.yaml`. The ones worth knowing about:

   | Variable          | Default   | Notes                                                                        |
   | ----------------- | --------- | ---------------------------------------------------------------------------- |
   | `MODEL_NAME`      | `gpt-5.5` | Any OpenAI model id.                                                         |
   | `SEMAPHORE_LIMIT` | `10`      | Concurrent LLM calls during ingestion. Raise it on a bigger instance.        |
   | `DEMO`            | `false`   | See [Demo mode](#demo-mode). Leave it off unless you want a public showcase. |

3. Wait for both services to go live. `graphiti-api` passes its health check at `/healthcheck`.

> **This API has no authentication.** Anyone who knows your URL can write to your graph and
> spend your OpenAI key. Before you point real traffic at it, put it behind your own auth,
> an API gateway, or a private network — or run it with `DEMO=true`, which adds rate limits
> and per-visitor isolation.

### Using the app

Ingestion is asynchronous — `/messages` queues the episode and returns immediately, then
Graphiti extracts entities and facts in the background. Give it 10–30 seconds before searching.

Set your URL once:

```bash
export GRAPHITI_URL=https://graphiti-api.onrender.com
```

1. **Check it's up.**

   ```bash
   curl $GRAPHITI_URL/healthcheck
   # {"status":"healthy"}
   ```

2. **Ingest the sample episode** that ships in this repo
   ([`examples/render/sample-episode.json`](examples/render/sample-episode.json)). It's a short
   conversation where Alex leads the payments team, and then hands it to Priya:

   ```bash
   curl -X POST $GRAPHITI_URL/messages \
     -H 'Content-Type: application/json' \
     -d @examples/render/sample-episode.json
   # {"message":"Messages added to processing queue","success":true}
   ```

3. **Watch the episodes land.**

   ```bash
   curl "$GRAPHITI_URL/episodes/demo?last_n=5"
   ```

4. **Search the facts Graphiti extracted.**

   ```bash
   curl -X POST $GRAPHITI_URL/search \
     -H 'Content-Type: application/json' \
     -d '{"group_ids": ["demo"], "query": "who owns the ledger migration?", "max_facts": 5}'
   ```

   You should get back facts naming Priya as the current owner — and, because Graphiti is
   temporal rather than destructive, the earlier Alex fact with an `invalid_at` timestamp on
   it. That pair is the whole point of the graph.

5. **Clean up** when you're done experimenting:

   ```bash
   curl -X DELETE $GRAPHITI_URL/group/demo
   ```

Full endpoint list at `$GRAPHITI_URL/docs` (FastAPI's generated OpenAPI docs).

`group_id` partitions the graph. Use one per user, per tenant, or per agent, and search stays
scoped to it.

## Demo mode

`DEMO=true` turns this deploy into a **public, no-signup showcase** running on your OpenAI key.
It is **off by default** — a fresh deploy gives you the ordinary API described above, with no
limits and no session rewriting.

Turn it on only for a demo URL you're deliberately hosting for strangers. It selects one
locked-down configuration:

**What a visitor can and can't do**

| Capability                                                              | In demo mode                                  |
| ----------------------------------------------------------------------- | --------------------------------------------- |
| Ingest episodes (`POST /messages`)                                      | ✅ on, capped and scoped to their own session |
| Search and read (`POST /search`, `GET /episodes/…`, `POST /get-memory`) | ✅ on, scoped to their own session            |
| Add an entity node (`POST /entity-node`)                                | ✅ on, scoped to their own session            |
| Delete an edge, episode, or group                                       | ❌ 403                                        |
| Wipe the graph (`POST /clear`)                                          | ❌ 403                                        |

**Session isolation.** Every visitor is assigned an unguessable UUID and pinned to it. Whatever
`group_id` they send is overwritten with their own, so no visitor can read, write, or delete
another visitor's graph. Sessions are carried by an `HttpOnly` cookie, or by an `X-Demo-Session`
header for API clients. Session graphs are deleted once they expire, so demo history never
accumulates.

**Limits.** All are plain env vars. Set one to `0` to disable that dimension; an unset or
invalid value falls back to the default rather than to "unlimited".

| Variable                        | Default        | Bounds                                                 |
| ------------------------------- | -------------- | ------------------------------------------------------ |
| `DEMO_RATE_LIMIT_PER_MINUTE`    | `10`           | Requests per minute, per IP.                           |
| `DEMO_MAX_EPISODES_PER_SESSION` | `30`           | Episodes one visitor can ingest, total.                |
| `DEMO_SESSION_TTL_MINUTES`      | `60`           | How long a session's graph survives before it's swept. |
| `DEMO_MODEL_NAME`               | `gpt-4.1-nano` | Cheaper model used only on the demo path.              |

Tripping a limit returns `429` with _"Demo limit reached — deploy your own Graphiti to keep
going."_

**If you host a public demo URL yourself,** use a dedicated OpenAI key you can revoke, set a
monthly spend cap and billing alerts in the OpenAI console, and keep the Render service handy
as a kill switch. The in-app limits bound normal use; the provider cap is your backstop.

## Configuration notes

**FalkorDB has no password.** It's a private service with no public address, so isolation comes
from the network. To add one anyway, set `REDIS_ARGS` to `--appendonly yes --requirepass <your
password>` on `graphiti-falkordb`, and set `FALKORDB_PASSWORD` to the same value on
`graphiti-api`. Render can't interpolate one env var into another, so this is a manual step.

**Using Neo4j instead.** Set `DB_BACKEND=neo4j` and supply `NEO4J_URI`, `NEO4J_USER`, and
`NEO4J_PASSWORD` (e.g. from Neo4j Aura), then drop the `graphiti-falkordb` service from
`render.yaml`.

**Local development.** Copy [`.env.example`](.env.example) to `.env`, fill in `OPENAI_API_KEY`,
and run `docker compose up`.

## Learn more

This repo is a fork of [getzep/graphiti](https://github.com/getzep/graphiti) with a Render
Blueprint added. For how Graphiti works — custom entity types, search strategies, the MCP
server, the Python library — see the
[upstream README](https://github.com/getzep/graphiti#readme) and
[the paper](https://arxiv.org/abs/2501.13956).
