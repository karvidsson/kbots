"""Exact trigger selection and explicit, labelled manual classification override."""

import copy
import json
import uuid
from contextlib import closing

import pytest

from extras.posthog.alerts import PostHogAdapter
from src.core.alert_channels import AlertStore
from src.core.alert_fix_store import FixJobs
from tests.test_alert_evidence_wait import delayed as delayed_fixture
from tests.test_alert_exception_evidence import adapter as adapter_fixture
from tests.test_alert_exception_evidence import response
from tests.test_alert_fix_jobs import delivered
from tests.test_alert_fix_jobs import setup as setup_fixture
from tests.test_alert_setup_ux import visible_text
from tests.test_posthog_alerts import FakeResponse, FakeSession

adapter = adapter_fixture
setup = setup_fixture
delayed = delayed_fixture


@pytest.mark.parametrize("case", ["real", "drill", "lag", "unavailable", "wrong_event", "empty"])
async def test_exact_uuid_and_same_event_drill_probe_with_bounded_latest_fallback(adapter, case):
    issue, trigger, latest = (str(uuid.uuid4()) for _ in range(3))
    sample = response()
    sample["results"][0]["uuid"] = trigger if case in {"real", "drill"} else latest
    reads = []

    class Session(FakeSession):
        def request(self, method, url, **kwargs):
            query = kwargs.get("json")
            reads.append((method, query))
            if method == "GET":
                data = {"id": issue, "name": "bad input"}
            else:
                assert adapter._sample_request_allowed("error_tracking/query/issue_events/", query)
                filters = query.get("filterGroup", [])
                is_drill = any(f["key"] == "test" for f in filters)
                selected = next((f["value"][0] for f in filters if f["key"] == "uuid"), None)
                if is_drill:
                    assert selected == sample["results"][0]["uuid"]
                    data = sample if case == "drill" else {"results": [], "hasMore": False}
                elif selected == trigger and case == "unavailable":
                    return FakeResponse(503, [b"{}"])
                elif case == "empty" or (selected == trigger and case == "lag"):
                    data = {"results": []}
                else:
                    data = sample  # wrong_event deliberately violates the query.
            return FakeResponse(200, [json.dumps(data).encode()])

    adapter.session_factory = lambda: Session(None)
    result = await adapter.issue({"config": adapter.test_config}, issue, trigger)
    posts = [body for method, body in reads if method == "POST"]
    assert posts[0]["filterGroup"] == [
        {"key": "uuid", "value": [trigger], "operator": "exact", "type": "event_metadata"}
    ]
    expected = "drill" if case == "drill" else "unknown" if case == "empty" else "unmarked"
    assert result["sample"]["drill_status"] == expected
    assert all(p["dateRange"] == posts[0]["dateRange"] for p in posts)
    assert all(p["include"] == ["exception", "stacktrace", "release"] and p["limit"] == 1 for p in posts)
    if case in {"real", "drill"}:
        assert len(posts) == 2 and result["sample"]["matches_trigger"] is True
        assert result["sample"]["status"] == "The sampled exception is the triggering event."
    else:
        assert "filterGroup" not in posts[1]  # Same bounded latest-event fallback.
        assert result["sample"].get("matches_trigger") is not True
        assert len(posts) == (2 if case == "empty" else 3)
        if case != "empty":
            assert "not confirmed" in result["sample"]["drill_scope"]
    projected = json.dumps(result)
    for private in (trigger, latest, "private-person", "private-session", "private-variable"):
        assert private not in projected


