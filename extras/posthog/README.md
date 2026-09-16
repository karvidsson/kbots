# PostHog application alerts

Opt-in adapter for [application alert channels](../../docs/ALERTS.md). Use the
private API host `eu.posthog.com` or `us.posthog.com`, not the ingestion host.
One vault reference supplies the API key for provisioning and diagnosis.
An existing token can be used directly, including an all-access key, without
rewriting it. Its name is not evidence of scope. The runtime adapter permits
fixed issue GETs and one fixed read-only issue-sample POST; the diagnostic model receives neither the key nor an
HTTP tool. Provisioning needs Hog function read/write access for reconciliation,
creation, test invocation, disabling and soft-deleting the owned destination.
A successful issue GET proves read access to that endpoint; it does not establish the key's
complete scope set or prove write access.

Before invoking, disabling or soft-deleting a destination, the adapter requires
its exact ID in the completed creation record for that source revision and
re-reads the object privately. The template, lifecycle filters, nonce-bearing content and
owned webhook URL must still match. It refuses foreign or changed objects even
when the key could mutate them. Repeated disabling of an already-disabled owned
object is a no-op. This is a pre-mutation check, not a vendor-side transaction
or protection against an administrator changing the object concurrently.

The implementation creates an enabled `internal_destination` Hog function
with `template_id: template-discord`. Its filters select `internal-events` and
the chosen lifecycle events. Inputs are `webhookUrl`, a structured `content`
template and `allowedMentions: none`. PostHog compiles bytecode server-side.
No insight alert, native error-tracking alert rule or Slack integration is
created by this path.

Supported events are `created`, `reopened` and `spiking`. The source
sub-templates use `$error_tracking_issue_created`,
`$error_tracking_issue_reopened` and `$error_tracking_issue_spiking`. The event
UUID is the receipt deduplication key; `event.distinct_id` carries the issue
UUID. The adapter accepts no arbitrary URLs, queries or tool instructions
from the webhook text. A real issue must already exist for the initial
diagnostic delivery test. The invocation uses `mock_async_functions: false`.
For spiking, verify that the project spike detector is configured and running
before enabling the subscription. A direct test invocation exercises delivery,
not the detector. One project-wide spike can emit many issue events in a burst.

The Discord webhook input is not safely assumed write-only. Hog function
responses can contain its full URL in both `inputs.webhookUrl.value` and
compiled bytecode. All destination inspection happens inside the adapter;
only the validated destination UUID is returned or journaled. HTTP errors
surface fixed status text, never response bodies, request payloads or raw
exceptions. Positive projection excludes arbitrary event properties, persons,
sessions, captured locals and unrecognized nested objects. Do not use a generic HTTP tool to dump these objects.

Read-only source verification, pinned to PostHog commit
`e4aa96a7ea4679a5004a0ad1c2630c7992f5af80`:

- [Event trigger reference](https://github.com/PostHog/posthog/blob/e4aa96a7ea4679a5004a0ad1c2630c7992f5af80/products/error_tracking/skills/authoring-error-tracking-alerts/references/event-triggers.md)
  distinguishes the three lifecycle events and documents per-issue spike bursts.
- [Destination sub-templates](https://github.com/PostHog/posthog/blob/e4aa96a7ea4679a5004a0ad1c2630c7992f5af80/frontend/src/scenes/hog-functions/sub-templates/sub-templates.ts)
  define the supported lifecycle filters.
- [Lifecycle event production](https://github.com/PostHog/posthog/blob/e4aa96a7ea4679a5004a0ad1c2630c7992f5af80/products/error_tracking/backend/logic/lifecycle_events.py)
  binds the issue ID to `distinct_id`.
- [Error Tracking route manifest](https://github.com/PostHog/posthog/blob/e4aa96a7ea4679a5004a0ad1c2630c7992f5af80/products/error_tracking/manifest.tsx)
  defines direct `/error_tracking/<issue-id>` links.
- [PostHog OpenAPI schema](https://app.posthog.com/api/schema/?format=json)
  describes Hog function creation and `/hog_functions/{id}/invocations/`.

These are source/schema contracts, not evidence of an authenticated
create-and-deliver run. Leave existing manually created destinations alone.
Creating, disabling or rotating a new destination is an explicit setup action.

Deleting the Discord channel soft-deletes every journaled owned destination via
PATCH `enabled: false, deleted: true`. Removal requires an exact GET returning
404 and absence from a complete paginated list. PostHog's `deleted` field is
write-only; it cannot be verified by reading that field back. A lost PATCH
response can be reconciled by these reads without repeating the mutation.
Unsubscribe keeps disabling only, preserving its channel reservation and vendor
object. Failures retain the ownership journal for durable cleanup retries.

## Readable messages and drills

New sources explicitly select message format 2. Content starts with the app,
issue title, lifecycle event and issue link, then a spoiler containing the bound
`KBOTS_ALERT_V2` envelope. Its final token is `drill` only when the lifecycle
event has boolean `test: true`; otherwise it is `unknown`. The referenced event
contract spreads originating exception properties onto created/reopened events,
but not spiking events. Manual transitions can also lack the marker. The setup
invocation supplies a test name/marker and is independently recognized by its
deterministic event UUID. The drill bit comes from the bound lifecycle envelope; the fixed sampled-event
read described below does not return custom event properties such as `test`.

Sources without `message_format: 2` retain the exact original `KBOTS_ALERT_V1`
content. The adapter does not accept an arbitrary alternate payload during an
ownership check: it reconstructs the one template selected by that revision.
Existing revisions, their snapshots and vendor resources require no migration.
Their original envelope lacks a drill bit, so natural drill classification is
unknown until a fresh format-2 registration is explicitly provisioned. The new
Hog content expression and natural event delivery require live acceptance on the
deployed revision; offline transport doubles are not evidence of vendor delivery.

## Exception evidence

In addition to the summary GET, incident processing may POST only to
`error_tracking/query/issue_events/` with exactly this body:

```json
{"issueId":"<canonical issue UUID>","limit":1,"onlyAppFrames":true,"filterTestAccounts":false,"include":["exception","stacktrace","release"]}
```

The pinned [query view](https://github.com/PostHog/posthog/blob/e4aa96a7ea4679a5004a0ad1c2630c7992f5af80/products/error_tracking/backend/presentation/views/query.py)
marks this as `error_tracking:read`. It samples the most recent event in a default
seven-day window. This is not necessarily the exception that triggered the
lifecycle notification. Missing samples are stated explicitly.

The runtime rejects any extra field, larger limit, different include group,
false application-frame filter or alternate endpoint on this path. It never
requests `code_variables`, `environment`, `navigation` or `correlation`.
The [response normalizer](https://github.com/PostHog/posthog/blob/e4aa96a7ea4679a5004a0ad1c2630c7992f5af80/products/error_tracking/backend/facade/query_utils.py)
still returns `distinct_id` alongside events, so a second positive projection is
mandatory. Only up to three exception type/value pairs and 24 application frames
(source, resolved name and line) reach diagnosis. Captured variables, identities,
sessions and arbitrary release metadata are not forwarded. Up to three release
versions and validated Git commit hashes are retained for revision comparison. Frame URL query strings and
credentials are discarded. Existing response-size, credential-host, redirect and
request-budget limits apply to this read too.
