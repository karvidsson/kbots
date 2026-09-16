"""HTTP boundary contracts with credential-bearing fake vendor responses."""

import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extras.posthog.alerts import EVENTS, DestinationNotFoundError, PostHogAdapter, parse_event
from src.core.alert_channels import AlertError, AlertStore

KEY = "synthetic-key-with-no-real-permissions"
WEBHOOK = "https://discord.com/api/webhooks/123/synthetic-webhook-token"
TEMPLATE = {"id": "template-discord", "code": "print(inputs.content)", "inputs_schema": []}


def serialized_destination(payload, destination_id):
    # HogFunctionSerializer at e4aa96a7: template_id is write_only; template
    # is read_only. POST, retrieve and PATCH all use this response shape.
    data = copy.deepcopy(payload)
    assert data.pop("template_id") == TEMPLATE["id"]
    data.update(id=destination_id, hog=TEMPLATE["code"], template=copy.deepcopy(TEMPLATE))
    return data


@pytest.fixture
def fixture(tmp_path):
    store = AlertStore(tmp_path)
    source = store.begin("worker", "101", "bot", "201")
    source = store.update(
        source["id"],
        state="provisional",
        config={
            "host": "https://eu.posthog.com",
            "project": "123",
            "app": "sample",
            "service": "posthog",
            "api_key": "secrets/posthog-api-key",
            "triggers": ["created", "reopened"],
        },
    )
    secrets = {
        "secrets/posthog-api-key": KEY,
        f"secrets/alert-webhook-{source['id']}-r{source['revision']}": WEBHOOK,
    }
    adapter = PostHogAdapter(SimpleNamespace(get=secrets.get), store)
    yield store, source, adapter, secrets
    store.close()


@pytest.mark.parametrize("change", ["host", "project", "kind", "nonce", "source", "issue", "extra"])
def test_forged_envelopes_or_config_are_rejected(fixture, change):
    _, source, adapter, _ = fixture
    parts = ["KBOTS_ALERT_V1", source["id"], source["nonce"], EVENTS["created"], str(uuid.uuid4()), str(uuid.uuid4())]
    if change in {"host", "project"}:
        source["config"][change] = "https://evil.invalid" if change == "host" else "123/../../people"
        with pytest.raises(AlertError):
            adapter.validate(source["config"])
        return
    index = {"kind": 3, "nonce": 2, "source": 1, "issue": 5}.get(change)
    if index is not None:
        parts[index] = "wrong"
    else:
        parts.append("run_shell")
    with pytest.raises(AlertError):
        parse_event(source, " ".join(parts))


async def test_create_projection_never_persists_vendor_secrets_or_bytecode(fixture, caplog):
    store, source, adapter, _ = fixture
    calls = []
    remote = {}

    async def request(config, method, resource, **kwargs):
        calls.append((method, resource, copy.deepcopy(kwargs)))
        if method == "GET":
            return copy.deepcopy(remote) if resource.endswith("/") else {"results": [], "next": None}
        data = serialized_destination(kwargs["payload"], str(uuid.uuid4()))
        data["inputs"]["webhookUrl"]["bytecode"] = ["literal", WEBHOOK]
        data["unrecognized_secret"] = KEY
        remote.update(data)
        return data

    adapter._request = request
    result = await adapter.destination(source, WEBHOOK)
    assert "template_id" not in remote and remote["template"]["id"] == TEMPLATE["id"]
    assert set(result) == {"id"}
    assert len([c for c in calls if c[0] == "POST"]) == 1
    assert await adapter.destination(source, WEBHOOK) == result
    assert len(calls) == 3
    dump = "\n".join(store.db.iterdump()) + caplog.text + json.dumps(result)
    assert WEBHOOK not in dump and KEY not in dump and "bytecode" not in dump
    payload = calls[1][2]["payload"]
    assert payload["type"] == "internal_destination"
    assert "{event.distinct_id}" in payload["inputs"]["content"]["value"]
    assert "bytecode" not in json.dumps(payload)


