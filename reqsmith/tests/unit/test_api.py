import httpx

from reqsmith.api.app import create_app


def _client():
    app = create_app(run_worker=False)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_webhook_returns_202_with_job(sample_webhook_payload):
    async with _client() as client:
        resp = await client.post("/webhooks/jira", json=sample_webhook_payload)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["run_id"] and body["job_id"]
        assert body["duplicate"] is False

        status = await client.get(body["status_url"])
        assert status.status_code == 200
        assert status.json()["status"] == "queued"
        assert status.json()["run_state"] == "intake"


async def test_webhook_replay_is_flagged_duplicate(sample_webhook_payload):
    async with _client() as client:
        first = (await client.post("/webhooks/jira", json=sample_webhook_payload)).json()
        second = (await client.post("/webhooks/jira", json=sample_webhook_payload)).json()
        assert second["duplicate"] is True
        assert second["run_id"] == first["run_id"]


async def test_webhook_rejects_bad_secret(sample_webhook_payload, monkeypatch):
    from reqsmith import settings as settings_module

    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "s3cret")
    settings_module.get_settings.cache_clear()
    try:
        async with _client() as client:
            resp = await client.post("/webhooks/jira?secret=wrong", json=sample_webhook_payload)
            assert resp.status_code == 401
            resp = await client.post("/webhooks/jira?secret=s3cret", json=sample_webhook_payload)
            assert resp.status_code == 202
    finally:
        settings_module.get_settings.cache_clear()


async def test_webhook_requires_issue_key():
    async with _client() as client:
        resp = await client.post("/webhooks/jira", json={"webhookEvent": "x"})
        assert resp.status_code == 400


async def test_audit_replay_endpoint(sample_webhook_payload):
    async with _client() as client:
        created = (await client.post("/webhooks/jira", json=sample_webhook_payload)).json()
        audit = await client.get(f"/runs/{created['run_id']}/audit")
        assert audit.status_code == 200
        actions = [e["action"] for e in audit.json()["events"]]
        assert "run.created" in actions and "webhook.received" in actions


async def test_healthz_and_tick():
    async with _client() as client:
        before = await client.get("/healthz")
        assert before.json()["last_tick"] is None
        await client.post("/internal/tick")
        after = await client.get("/healthz")
        assert after.json()["last_tick"] is not None


async def test_outreach_kill_switch():
    async with _client() as client:
        resp = await client.post("/outreach/pause", params={"paused": True, "reason": "test"})
        assert resp.json()["outreach_paused"] is True
