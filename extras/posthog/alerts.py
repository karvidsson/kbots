"""Opt-in PostHog lifecycle alert adapter. No generic model-visible HTTP tool.

Contract: PostHog sub-templates and CDP internal_events at e4aa96a7ea4679a5004a0ad1c2630c7992f5af80.
Created/reopened/spiking use event.uuid and the issue id in event.distinct_id.
An authenticated provisioning/delivery test is still required per installation.
"""

import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import aiohttp

from src.core.alert_channels import AlertError, UncertainOperationError, ensure_operation

HOSTS = {"https://eu.posthog.com", "https://us.posthog.com"}
EVENTS = {name: "$error_tracking_issue_" + name for name in ("created", "reopened", "spiking")}
MARKER = "KBOTS_ALERT_V1"
_WEBHOOK = re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/[^\s\"'<>]+")
_KEY = re.compile(r"\b(?:phx_|phc_|sk-)[A-Za-z0-9_-]{12,}")


def clean_text(value, limit=2000):
    text = _WEBHOOK.sub("[webhook redacted]", str(value or ""))
    text = _KEY.sub("[key redacted]", text)
    return text[:limit].replace("@", "＠")


def destination_summary(value):
    """Positive projection: never return inputs, bytecode or arbitrary response fields."""
    return {key: value.get(key) for key in ("id", "name", "type", "enabled", "template_id")}


def alert_heading(source):
    app = re.sub(r"[^a-z0-9-]", "", source["config"].get("app", "app"))[:41] or "app"
    return f"**{app}: issue alert**"


def issue_link(source, issue_id):
    config = source["config"]
    return f"[Open issue]({config['host']}/project/{config['project']}/error_tracking/{issue_id})"


def parse_event(source, text):
    modern = source["config"].get("message_format", 1) == 2
    original = text.strip()
    if modern:
        body, separator, envelope = original.rpartition("\n||")
        if not separator or not envelope.endswith("||") or not body.startswith(alert_heading(source) + "\n"):
            raise AlertError("Alert envelope is invalid")
        text = envelope[:-2]
    parts = text.strip().split()
    if len(parts) != (7 if modern else 6) or parts[0] != ("KBOTS_ALERT_V2" if modern else MARKER):
        raise AlertError("Alert envelope is invalid")
    _, source_id, nonce, kind, event_id, issue_id = parts[:6]
    if source_id != source["id"] or nonce != source["nonce"]:
        raise AlertError("Alert belongs to a different registration")
    if kind not in EVENTS.values():
        raise AlertError("Alert lifecycle event is not supported")
    if kind not in {EVENTS[k] for k in source["config"]["triggers"]}:
        raise AlertError("Alert lifecycle event is not subscribed")
    try:
        event_id, issue_id = str(uuid.UUID(event_id)), str(uuid.UUID(issue_id))
    except (ValueError, AttributeError):
        raise AlertError("Alert identifiers are invalid") from None
    result = {"event_id": event_id, "issue_id": issue_id, "kind": kind}
    if modern:
        if parts[6] not in {"drill", "unknown"} or not body.endswith("\n" + issue_link(source, issue_id)):
            raise AlertError("Alert heading or drill marker is invalid")
        result["drill"] = parts[6] == "drill"
    return result