async def test_test_delivery_is_real_and_request_id_is_stable(fixture):
    store, source, adapter, _ = fixture
    issue_id, destination_id = str(uuid.uuid4()), str(uuid.uuid4())
    remote = owned_destination(store, source, destination_id)
    adapter._request = AsyncMock(
        side_effect=[remote, {"results": [{"id": issue_id}]}, {}, remote, {"results": [{"id": issue_id}]}]
    )
    await adapter.test_delivery(source, destination_id)
    await adapter.test_delivery(source, destination_id)
    posts = [call for call in adapter._request.call_args_list if call.args[1] == "POST"]
    assert len(posts) == 1
    body = posts[0].kwargs["payload"]
    assert body["mock_async_functions"] is False
    assert body["configuration"]["hog"] == TEMPLATE["code"]
    assert body["configuration"]["inputs"]["webhookUrl"]["value"] == WEBHOOK
    assert "bytecode" not in json.dumps(body["configuration"])
    assert body["globals"]["event"]["distinct_id"] == issue_id
    assert body["globals"]["event"]["uuid"] == str(uuid.uuid5(uuid.UUID(source["id"]), source["nonce"]))
    assert store.get(source["id"])["state"] == "provisional"


async def test_issues_use_fixed_get_and_positive_projection(fixture):
    _, source, adapter, _ = fixture
    issue = str(uuid.uuid4())
    adapter._request = AsyncMock(
        return_value={"id": issue, "name": "Error " + WEBHOOK, "properties": {"secret": KEY}, "person": KEY}
    )
    result = await adapter.issue(source, issue)
    assert set(result) == {"id", "name"}
    assert WEBHOOK not in json.dumps(result) and KEY not in json.dumps(result)
    assert adapter._request.call_args.args[1:] == ("GET", f"error_tracking/issues/{issue}/")
    assert not adapter._request.call_args.kwargs.get("provisioning")


async def test_incident_path_never_mutates_even_with_all_access_key(fixture):
    _, source, adapter, _ = fixture
    with pytest.raises(AlertError, match="cannot perform mutations"):
        await adapter._request(source["config"], "POST", "hog_functions/", payload={})


class FakeResponse:
    def __init__(self, status, chunks):
        self.status, self.chunks = status, chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for part in self.chunks:
            yield part


class FakeSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


async def test_http_reads_complete_chunked_response_and_disables_redirects(fixture):
    _, source, adapter, _ = fixture
    session = FakeSession(FakeResponse(200, [b'{"res', b'ults":', b"[]}"]))
    adapter.session_factory = lambda: session
    assert await adapter._request(source["config"], "GET", "error_tracking/issues/") == {"results": []}
    args, kwargs = session.calls[0]
    assert args[1] == "https://eu.posthog.com/api/projects/123/error_tracking/issues/"
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Authorization"] == "Bearer " + KEY


@pytest.mark.parametrize(
    "status,chunks",
    [(403, [WEBHOOK.encode()]), (302, [KEY.encode()]), (200, [b"x" * 2_000_001]), (200, [WEBHOOK.encode()])],
)
async def test_http_errors_never_reflect_bodies_or_credentials(fixture, caplog, status, chunks):
    _, source, adapter, _ = fixture
    adapter.session_factory = lambda: FakeSession(FakeResponse(status, chunks))
    with pytest.raises(AlertError) as caught:
        await adapter._request(source["config"], "GET", "error_tracking/issues/")
    assert WEBHOOK not in str(caught.value) + caplog.text
    assert KEY not in str(caught.value) + caplog.text


async def test_host_binding_mismatch_never_opens_a_session(fixture):
    _, source, adapter, secrets = fixture
    secrets["secrets/posthog-api-key"] = json.dumps({"host": "us.posthog.com", "value": KEY})
    adapter.session_factory = lambda: pytest.fail("credential must not be sent")
    with pytest.raises(AlertError, match="host binding"):
        await adapter._request(source["config"], "GET", "error_tracking/issues/")


async def test_resuming_a_locally_complete_but_changed_destination_is_held(fixture):
    store, source, adapter, _ = fixture
    store.intent(source, "destination")
    store.finish_operation(source, "destination", {"id": str(uuid.uuid4())})
    adapter._request = AsyncMock(return_value={"id": str(uuid.uuid4()), "enabled": False})
    with pytest.raises(AlertError, match="Recorded destination changed"):
        await adapter.destination(source, WEBHOOK)
    assert all(call.args[1] == "GET" for call in adapter._request.call_args_list)
    assert store.get(source["id"])["state"] == "provisional"