@pytest.mark.parametrize("drill", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "other_key",
        "event_property",
        "hogql",
        "operator",
        "second_uuid",
        "empty_uuid",
        "scalar_uuid",
        "invalid_uuid",
        "uppercase_uuid",
        "extra_filter",
        "extra_field",
        "limit",
        "includes",
        "window",
        "false_marker",
        "missing_type",
        "bad_group",
        "bad_filter",
        "empty_group",
    ],
)
def test_exact_event_allowlist_does_not_admit_arbitrary_queries(drill, mutation):
    body = PostHogAdapter.sample_request(
        str(uuid.uuid4()), event_id="abcdefab-cdef-4abc-8abc-abcdefabcdef", drill=drill
    )
    assert PostHogAdapter._sample_request_allowed("error_tracking/query/issue_events/", body)
    clause = body["filterGroup"][0]
    if mutation == "other_key":
        clause["key"] = "distinct_id"
    elif mutation in {"event_property", "hogql"}:
        clause["type"] = "event" if mutation == "event_property" else "hogql"
    elif mutation == "operator":
        clause["operator"] = "is_not"
    elif mutation == "second_uuid":
        clause["value"].append(str(uuid.uuid4()))
    elif mutation == "empty_uuid":
        clause["value"] = []
    elif mutation == "scalar_uuid":
        clause["value"] = clause["value"][0]
    elif mutation == "invalid_uuid":
        clause["value"] = ["../../people"]
    elif mutation == "uppercase_uuid":
        clause["value"] = [clause["value"][0].upper()]
    elif mutation == "extra_filter":
        body["filterGroup"].append(copy.deepcopy(clause))
    elif mutation == "extra_field":
        body["eventId"] = clause["value"][0]
    elif mutation == "limit":
        body["limit"] = True
    elif mutation == "includes":
        body["include"].append("code_variables")
    elif mutation == "window":
        body["dateRange"]["date_from"] = "2000-01-01T00:00:00Z"
    elif mutation == "false_marker":
        body["filterGroup"].append({"key": "test", "value": ["false"], "operator": "exact", "type": "event"})
    elif mutation == "missing_type":
        del clause["type"]
    elif mutation == "bad_group":
        body["filterGroup"] = {"type": "OR", "values": [clause]}
    elif mutation == "bad_filter":
        body["filterGroup"] = [None]
    else:
        body["filterGroup"] = []
    assert not PostHogAdapter._sample_request_allowed("error_tracking/query/issue_events/", body)


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_automatic_stays_strict_and_manual_override_survives_reopen(setup, manual, confirmed):
    o = setup
    row = await delivered(o)
    row["fix_context"]["trigger_unmarked"] = confirmed
    jobs = FixJobs(o.h.store)
    job = jobs.enqueue(o.source, row, manual=manual)
    with closing(AlertStore(o.h.directory)) as store:
        resumed = FixJobs(store)
        saved = resumed.get(job["id"])
        assert saved["payload"]["drill_status_unconfirmed"] is (manual and not confirmed)
        claimed = resumed.claim()
        assert bool(claimed) is (manual or confirmed)
        if not claimed:
            assert "classification" in saved["result"]["reason"]
        else:
            await o.h.alerts.transport.fix_status(o.source, row, claimed)
            assert ("drill status unconfirmed" in visible_text(o.room.messages[0])) is (manual and not confirmed)


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("marker", ["drill", "setup_test", "sample_drill_status", "saved_sample", "setup_uuid"])
async def test_declared_drill_never_runs_even_with_manual_override(setup, manual, marker):
    o = setup
    row = await delivered(o)
    row["fix_context"]["trigger_unmarked"] = False
    if marker == "saved_sample":
        row["evidence"] = {"issue": {"sample": {"drill_status": "drill"}}}
    elif marker == "setup_uuid":
        row["event_id"] = str(uuid.uuid5(uuid.UUID(o.source["id"]), o.source["nonce"]))
    else:
        row[marker] = "drill" if marker == "sample_drill_status" else True
    jobs = FixJobs(o.h.store)
    job = jobs.enqueue(o.source, row, manual=manual)
    assert job["state"] == "skipped" and job["result"]["reason"] == "drill or setup check"
    assert jobs.claim() is None
    assert o.h.alerts.fix_controls.view(o.source, row) is None


@pytest.mark.parametrize("case", ["real", "lag", "drill"])
async def test_diagnosis_persists_actual_trigger_relation_for_both_fix_paths(delayed, case):
    d = delayed
    receipt = d.h.store.pending()[0]
    d.source = d.h.store.update(d.source["id"], config={**d.source["config"], "auto_fix_pr": True})
    d.raw["results"][0]["uuid"] = receipt["event_id"] if case != "lag" else str(uuid.uuid4())
    d.visible_at = 0
    original = d.h.adapter._request

    async def request(config, method, resource, **kwargs):
        value = await original(config, method, resource, **kwargs)
        filters = (kwargs.get("payload") or {}).get("filterGroup", [])
        if case != "drill" and any(f["key"] == "test" for f in filters):
            return {"results": [], "hasMore": False}
        return value

    d.h.adapter._request = request
    await d.worker.once()
    ready = d.h.store.ready()[0]
    assert ready["fix_context"]["trigger_unmarked"] is (case == "real")
    jobs = FixJobs(d.h.store)
    auto = jobs.enqueue(d.source, ready)
    assert auto["state"] == ("pending" if case == "real" else "skipped")
    manual = jobs.enqueue(d.source, ready, manual=True)
    assert manual["state"] == ("skipped" if case == "drill" else "pending")
    if case == "lag":
        assert manual["payload"]["drill_status_unconfirmed"] is True
        assert "not confirmed" in ready["evidence"]["issue"]["sample"]["drill_scope"]


@pytest.mark.parametrize("intent", ["push_intent", "pr_intent"])
async def test_retry_cannot_remove_unconfirmed_warning_from_uncertain_publication(setup, intent):
    o = setup
    receipt = await delivered(o)
    receipt["fix_context"]["trigger_unmarked"] = False
    jobs = FixJobs(o.h.store)
    jobs.enqueue(o.source, receipt, manual=True)
    job = jobs.claim()
    jobs.save(job, state="failed", **{intent: True, "reason": "synthetic publication interruption"})
    receipt["fix_context"]["trigger_unmarked"] = True
    retried = jobs.enqueue(o.source, receipt, manual=True)
    assert retried["payload"]["drill_status_unconfirmed"] is True
    assert retried["result"][intent] is True and retried["state"] == "pending"
