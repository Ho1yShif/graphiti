"""Unit tests for the demo-mode middleware.

These exercise the middleware directly against a stand-in app, so they need neither a
graph database nor an OpenAI key.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph_service.demo import (
    LIMIT_MESSAGE,
    LOCKED_MESSAGE,
    SESSION_HEADER,
    DemoConfig,
    DemoModeMiddleware,
    DemoState,
)


def build_client(**overrides) -> TestClient:
    """A stand-in for the real routers that echoes back what the middleware passed through."""
    config = DemoConfig(enabled=True, **overrides)
    app = FastAPI()
    app.add_middleware(DemoModeMiddleware, state=DemoState(config))

    @app.get('/healthcheck')
    async def healthcheck():
        return {'status': 'healthy'}

    @app.post('/messages')
    async def messages(payload: dict):
        return payload

    @app.post('/search')
    async def search(payload: dict):
        return payload

    @app.get('/episodes/{group_id}')
    async def episodes(group_id: str, last_n: int = 1):
        return {'group_id': group_id}

    @app.post('/clear')
    async def clear():
        return {'cleared': True}

    @app.delete('/group/{group_id}')
    async def delete_group(group_id: str):
        return {'deleted': group_id}

    # https so the Secure session cookie is actually retained between requests.
    return TestClient(app, base_url='https://testserver')


def session_id(response) -> str:
    return response.headers[SESSION_HEADER]


def test_healthcheck_is_never_gated():
    client = build_client(rate_limit_per_minute=1)
    for _ in range(5):
        assert client.get('/healthcheck').status_code == 200


def test_destructive_routes_are_locked():
    client = build_client()
    for response in (client.post('/clear'), client.delete('/group/anything')):
        assert response.status_code == 403
        assert response.json()['detail'] == LOCKED_MESSAGE


def test_group_id_is_forced_to_the_visitor_session():
    client = build_client()
    response = client.post('/messages', json={'group_id': 'someone-else', 'messages': []})

    assert response.status_code == 200
    assert response.json()['group_id'] == session_id(response)
    assert response.json()['group_id'] != 'someone-else'


def test_group_ids_list_is_forced_to_the_visitor_session():
    client = build_client()
    response = client.post('/search', json={'group_ids': ['someone-else'], 'query': 'hi'})

    assert response.json()['group_ids'] == [session_id(response)]
    assert response.json()['query'] == 'hi'


def test_search_without_group_ids_is_still_scoped_to_the_session():
    """Omitting group_ids means 'every group' downstream, so it must be pinned anyway."""
    client = build_client()
    response = client.post('/search', json={'query': 'hi'})

    assert response.json()['group_ids'] == [session_id(response)]


def test_group_id_in_the_path_is_rewritten():
    client = build_client()
    response = client.get('/episodes/someone-else', params={'last_n': 1})

    assert response.json()['group_id'] == session_id(response)


def test_two_visitors_get_separate_sessions():
    first = build_client()
    second = build_client()
    first_id = session_id(first.post('/search', json={'group_ids': None, 'query': 'a'}))
    second_id = session_id(second.post('/search', json={'group_ids': None, 'query': 'a'}))

    assert first_id != second_id


def test_session_is_reused_across_requests():
    client = build_client()
    first = client.post('/search', json={'group_ids': [], 'query': 'a'})
    second = client.post('/search', json={'group_ids': [], 'query': 'b'})

    assert second.json()['group_ids'] == [session_id(first)]


def test_session_can_be_carried_by_header_for_api_clients():
    client = build_client()
    minted = session_id(client.post('/search', json={'query': 'a'}))

    response = client.post(
        '/search',
        json={'group_ids': ['someone-else'], 'query': 'b'},
        headers={SESSION_HEADER: minted},
    )
    assert response.json()['group_ids'] == [minted]


def test_a_non_uuid_session_header_is_rejected():
    client = build_client()
    response = client.post(
        '/search', json={'group_ids': None, 'query': 'a'}, headers={SESSION_HEADER: 'not-a-uuid'}
    )

    assert response.json()['group_ids'] == [session_id(response)]
    assert session_id(response) != 'not-a-uuid'


def test_rate_limit_returns_429_not_500():
    client = build_client(rate_limit_per_minute=2)
    for _ in range(2):
        assert client.post('/search', json={'query': 'a'}).status_code == 200

    response = client.post('/search', json={'query': 'a'})
    assert response.status_code == 429
    assert response.json()['detail'] == LIMIT_MESSAGE


def forwarded_for(spoofed: str, real: str = '203.0.113.7') -> dict[str, str]:
    """Render appends the real client IP to whatever the caller sent, so the header looks
    like this by the time it reaches us: the caller's hop first, the trustworthy one last."""
    return {'X-Forwarded-For': f'{spoofed}, {real}'}


def test_a_spoofed_forwarded_for_cannot_buy_a_fresh_rate_limit_bucket():
    client = build_client(rate_limit_per_minute=2)

    for attempt in range(2):
        response = client.post(
            '/search', json={'query': 'a'}, headers=forwarded_for(f'10.0.0.{attempt}')
        )
        assert response.status_code == 200

    # A brand new spoofed hop, but the same real client behind it.
    response = client.post('/search', json={'query': 'a'}, headers=forwarded_for('10.0.0.99'))
    assert response.status_code == 429


def test_separate_clients_get_separate_rate_limit_buckets():
    client = build_client(rate_limit_per_minute=1)

    assert (
        client.post('/search', json={'query': 'a'}, headers=forwarded_for('x')).status_code == 200
    )
    second = client.post('/search', json={'query': 'a'}, headers=forwarded_for('x', '198.51.100.4'))
    assert second.status_code == 200


def test_zero_disables_the_rate_limit():
    client = build_client(rate_limit_per_minute=0)
    for _ in range(20):
        assert client.post('/search', json={'query': 'a'}).status_code == 200


def test_episode_cap_counts_messages_in_the_payload():
    client = build_client(max_episodes_per_session=3)
    two = {'group_id': 'x', 'messages': [{'content': 'a'}, {'content': 'b'}]}

    assert client.post('/messages', json=two).status_code == 200
    assert client.post('/messages', json=two).status_code == 429


def test_zero_disables_the_episode_cap():
    client = build_client(max_episodes_per_session=0)
    payload = {'group_id': 'x', 'messages': [{'content': 'a'}] * 50}

    assert client.post('/messages', json=payload).status_code == 200