def owned_destination(store, source, destination_id):
    store.intent(source, "destination")
    store.finish_operation(source, "destination", {"id": destination_id})
    return serialized_destination(PostHogAdapter._destination_payload(source, WEBHOOK), destination_id)


@pytest.mark.parametrize("bundled", [False, True])
async def test_one_existing_key_serves_reads_and_provisioning_without_rewriting_vault(fixture, bundled):
    _, source, adapter, secrets = fixture
    if bundled:
        secrets["secrets/posthog-api-key"] = json.dumps({"host": "eu.posthog.com", "value": KEY})
    original = dict(secrets)
    session = FakeSession(FakeResponse(200, [b'{"results":[]}']))
    adapter.session_factory = lambda: session
    assert await adapter.check_credentials(source["config"]) == {
        "read_access": True, "write_access": "not yet exercised"
    }
    await adapter._request(source["config"], "POST", "hog_functions/", provisioning=True, payload={})
    assert len(session.calls) == 2
    assert {c[1]["headers"]["Authorization"] for c in session.calls} == {"Bearer " + KEY}
    assert secrets == original


@pytest.mark.parametrize("resource", ["hog_functions/", "../people/", "error_tracking/issues/?limit=100"])
async def test_incident_read_path_rejects_other_resources_without_http(fixture, resource):
    _, source, adapter, _ = fixture
    adapter.session_factory = lambda: pytest.fail("no request may escape")
    with pytest.raises(AlertError, match="fixed issue reads"):
        await adapter._request(source["config"], "GET", resource)


@pytest.mark.parametrize("operation", ["test_delivery", "disable", "remove"])
@pytest.mark.parametrize("ownership", ["missing", "incomplete", "wrong_id", "old_revision"])
async def test_mutation_requires_exact_completed_creation_record(fixture, operation, ownership):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    if ownership != "missing":
        store.intent(source, "destination")
    if ownership in {"wrong_id", "old_revision"}:
        store.finish_operation(
            source, "destination", {"id": str(uuid.uuid4()) if ownership == "wrong_id" else destination_id}
        )
    if ownership == "old_revision":
        source = {**source, "revision": source["revision"] + 1}
    adapter._request = AsyncMock()
    with pytest.raises(AlertError, match="not owned"):
        await getattr(adapter, operation)(source, destination_id)
    adapter._request.assert_not_awaited()


@pytest.mark.parametrize("operation", ["test_delivery", "disable", "remove"])
@pytest.mark.parametrize("changed", [
    "id", "name", "webhook", "content", "template", "filters", "inputs_shape",
    "template_missing", "template_null", "template_string", "template_no_id",
    "template_conflict", "template_request_echo",
])
async def test_foreign_or_changed_destination_never_receives_mutation(fixture, operation, changed):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    remote = owned_destination(store, source, destination_id)
    if changed in {"id", "name"}:
        remote[changed] = str(uuid.uuid4())
    elif changed in {"webhook", "content"}:
        remote["inputs"]["webhookUrl" if changed == "webhook" else "content"]["value"] = "different"
    elif changed == "template":
        remote["template"]["id"] = "template-other"
    elif changed == "template_missing":
        remote.pop("template")
    elif changed == "template_null":
        remote["template"] = None
    elif changed == "template_string":
        remote["template"] = TEMPLATE["id"]
    elif changed == "template_no_id":
        remote["template"].pop("id")
    elif changed == "template_conflict":
        remote["template_id"] = "template-other"
    elif changed == "template_request_echo":
        remote["template_id"] = remote.pop("template")["id"]
    elif changed == "filters":
        remote["filters"]["events"] = []
    else:
        remote["inputs"] = None
    adapter._request = AsyncMock(return_value=remote)
    with pytest.raises(AlertError, match="changed"):
        await getattr(adapter, operation)(source, destination_id)
    assert len(adapter._request.call_args_list) == 1
    assert adapter._request.call_args.args[1:] == ("GET", f"hog_functions/{destination_id}/")


async def test_disable_confirms_owned_object_and_is_idempotent(fixture, caplog):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    remote = owned_destination(store, source, destination_id)
    disabled = {**copy.deepcopy(remote), "enabled": False}
    adapter._request = AsyncMock(side_effect=[remote, disabled, disabled, disabled])
    await adapter.disable(source, destination_id)
    await adapter.disable(source, destination_id)
    mutations = [c for c in adapter._request.call_args_list if c.args[1] != "GET"]
    assert len(mutations) == 1
    assert mutations[0].args[1:] == ("PATCH", f"hog_functions/{destination_id}/")
    assert mutations[0].kwargs["payload"] == {"enabled": False}
    dump = "\n".join(store.db.iterdump()) + caplog.text
    assert WEBHOOK not in dump and KEY not in dump