def sampled_exception(data):
    """Positive projection of one sampled event. No identities or captured locals."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list) or len(data["results"]) > 1:
        raise AlertError("PostHog sampled-event response changed; diagnosis has no verified stack")
    if not data["results"]:
        return {
            "exceptions": [],
            "availability": "empty",
            "status": "No exception sample returned yet in the checked seven-day window. "
            "This can mean indexing delay or no matching events; an empty read does not distinguish them.",
        }
    event = data["results"][0]
    if not isinstance(event, dict) or not isinstance(event.get("properties"), dict):
        raise AlertError("PostHog sampled-event properties are unavailable")
    raw = event["properties"].get("$exception_list", [])
    if not isinstance(raw, list):
        raise AlertError("PostHog sampled exception list is invalid")
    exceptions, remaining = [], 24
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        summary = {
            k: clean_text(item[k], 120 if k == "type" else 1200)
            for k in ("type", "value")
            if isinstance(item.get(k), str)
        }
        stack = item.get("stacktrace")
        frames = stack.get("frames", []) if isinstance(stack, dict) else []
        selected = []
        if isinstance(frames, list):
            # Deepest application frames are usually closest to the throw site.
            for frame in reversed(frames[-100:]):
                if not isinstance(frame, dict) or frame.get("in_app") is not True or not remaining:
                    continue
                projected = {
                    k: clean_text(frame[k], 500 if k == "source" else 200)
                    for k in ("source", "resolved_name")
                    if isinstance(frame.get(k), str)
                }
                if "source" in projected:
                    value = projected["source"]
                    try:
                        value = urlparse(value).path if "://" in value else value.split("?", 1)[0].split("#", 1)[0]
                    except ValueError:
                        value = ""
                    projected["source"] = value
                if type(frame.get("line")) is int and 0 < frame["line"] < 10_000_000:
                    projected["line"] = frame["line"]
                if projected:
                    selected.append(projected)
                    remaining -= 1
        summary["frames"] = selected
        exceptions.append(summary)
    raw_releases = event["properties"].get("$exception_releases", [])
    records = (
        list(raw_releases.values())[:3]
        if isinstance(raw_releases, dict)
        else raw_releases[:3]
        if isinstance(raw_releases, list)
        else []
    )
    releases = []
    for record in records:
        if not isinstance(record, dict):
            continue
        release = {"version": clean_text(record["version"], 120)} if isinstance(record.get("version"), str) else {}
        metadata = record.get("metadata")
        git = metadata.get("git") if isinstance(metadata, dict) else None
        commit = git.get("commit_id") if isinstance(git, dict) else None
        if isinstance(commit, str) and re.fullmatch(r"[a-fA-F0-9]{7,64}", commit):
            release["commit_id"] = commit
        if release:
            releases.append(release)
    return {
        "exceptions": exceptions,
        "availability": "available",
        "releases": releases,
        "status": "One recent sampled exception event; not necessarily the triggering event",
    }


def sample_drill_status(sample, filtered):
    """Classify only the same sampled exception, never every event in its issue."""
    for data in (sample, filtered):
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("results"), list)
            or len(data["results"]) > 1
            or any(not isinstance(row, dict) for row in data["results"])
        ):
            return "unknown"
    if not sample["results"]:
        return "unknown"
    try:
        event_id = str(uuid.UUID(sample["results"][0]["uuid"]))
        if not filtered["results"]:
            return "unknown" if filtered.get("hasMore") else "unmarked"
        filtered_id = str(uuid.UUID(filtered["results"][0]["uuid"]))
    except (ValueError, TypeError, AttributeError, KeyError):
        return "unknown"
    return "drill" if event_id == filtered_id else "unknown"


class DestinationNotFoundError(AlertError):
    """An exact destination GET returned 404; listing confirmation is still required."""


class PostHogAdapter:
    name = "posthog"
    credential_hosts = {"eu.posthog.com", "us.posthog.com"}
    triggers = tuple(EVENTS)
    project_prompt = "What is the PostHog project URL? For example https://eu.posthog.com/project/123."

    @staticmethod
    def parse_project(text):
        url = urlparse(text)
        match = re.fullmatch(r"/project/([1-9][0-9]{0,11})(?:/.*)?", url.path)
        if url.netloc not in PostHogAdapter.credential_hosts or url.scheme != "https":
            raise AlertError(
                "Use an https://eu.posthog.com or https://us.posthog.com project URL, not the ingestion host"
            )
        if not match:
            raise AlertError("The PostHog URL needs a numeric project ID, for example /project/123/home")
        return {"host": f"https://{url.netloc}", "project": match[1]}

    parse_event = staticmethod(parse_event)

    def __init__(self, vault, store, session_factory=aiohttp.ClientSession):
        self.vault, self.store, self.session_factory = vault, store, session_factory

    @staticmethod
    def validate(config):
        if config.get("host") not in HOSTS:
            raise AlertError("Use https://eu.posthog.com or https://us.posthog.com")
        if not re.fullmatch(r"[1-9][0-9]{0,11}", str(config.get("project", ""))):
            raise AlertError("PostHog project id must be a positive number")
        if not config.get("triggers") or set(config["triggers"]) - EVENTS.keys():
            raise AlertError("Choose created, reopened, or spiking lifecycle events")
        if not re.fullmatch(r"secrets/[A-Za-z0-9_-]{1,100}", config.get("api_key", "")):
            raise AlertError("Supply a vault reference, never paste keys in chat")

    def _credential(self, config):
        raw = self.vault.get(config["api_key"])
        if not isinstance(raw, str) or not raw:
            raise AlertError("Required credential is missing from the vault")
        # Existing vault tokens remain in their original format. The administrator
        # selects the fixed private API host during setup; webhook input cannot.
        if re.fullmatch(r"[A-Za-z0-9_-]{20,2048}", raw):
            return raw
        try:
            entry = json.loads(raw)
            if (
                not isinstance(entry, dict)
                or set(entry) != {"host", "value"}
                or entry["host"] != config["host"].removeprefix("https://")
                or not isinstance(entry["value"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{20,2048}", entry["value"])
            ):
                raise ValueError("Invalid binding")
            return entry["value"]
        except (ValueError, TypeError):
            raise AlertError("Credential format or host binding is invalid") from None

    def _budget(self, host):
        now = time.time()
        # Shared across all sources on a host; conservative below vendor caps.
        with self.store.transaction():
            for period, maximum in ((60, 60), (3600, 1000)):
                scope, bucket = "posthog:" + host + ":" + str(period), int(now // period)
                row = self.store.db.execute(
                    "SELECT used FROM budgets WHERE scope=? AND bucket=?", (scope, bucket)
                ).fetchone()
                if row and row[0] >= maximum:
                    raise AlertError("PostHog request budget exhausted; retry later")
                self.store.db.execute(
                    "INSERT INTO budgets VALUES(?,?,1) ON CONFLICT(scope,bucket) DO UPDATE SET used=used+1",
                    (scope, bucket),
                )

    @staticmethod
    def _incident_resource(resource):
        if resource in {"error_tracking/issues/", "error_tracking/issues/?limit=1"}:
            return True
        match = re.fullmatch(r"error_tracking/issues/([^/]+)/", resource)
        if not match:
            return False
        try:
            return str(uuid.UUID(match[1])) == match[1]
        except ValueError:
            return False

    @staticmethod
    def sample_request(issue_id, *, date_range=None, drill=False):
        if date_range is None:
            end = datetime.fromtimestamp(time.time(), UTC).replace(microsecond=0)
            date_range = {
                "date_from": (end - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "date_to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        body = {
            "issueId": str(uuid.UUID(issue_id)),
            "limit": 1,
            "onlyAppFrames": True,
            "filterTestAccounts": False,
            "include": ["exception", "stacktrace", "release"],
            "dateRange": dict(date_range),
        }
        if drill:
            body["filterGroup"] = [{"key": "test", "value": ["true"], "operator": "exact", "type": "event"}]
        return body

    @classmethod
    def _sample_request_allowed(cls, resource, payload):
        if resource != "error_tracking/query/issue_events/" or not isinstance(payload, dict):
            return False
        try:
            window = payload["dateRange"]
            if not isinstance(window, dict) or set(window) != {"date_from", "date_to"}:
                return False
            dates = []
            for name in ("date_from", "date_to"):
                value = window[name]
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
                    return False
                dates.append(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC))
            if dates[1] - dates[0] != timedelta(days=7) or not -60 <= time.time() - dates[1].timestamp() <= 300:
                return False
            return (
                payload == cls.sample_request(payload["issueId"], date_range=window, drill="filterGroup" in payload)
                and type(payload["limit"]) is int
                and payload["onlyAppFrames"] is True
                and payload["filterTestAccounts"] is False
            )
        except (ValueError, TypeError, AttributeError, KeyError):
            return False

    async def _request(self, config, method, resource, *, provisioning=False, payload=None):
        self.validate(config)
        # Provisioning includes private ownership GETs as well as mutations.
        # The default incident path is limited to these fixed issue reads.
        sampled_read = method == "POST" and self._sample_request_allowed(resource, payload)
        if not provisioning and method != "GET" and not sampled_read:
            raise AlertError("Incident requests cannot perform mutations or arbitrary queries")
        if not provisioning and not sampled_read and (not self._incident_resource(resource) or payload is not None):
            raise AlertError("Incident requests are limited to fixed issue reads")
        token = self._credential(config)
        self._budget(config["host"])
        url = f"{config['host']}/api/projects/{config['project']}/{resource}"
        try:
            async with self.session_factory() as session:
                async with session.request(
                    method,
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=25),
                    allow_redirects=False,
                ) as response:
                    if (
                        response.status == 404
                        and provisioning
                        and method == "GET"
                        and re.fullmatch(r"hog_functions/[0-9a-f-]{36}/", resource)
                    ):
                        raise DestinationNotFoundError("PostHog destination GET returned HTTP 404")
                    if (
                        response.status == 404
                        and not provisioning
                        and method == "GET"
                        and re.fullmatch(r"error_tracking/issues/[0-9a-f-]{36}/", resource)
                    ):
                        raise AlertError("PostHog could not find this issue (HTTP 404); diagnosis is held")
                    if response.status not in (200, 201, 204):
                        # Never include response bodies, request data, or exceptions.
                        raise AlertError(
                            f"PostHog returned HTTP {response.status}; "
                            "check host, project, scopes and feature availability"
                        )
                    if response.status == 204:
                        return {}
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 2_000_000:
                            raise AlertError("PostHog response exceeded the bounded read size")
                    return json.loads(raw)
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            raise AlertError("PostHog request failed or its outcome is unknown") from None

    async def check_credentials(self, config):
        self.validate(config)
        # The same key serves both paths. This GET is not proof of write scope.
        value = await self._request(config, "GET", "error_tracking/issues/?limit=1")
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise AlertError("PostHog issue-list contract changed")
        return {"read_access": True, "write_access": "not yet exercised"}

    @staticmethod
    def _destination_payload(source, webhook_url):
        config = source["config"]
        name = f"kbots-alert-{source['id']}-r{source['revision']}"
        content = f"{MARKER} {source['id']} {source['nonce']} {{event.event}} {{event.uuid}} {{event.distinct_id}}"
        # Absence means the exact v1 template, including existing live revisions.
        if config.get("message_format", 1) == 2:
            content = (
                alert_heading(source) + "\n{event.properties.test == true ? 'Drill. ' : ''}**"
                "{event.event == '$error_tracking_issue_created' ? 'New issue' : "
                "event.event == '$error_tracking_issue_reopened' ? 'Reopened issue' : 'Spiking issue'}**: "
                "{substring(event.properties.name, 1, 150)}\n"
                + issue_link(source, "{event.distinct_id}")
                + "\n||"
                + content.replace(MARKER, "KBOTS_ALERT_V2", 1)
                + " {event.properties.test == true ? 'drill' : 'unknown'}||"
            )
        return {
            "name": name,
            "type": "internal_destination",
            "template_id": "template-discord",
            "enabled": True,
            "filters": {
                "source": "internal-events",
                "events": [{"id": EVENTS[k], "type": "events"} for k in config["triggers"]],
            },
            "inputs": {
                "webhookUrl": {"value": webhook_url},
                "content": {"value": content},
                "allowedMentions": {"value": "none"},
            },
        }

    @staticmethod
    def _matches_destination(item, payload, *, enabled=True):
        # Compare privately. Never persist or return credential-bearing fields.
        if not isinstance(item, dict):
            return False
        # HogFunctionSerializer makes template_id write-only. Full create,
        # retrieve and update responses identify the template via template.id.
        # Require that read projection; a request echo is not ownership proof.
        template = item.get("template")
        if not isinstance(template, dict) or template.get("id") != payload["template_id"]:
            return False
        if "template_id" in item and item["template_id"] != template["id"]:
            return False
        return (
            all(item.get(k) == payload[k] for k in ("name", "type"))
            and isinstance(item.get("enabled"), bool)
            and (enabled is None or item["enabled"] is enabled)
            and isinstance(item.get("filters"), dict)
            and item["filters"].get("source") == "internal-events"
            and item["filters"].get("events") == payload["filters"]["events"]
            and isinstance(item.get("inputs"), dict)
            and all(
                isinstance(item["inputs"].get(k), dict) and item["inputs"][k].get("value") == v["value"]
                for k, v in payload["inputs"].items()
            )
        )

    async def _owned_destination(self, source, destination_id, *, enabled=True):
        """Require a completed creation record AND fresh exact remote ownership."""
        try:
            destination_id = str(uuid.UUID(destination_id))
            row = self.store.db.execute(
                "SELECT state,result FROM operations WHERE source_id=? AND revision=? AND step='destination'",
                (source["id"], source["revision"]),
            ).fetchone()
            if not row or row["state"] != "complete" or json.loads(row["result"]) != {"id": destination_id}:
                raise ValueError("Not owned")
        except (ValueError, TypeError, AttributeError):
            raise AlertError("Destination is not owned by this setup revision") from None
        webhook_url = self.vault.get(f"secrets/alert-webhook-{source['id']}-r{source['revision']}")
        if not webhook_url:
            raise AlertError("Owned webhook credential is unavailable; refusing destination mutation")
        payload = self._destination_payload(source, webhook_url)
        data = await self._request(source["config"], "GET", f"hog_functions/{destination_id}/", provisioning=True)
        if (
            not isinstance(data, dict)
            or data.get("id") != destination_id
            or not self._matches_destination(data, payload, enabled=enabled)
        ):
            raise AlertError("Owned destination changed; refusing mutation until reviewed")
        return data, payload

    async def _find_destinations(self, source, payload, *, enabled=True):
        found = []
        for offset in range(0, 2000, 100):
            data = await self._request(
                source["config"], "GET", f"hog_functions/?limit=100&offset={offset}", provisioning=True
            )
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise AlertError("PostHog destination-list contract changed")
            for item in data["results"]:
                if not isinstance(item, dict):
                    raise AlertError("PostHog destination-list contract changed")
                if item.get("name") == payload["name"]:
                    try:
                        destination_id = str(uuid.UUID(item["id"]))
                    except (KeyError, ValueError, TypeError, AttributeError):
                        raise AlertError("PostHog destination identifier is invalid") from None
                    # Minimal list cards omit inputs and the nested template.
                    detail = await self._request(
                        source["config"], "GET", f"hog_functions/{destination_id}/", provisioning=True
                    )
                    if (
                        not isinstance(detail, dict)
                        or detail.get("id") != destination_id
                        or not self._matches_destination(detail, payload, enabled=enabled)
                    ):
                        raise AlertError("Existing setup destination differs; review required")
                    found.append({"id": destination_id})
            if not data.get("next"):
                return found
        raise AlertError("PostHog destination lookup was incomplete")

    async def reconcile_destination(self, source):
        """Recover an interrupted create for cleanup. This path never creates."""
        row = self.store.db.execute(
            "SELECT state,result FROM operations WHERE source_id=? AND revision=? AND step='destination'",
            (source["id"], source["revision"]),
        ).fetchone()
        if not row:
            if source["config"].get("destination_id"):
                raise AlertError("Destination ownership record is missing; review required")
            return None  # No destination dispatch was begun for this revision.
        if row["state"] == "complete":
            return json.loads(row["result"])["id"]  # Cleanup revalidates exact remote ownership.
        webhook = self.vault.get(f"secrets/alert-webhook-{source['id']}-r{source['revision']}")
        if not webhook:
            raise AlertError("Owned webhook credential is unavailable; cleanup requires review")
        matches = await self._find_destinations(source, self._destination_payload(source, webhook), enabled=None)
        if len(matches) != 1:
            raise UncertainOperationError("Destination creation remains uncertain; no new destination submitted")
        self.store.finish_operation(source, "destination", matches[0])
        return matches[0]["id"]

    async def destination(self, source, webhook_url):
        self.store.require_setup(source)
        self.store.remember_revision(source)
        config = source["config"]
        payload = self._destination_payload(source, webhook_url)

        def matching(item):
            return self._matches_destination(item, payload)

        async def find():
            return await self._find_destinations(source, payload)

        async def create():
            self.store.require_setup(source)
            data = await self._request(config, "POST", "hog_functions/", provisioning=True, payload=payload)
            if not matching(data):
                raise AlertError("Destination response does not match the requested configuration")
            return {"id": str(uuid.UUID(data["id"]))}

        # A completed local step is not proof the remote object is unchanged.
        # A resumed setup must not activate a disabled or repointed destination.
        prior = self.store.db.execute(
            "SELECT state FROM operations WHERE source_id=? AND revision=? AND step='destination'",
            (source["id"], source["revision"]),
        ).fetchone()
        result = await ensure_operation(self.store, source, "destination", find, create)
        if prior and prior["state"] == "complete":
            data = await self._request(config, "GET", f"hog_functions/{uuid.UUID(result['id'])}/", provisioning=True)
            if str(data.get("id")) != result["id"] or not matching(data):
                raise AlertError("Recorded destination changed; setup cannot resume without review")
        return result

    async def test_delivery(self, source, destination_id):
        owned, configuration = await self._owned_destination(source, destination_id)
        template = owned.get("template")
        if (
            not isinstance(template, dict)
            or template.get("id") != "template-discord"
            or not isinstance(template.get("code"), str)
            or not template["code"].strip()
            or owned.get("hog") != template["code"]
            or not isinstance(template.get("inputs_schema"), list)
        ):
            raise AlertError("Owned destination code differs from its template; test requires review")
        # Invocation requires a complete configuration unless use_draft=true.
        # Reconstruct expected inputs privately, using the verified template's
        # source/schema. Never forward response bytecode or unrelated fields.
        configuration = {
            **configuration,
            "hog": template["code"],
            "inputs_schema": template["inputs_schema"],
        }
        config = source["config"]
        data = await self._request(config, "GET", "error_tracking/issues/?limit=1")
        issues = data.get("results", [])
        if not issues:
            raise AlertError("A real issue is required for the diagnostic setup test")
        issue_id = str(uuid.UUID(issues[0]["id"]))
        # Fixed event UUID makes an ambiguous test dispatch safe to reconcile at intake.
        event_id = str(uuid.uuid5(uuid.UUID(source["id"]), source["nonce"]))
        self.store.require_setup(source)
        previous = self.store.intent(source, "delivery-test")
        if previous:
            return  # Provisional source stays pending until a verified receipt arrives.
        await self._request(
            config,
            "POST",
            f"hog_functions/{uuid.UUID(destination_id)}/invocations/",
            provisioning=True,
            payload={
                "configuration": configuration,
                "mock_async_functions": False,
                "globals": {
                    "event": {
                        "uuid": event_id,
                        "event": EVENTS[config["triggers"][0]],
                        "distinct_id": issue_id,
                        "properties": {"name": "Setup delivery test", "test": True},
                    },
                    "project": {
                        "id": int(config["project"]),
                        "name": config["app"],
                        "url": f"{config['host']}/project/{config['project']}",
                    },
                },
            },
        )
        self.store.finish_operation(source, "delivery-test", {"event_id": event_id})

    async def issue(self, source, issue_id):
        issue_id = str(uuid.UUID(issue_id))
        data = await self._request(source["config"], "GET", f"error_tracking/issues/{issue_id}/")
        if str(data.get("id")) != issue_id:
            raise AlertError("PostHog returned a different issue")
        # Deliberately exclude event properties, people, destinations and arbitrary nesting.
        fields = (
            "id",
            "name",
            "description",
            "status",
            "severity",
            "created_at",
            "first_seen",
            "last_seen",
            "occurrences",
            "users",
            "volume",
        )
        issue = {key: clean_text(data[key]) for key in fields if isinstance(data.get(key), (str, int, float))}
        query = self.sample_request(issue_id)
        sample = await self._request(source["config"], "POST", "error_tracking/query/issue_events/", payload=query)
        issue["sample"] = sampled_exception(sample)
        if issue["sample"]["availability"] == "empty":
            issue["sample"]["drill_status"] = "unknown"
            return issue
        # Both reads share an absolute window; a later ingestion can still change
        # membership, so matching the sampled event identity remains necessary.
        filtered = await self._request(
            source["config"],
            "POST",
            "error_tracking/query/issue_events/",
            payload=self.sample_request(issue_id, date_range=query["dateRange"], drill=True),
        )
        issue["sample"]["drill_status"] = sample_drill_status(sample, filtered)
        issue["sample"]["drill_scope"] = (
            "The recent sampled exception only, not the triggering lifecycle event or every event in this issue. "
            "Unmarked means no declared test=true match; it is not proof of a production defect."
        )
        return issue

    async def disable(self, source, destination_id):
        data, payload = await self._owned_destination(source, destination_id, enabled=None)
        if data["enabled"] is False:
            return  # Already disabled, including a previous unacknowledged PATCH.
        result = await self._request(
            source["config"],
            "PATCH",
            f"hog_functions/{uuid.UUID(destination_id)}/",
            provisioning=True,
            payload={"enabled": False},
        )
        if (
            not isinstance(result, dict)
            or result.get("id") != data["id"]
            or not self._matches_destination(result, payload, enabled=False)
        ):
            raise AlertError("Destination disable was not confirmed; local intake remains revoked")
        # Teardown completion requires a separate read, not only a PATCH echo.
        await self._owned_destination(source, destination_id, enabled=False)

    async def _confirm_destination_absent_from_list(self, source, destination_id):
        # A missing exact GET alone can also mean lost access. Require a readable,
        # complete list and compare IDs, including disabled destinations.
        seen = set()
        for offset in range(0, 2000, 100):
            page = await self._request(
                source["config"], "GET", f"hog_functions/?limit=100&offset={offset}", provisioning=True
            )
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("results"), list)
                or "next" not in page
                or len(page["results"]) > 100
            ):
                raise AlertError("PostHog removal listing is incomplete or malformed")
            for item in page["results"]:
                try:
                    item_id = str(uuid.UUID(item["id"]))
                    if item_id != item["id"] or item_id in seen:
                        raise ValueError("Unreliable pagination")
                except (KeyError, TypeError, AttributeError, ValueError):
                    raise AlertError("PostHog removal listing is incomplete or malformed") from None
                if item_id == destination_id:
                    raise AlertError("Destination removal is not confirmed by the listing")
                seen.add(item_id)
            if page["next"] is None:
                return
            if not isinstance(page["next"], str) or not page["next"] or len(page["results"]) != 100:
                raise AlertError("PostHog removal listing is incomplete or malformed")
        raise AlertError("PostHog removal listing exceeded the bounded scan; review required")

    async def remove(self, source, destination_id):
        """Terminal channel cleanup; unsubscribe continues to use disable()."""
        try:
            await self._owned_destination(source, destination_id, enabled=None)
        except DestinationNotFoundError:
            # Local creation ownership and the webhook reference were checked
            # before this GET. This also reconciles a lost soft-delete response.
            await self._confirm_destination_absent_from_list(source, str(uuid.UUID(destination_id)))
            return
        destination_id = str(uuid.UUID(destination_id))
        resource = f"hog_functions/{destination_id}/"
        await self._request(
            source["config"], "PATCH", resource, provisioning=True, payload={"enabled": False, "deleted": True}
        )
        # `deleted` is write_only. Neither its omission nor a PATCH echo proves
        # removal. Keep ownership records until both independent reads agree.
        try:
            await self._request(source["config"], "GET", resource, provisioning=True)
        except DestinationNotFoundError:
            await self._confirm_destination_absent_from_list(source, destination_id)
            return
        raise AlertError("Destination removal is not confirmed by its exact GET")
