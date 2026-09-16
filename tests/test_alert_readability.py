"""Human-readable incident results retain precise evidence scope."""

import copy
import json
import uuid

import pytest

from extras.posthog.alerts import sampled_event_matches_trigger, sampled_exception
from src.core.alert_diagnosis import human_diagnosis, incident_label, source_evidence
from src.core.base import LLMResponse
from tests.test_alert_evidence_wait import delayed as delayed_fixture
from tests.test_alert_evidence_wait import restart
from tests.test_alert_exception_evidence import response, tracked_repo

delayed = delayed_fixture


def test_verdict_and_bold_headings_have_separate_paragraphs():
    actual = human_diagnosis(
        "The sampled exception is a declared drill. **Observations** An intentional throw. "
        "**Suspected cause:** The test route. **Missing evidence** No deployment check."
    )
    assert actual == (
        "The sampled exception is a declared drill.\n\n**Observations**\nAn intentional throw.\n\n"
        "**Suspected cause:**\nThe test route.\n\n**Missing evidence**\nNo deployment check."
    )
    assert human_diagnosis(actual) == actual


@pytest.mark.parametrize("description", [None, "", "   "])
def test_missing_description_falls_back_to_name_with_app(description):
    source = {"config": {"app": "example-app"}}
    assert incident_label(source, {"name": "TypeError", "description": description}) == "example-app: TypeError"


def test_description_title_uses_first_line_and_cannot_inject_markdown_or_mentions():
    source = {"config": {"app": "example-app"}}
    label = incident_label(
        source,
        {"name": "Error", "description": "[drill](bad) **@everyone**\x00\u202e /api/debug/boom\nprivate next line"},
    )
    assert label.startswith("example-app: drill") and "/api/debug/boom" in label
    assert not any(x in label for x in ("[", "]", "*", "@", "\x00", "\u202e", "private", "Error"))
    assert len(incident_label(source, {"description": "x" * 500})) == 80


@pytest.mark.parametrize("match", [True, False, None])
async def test_uuid_relation_is_private_and_only_exact_match_confirms_trigger(delayed, match):
    d = delayed
    receipt = d.h.store.pending()[0]
    event_id = receipt["event_id"]
    d.raw["results"][0]["uuid"] = event_id if match else str(uuid.uuid4()) if match is False else "invalid"
    d.visible_at = 0
    await d.worker.once()
    payload = json.loads(d.provider.complete.call_args.args[0][1].content)
    sample = payload["issue"]["sample"]
    assert sample["matches_trigger"] is match
    assert event_id not in json.dumps(payload)
    assert "private-person" not in json.dumps(payload) and "private-session" not in json.dumps(payload)
    ready = d.h.store.ready()[0]
    if match:
        assert sample["status"] == "The sampled exception is the triggering event."
        assert "not necessarily" not in json.dumps(payload)
        assert ready["result"].startswith("The triggering event is a declared drill.\n\n")
        assert ready["drill"] is True and "Deliberate drill:" in payload["issue"]["alert_context"]
    else:
        assert "not necessarily" in sample["status"] and ready["drill"] is False
    posts = [entry[3]["payload"] for entry in d.reads if entry[1] == "POST"]
    assert len(posts) == 2
    assert all(p["include"] == ["exception", "stacktrace", "release"] for p in posts)


@pytest.mark.parametrize("event_id", [None, "", "not-a-uuid", 1, {}, []])
def test_missing_or_malformed_trigger_identity_never_matches(event_id):
    assert sampled_event_matches_trigger(response(), event_id) is None


async def test_title_persists_from_first_summary_across_wait_restart_and_final_edit(delayed):
    d = delayed
    description = "Expected drill from /api/debug/boom"
    request = d.h.adapter._request

    async def described(config, method, resource, **kwargs):
        result = await request(config, method, resource, **kwargs)
        if method == "GET":
            result["description"] = description + "\nsecond line must not reach title"
        return result

    d.h.adapter._request = described
    await d.worker.once()
    first = d.room.messages[0].content.splitlines()[0]
    assert first.startswith("[sample: Expected drill from /api/debug/boom](")
    assert "[Error]" not in first and "second line" not in first
    assert d.h.store.pending()[0]["issue_title"] == "sample: Expected drill from /api/debug/boom"
    description = "Changed later summary"
    d.clock.now = 1005
    restart(d)
    await d.worker.once()
    assert d.room.messages[0].content.splitlines()[0] == first
    d.clock.now = 1030
    await d.worker.once()
    await d.worker.once()
    assert len(d.room.messages) == 1
    assert d.room.messages[0].content.splitlines()[0] == first
    assert "Drill sample received. Alert path works; no fix needed for this sample." in d.room.messages[0].content
    assert "Proposed fix for review" not in d.room.messages[0].content


async def test_matched_drill_result_has_own_verdict_and_heading_paragraphs(delayed):
    d = delayed
    d.raw["results"][0]["uuid"] = d.h.store.pending()[0]["event_id"]
    d.visible_at = 0
    d.provider.complete.return_value = LLMResponse(
        content="**Observations** Expected throw. **Missing evidence** No other behavior was tested."
    )
    await d.worker.once()
    await d.worker.once()
    text = d.room.messages[0].content
    assert "Drill received. Alert path works; no fix needed.\n\n" in text
    assert "The triggering event is a declared drill.\n\n**Observations**\n" in text
    assert "\n\n**Missing evidence**\n" in text
    assert "Proposed fix for review" not in text


async def test_failed_diagnosis_of_drill_never_claims_path_works(delayed):
    d = delayed
    d.raw["results"][0]["uuid"] = d.h.store.pending()[0]["event_id"]
    d.visible_at = 0
    d.provider.complete.return_value = LLMResponse(content="", stop_reason="error")
    await d.worker.once()
    await d.worker.once()
    assert "Diagnosis held:\n\n" in d.room.messages[0].content
    assert "Alert path works" not in d.room.messages[0].content


@pytest.mark.parametrize("frame", ["[eval1]", "unknown/handler.mjs", "../outside.ts"])
def test_existing_unmapped_frames_do_not_sample_unrelated_error_files(tmp_path, frame):
    repo = tracked_repo(tmp_path, {f"client/{n}.ts": "throw Error('unrelated')" for n in range(6)})
    evidence = source_evidence(str(repo), {"name": "Error", "sample": sampled_exception(response(frame))})
    assert evidence["snippets"] == [] and evidence["source_files"] == []
    assert evidence["selection"] == "No in-app frame maps to a tracked file."


def test_keyword_fallback_still_works_when_there_are_no_frames(tmp_path):
    repo = tracked_repo(tmp_path, {"client/handler.ts": "throw Error('keyword')"})
    raw = copy.deepcopy(response())
    raw["results"][0]["properties"]["$exception_list"][0]["stacktrace"]["frames"] = []
    evidence = source_evidence(str(repo), {"name": "Error", "sample": sampled_exception(raw)})
    assert [x["path"] for x in evidence["snippets"]] == ["client/handler.ts"]