async def test_unconfirmed_disable_reports_failure_after_local_revocation(fixture):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    remote = owned_destination(store, source, destination_id)
    store.disable(source["id"])
    adapter._request = AsyncMock(side_effect=[remote, remote])
    with pytest.raises(AlertError, match="not confirmed"):
        await adapter.disable(source, destination_id)
    assert store.get(source["id"])["state"] == "disabled"


@pytest.mark.parametrize(
    "issue_id",
    ["-" * 36, "0" * 36, "12345678-1234-1234-1234-123456789abz", "12345678123412341234123456789abc"],
)
async def test_incident_request_rejects_malformed_uuid_before_http(fixture, issue_id):
    _, source, adapter, _ = fixture
    adapter.session_factory = lambda: pytest.fail("malformed identifier must not reach HTTP")
    with pytest.raises(AlertError, match="fixed issue reads"):
        await adapter._request(source["config"], "GET", f"error_tracking/issues/{issue_id}/")


async def test_incident_uuid_read_uses_default_path_and_ownership_get_is_explicit(fixture):
    _, source, adapter, _ = fixture
    issue_id, destination_id = str(uuid.uuid4()), str(uuid.uuid4())
    session = FakeSession(FakeResponse(200, [b'{}']))
    adapter.session_factory = lambda: session
    await adapter._request(source["config"], "GET", f"error_tracking/issues/{issue_id}/")
    await adapter._request(source["config"], "GET", f"hog_functions/{destination_id}/", provisioning=True)
    assert [args[0] for args, _ in session.calls] == ["GET", "GET"]
    assert len(session.calls) == 2


async def test_recovery_reads_full_candidate_after_minimal_list_card(fixture):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    full = serialized_destination(PostHogAdapter._destination_payload(source, WEBHOOK), destination_id)
    card = {key: full[key] for key in ("id", "name", "type", "enabled", "filters")}
    store.intent(source, "destination")  # Simulate an unacknowledged create.
    adapter._request = AsyncMock(side_effect=[{"results": [card], "next": None}, full])
    assert await adapter.destination(source, WEBHOOK) == {"id": destination_id}
    assert [(c.args[1], c.args[2]) for c in adapter._request.call_args_list] == [
        ("GET", "hog_functions/?limit=100&offset=0"), ("GET", f"hog_functions/{destination_id}/")
    ]


async def test_test_invocation_rejects_changed_executable_code(fixture):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    remote = owned_destination(store, source, destination_id)
    remote["hog"] = "unexpected executable body"
    adapter._request = AsyncMock(return_value=remote)
    with pytest.raises(AlertError, match="code differs"):
        await adapter.test_delivery(source, destination_id)
    assert len(adapter._request.call_args_list) == 1
    assert adapter._request.call_args.args[1] == "GET"
    assert store.db.execute("SELECT COUNT(*) FROM operations WHERE step='delivery-test'").fetchone()[0] == 0


def scripted_http(adapter, responses):
    """Exercise the real status decoder, not an AsyncMock exception convention."""
    sessions = []
    remaining = iter(responses)

    def factory():
        status, data = next(remaining)
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        session = FakeSession(FakeResponse(status, [raw]))
        sessions.append(session)
        return session

    adapter.session_factory = factory
    return sessions


@pytest.mark.parametrize("enabled", [True, False])
async def test_remove_uses_soft_delete_then_exact_404_and_complete_listing(fixture, enabled, caplog):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    remote = owned_destination(store, source, destination_id)
    remote["enabled"] = enabled
    sessions = scripted_http(adapter, [
        (200, remote), (200, {**remote, "enabled": False}),
        (404, WEBHOOK.encode()), (200, {"results": [], "next": None}),
    ])
    await adapter.remove(source, destination_id)
    calls = [session.calls[0] for session in sessions]
    assert [args[0] for args, _ in calls] == ["GET", "PATCH", "GET", "GET"]
    assert calls[1][1]["json"] == {"enabled": False, "deleted": True}
    assert calls[2][0][1].endswith(f"/hog_functions/{destination_id}/")
    assert calls[3][0][1].endswith("/hog_functions/?limit=100&offset=0")
    assert all(kwargs["allow_redirects"] is False for _, kwargs in calls)
    assert KEY not in caplog.text and WEBHOOK not in caplog.text + "\n".join(store.db.iterdump())


