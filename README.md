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
   | **Embeddings** | Write access on `/v1/embeddings` — embedding nodes, edges, and search queries.     |

   Everything else is set for you in `render.yaml`. The ones worth knowing about:

   | Variable          | Default   | Notes                                                                 |
   | ----------------- | --------- | --------------------------------------------------------------------- |
   | `MODEL_NAME`      | `gpt-5.5` | Any OpenAI model id.                                                  |
   | `SEMAPHORE_LIMIT` | `10`      | Concurrent LLM calls during ingestion. Raise it on a bigger instance. |

3. Wait for both services to go live. `graphiti-api` passes its health check at `/healthcheck`.

> **This API has no authentication.** Anyone who knows your URL can write to your graph and
> spend your OpenAI key. Before you point real traffic at it, put it behind your own auth,
> an API gateway, or a private network. In the meantime, use a dedicated OpenAI key you can
> revoke and set a monthly spend cap in the OpenAI console — that cap is your backstop.

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

## Configuration notes

**FalkorDB has no password.** It's a private service with no public address, so isolation comes
from the network. To add one anyway, set `REDIS_ARGS` to `--appendonly yes --requirepass <your
password>` on `graphiti-falkordb`, and set `FALKORDB_PASSWORD` to the same value on
`graphiti-api`. Render can't interpolate one env var into another, so this is a manual step.

**Using Neo4j instead.** Set `DB_BACKEND=neo4j` and supply `NEO4J_URI`, `NEO4J_USER`, and
`NEO4J_PASSWORD` (e.g. from Neo4j Aura), then drop the `graphiti-falkordb` service from
`render.yaml`.

**Local development.** Copy [`.env.example`](.env.example) to `.env`, fill in `OPENAI_API_KEY`,
and run `docker compose --profile falkordb up`. That mirrors this Blueprint — the API on
`http://localhost:8001`, FalkorDB beside it. A plain `docker compose up` runs the repo's other
pairing, API plus Neo4j, on port 8000.

## Learn more

This repo is a fork of [getzep/graphiti](https://github.com/getzep/graphiti) with a Render
Blueprint added. For how Graphiti works — custom entity types, search strategies, the MCP
server, the Python library — see the
[upstream README](https://github.com/getzep/graphiti#readme) and
[the paper](https://arxiv.org/abs/2501.13956).