@pytest.mark.parametrize("initially_missing", [False, True])
@pytest.mark.parametrize("failure", ["present", "listed", "list403", "list404", "get403", "get500", "timeout"])
async def test_removal_needs_both_positive_absence_reads(fixture, initially_missing, failure):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    remote = owned_destination(store, source, destination_id)
    statuses = {"get403": 403, "get500": 500}
    if failure in statuses:
        responses = [(statuses[failure], WEBHOOK.encode())]
    else:
        responses = [] if initially_missing and failure != "present" else [(200, remote), (204, {})]
        responses += [(200, remote)] if failure == "present" else [(404, KEY.encode())]
        if failure == "listed":
            responses.append((200, {"results": [{"id": destination_id}], "next": None}))
        elif failure in {"list403", "list404"}:
            responses.append((403 if failure == "list403" else 404, WEBHOOK.encode()))
        elif failure == "timeout":
            responses.append((200, {"results": [], "next": None}))
    sessions = scripted_http(adapter, responses)
    if failure == "timeout":
        factory = adapter.session_factory

        def fail_list():
            session = factory()
            if len(sessions) == len(responses):
                raise TimeoutError("sensitive request text " + KEY)
            return session

        adapter.session_factory = fail_list
    with pytest.raises(AlertError) as caught:
        await adapter.remove(source, destination_id)
    assert KEY not in str(caught.value) and WEBHOOK not in str(caught.value)


async def test_missing_destination_needs_all_list_pages_without_repeating_patch(fixture):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    owned_destination(store, source, destination_id)
    pages = [
        {"results": [{"id": str(uuid.uuid4())} for _ in range(100)], "next": "next page"},
        {"results": [], "next": None},
    ]
    sessions = scripted_http(adapter, [(404, b""), *[(200, page) for page in pages]])
    await adapter.remove(source, destination_id)
    assert [s.calls[0][0][0] for s in sessions] == ["GET", "GET", "GET"]
    assert sessions[-1].calls[0][0][1].endswith("offset=100")
    pages[-1]["results"] = [{"id": destination_id}]
    scripted_http(adapter, [(404, b""), *[(200, page) for page in pages]])
    with pytest.raises(AlertError, match="not confirmed"):
        await adapter.remove(source, destination_id)


@pytest.mark.parametrize("page", [
    {}, {"results": []}, {"results": [], "next": False}, {"results": [], "next": "more"},
    {"results": [{"name": "missing id"}], "next": None},
    {"results": [{"id": "bad"}], "next": None},
    {"results": None, "next": None},
    {"results": [{"id": "00000000-0000-0000-0000-000000000001"}] * 2, "next": None},
])
async def test_missing_destination_with_incomplete_listing_never_confirms_removal(fixture, page):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    owned_destination(store, source, destination_id)
    scripted_http(adapter, [(404, b""), (200, page)])
    with pytest.raises(AlertError, match="incomplete or malformed"):
        await adapter.remove(source, destination_id)


async def test_removal_scan_limit_is_not_absence(fixture):
    store, source, adapter, _ = fixture
    destination_id = str(uuid.uuid4())
    owned_destination(store, source, destination_id)
    adapter._request = AsyncMock(side_effect=[DestinationNotFoundError("HTTP 404"), *[
        {"results": [{"id": str(uuid.uuid4())} for _ in range(100)], "next": "more"} for _ in range(20)
    ]])
    with pytest.raises(AlertError, match="bounded scan"):
        await adapter.remove(source, destination_id)
    assert adapter._request.await_count == 21


async def test_missing_webhook_reference_cannot_authorize_removal(fixture):
    store, source, adapter, secrets = fixture
    destination_id = str(uuid.uuid4())
    owned_destination(store, source, destination_id)
    secrets.pop(f"secrets/alert-webhook-{source['id']}-r1")
    adapter._request = AsyncMock()
    with pytest.raises(AlertError, match="credential is unavailable"):
        await adapter.remove(source, destination_id)
    adapter._request.assert_not_awaited()
