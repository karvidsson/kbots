# Application alert channels

The service receives application incidents in a private Discord channel, reads a
bounded incident/source sample and posts a diagnosis. Eligible real incidents
continue to an isolated, tested fix PR linked from that same card. The service
does not merge PRs, change production or resolve issues.

This feature is opt-in. The adapter lives in `extras/`; it is not a default
tool, and no model can call its provisioning operations.

The application incident feature uses `src/core/alert_channels.py`,
`alert_credentials.py`, `alert_diagnosis.py`, `alert_lifecycle.py`, and the
`alert_fix_*` modules, with Discord routing in
`src/connectors/discord_alerts.py`. The existing `src/core/alerts.py` and
`alert_details.py` handle the platform's operational alert notices and details;
those modules retain their separate purpose.

## Enable and set up

Use an installation running from the repository, including the chosen extra.
The wheel currently packages `src` only. Configure the responsible agent and
Discord account normally, then add:

```yaml
alerts:
  enabled: true
  repository_roots:
    - /srv/repos
  adapters:
    posthog: extras.posthog.alerts:PostHogAdapter
```

Restart through the installation's normal reviewed deployment process. Never
enable this merely because a unit test or API schema passed: perform the live
acceptance checks below on the exact deployed version.

As a configured Discord administrator, DM the responsible bot and run
`/alerts create service:posthog` or `/alert posthog`. Supply the application
name, Git repository URL or local path, and private API project URL. A URL inside
a sentence is accepted. The bot finds existing vault key names, choosing a sole
match or offering a numbered list. A sole server is selected automatically;
multiple servers accept a number, name or ID. Event selection defaults to all
three; reply `yes`, `all`, an empty message or a comma-separated subset. The bot shows the concrete resources and event
selection, then asks "Open a fix PR automatically for real errors? (yes/no)".
Both the automatic-PR question and the final summary have bot-seeded ✅ and 🔴
reactions. On the question, these mean yes and no. On the summary, they mean
create and cancel. Only the setup user's reaction on the exact saved DM prompt
counts; bot seeds and other users do not authorize anything. Bindings persist
across restart and are invalidated by typed answers or changed setup state.
No provisioning happens before the final confirmation. Typed `yes`/`no` and
`CREATE`/`CANCEL` remain supported, including in operator answer files.
If seeding fails, the bot says reaction controls are unavailable and keeps the
typed prompt usable. A failed seed does not fail setup.

Enter the app's natural name, including spaces and capitals. For example,
`Example App` becomes `alerts-example-app`. Setup lowercases the
channel suffix, folds decomposable accents, removes invisible formatting marks,
turns separators into hyphens and shortens it to 41 characters. A name with no
usable ASCII letters or digits uses `alerts-app`. The creation confirmation shows the
resulting channel name before any resources are created.

The selected vault reference supplies both provisioning and incident reads,
for example `secrets/posthog-api-key`. Existing plain tokens are accepted without
rewriting the vault or requiring the key to be entered again. The final setup
confirmation includes the API host and reference. Credential names do not prove
scopes; an all-access key remains all-access, while the incident path permits
only fixed issue reads and two strictly shaped read-only sample POST variants, and
never gives that key to the model.

For a repository URL, setup finds local clones under `repository_roots` by
reading their local Git remote URLs. HTTPS and SSH forms, optional `.git`,
trailing slashes, and case differences match the same host/namespace/repository;
embedded credentials are ignored and never reflected in status messages.
Exactly one match is required. No match names the searched roots; multiple
matches list candidate paths so the administrator can choose one explicitly.
The stored value remains an absolute local path. Setup never fetches or clones.
Local paths, including repositories without remotes, remain supported.
Setup announces the resolved clone before asking for the project URL and repeats
it in the CREATE confirmation. Paths under the invoking home directory display
with `~`; the stored path remains absolute.

Discovery searches at most four directory levels below each root, skips Git
metadata and common dependency/build directories, and is bounded to 2,000
directories, 20,000 entries and 15 seconds. Git reads have a two-second timeout.
Search errors and exhausted budgets refuse selection from a partial result.
Resolved paths must remain within the configured roots, including symlink
aliases. Overlapping roots and aliases of the same clone are deduplicated.
Git configuration includes and inherited Git environment overrides are ignored;
use an explicit local path if a remote exists only in an included configuration.

For a new key, use hidden terminal input instead of Discord. Only when no matching vault name exists does the setup prompt
give the running service's socket path:

```sh
python scripts/alert-credential.py \
  --socket /srv/state/application-alerts/credentials.sock \
  --key secrets/posthog-api-key --host eu.posthog.com
```

This command requires a terminal and a private Unix socket owned by the local
user. It writes a host-bound credential bundle to the running encrypted Fernet
vault. Use a new reference when adding a bundle if other tools expect the plain
format. Do not run it to replace a working existing token. Bundled credentials
require an exact host match; plain tokens use the administrator-selected private
API host, restricted to the adapter's fixed allowlist. Redirects are disabled.
Existing owned credential directories have their permissions repaired to 0700
before binding the socket. The listener starts accepting only after its socket
has mode 0600. Startup failure closes it and removes only the captured socket;
repeated cleanup is safe. Entry failures log the exception type without its
message, traceback or input. The socket offers no credential read endpoint. It uses the running service to
avoid stale cached vault state and refuses environment-only vaults. This is a
boundary against incident content, not an administrator with the same OS account.

Setup creates a private channel accessible to the requesting human and bot,
then a webhook and service destination. The listener is provisionally
registered before the destination is enabled and the test is sent. Setup
becomes active only after the exact test event has been received, its issue
fetched, a restricted diagnosis produced and the result delivered to Discord.
A successful vendor HTTP response alone leaves setup provisional. Activation queues
one durable success notice to the initiating DM with the channel link. Failed
test diagnoses and held provisioning/result delivery also report a safe reason
there. Notices survive restart and reconcile lost send acknowledgements.

Only a draft awaiting an answer consumes setup DM messages. During local lookup,
provisioning, provisional monitoring, or a paused registration, ordinary messages
continue to the agent. A resource-free draft idle for 30 minutes is cancelled
on the next DM, with one expiry notice; that same message reaches the agent.
Exceptions log their type and stack locations, excluding exception values,
source lines, locals and vendor payloads. User-facing errors distinguish invalid
input, missing access, missing resources and timeouts.

Use `/alerts status` to see setup state and pending diagnoses. Commands select a
sole registration automatically. With several, supply its app name, channel ID
or the existing registration ID in `source_id`.
`/alerts resume` reconciles a previously confirmed interrupted setup; it does
not blindly repeat a create request whose result is unknown. It can also
resume a full queue after review. `unsubscribe` immediately revokes local
intake and schedules durable cleanup of its owned destinations. If the vendor
operation fails, intake stays revoked; cleanup retries and reports to the
responsible agent's home channel. `status` includes cleanup state and attempt
count. `resume` on a cleanup record retries cleanup only, never setup or diagnosis.
`rotate` revokes the old registration before replacing its webhook and
destination and repeating verification. Old Discord webhooks are retained
but cannot wake a diagnosis. Manual cleanup can remove them after review.
There is no transfer or automatic retry of an uncertain test invocation in
Phase 1. Inspect the recorded setup rather than deleting its journal.

## Local operator setup rehearsal

A machine operator can exercise the real setup conversation without a human DM
or a second Discord gateway. This optional bridge runs inside the existing alert
service. It shares that service's store, encrypted vault, bot REST client,
provisioning locks, receipt worker and teardown. The terminal client opens neither
a vault nor a database. It is not registered as a chat command, model tool or HTTP
endpoint. Like local credential entry, this does not restrict an administrator
who already controls the service's OS account and key file.

After reviewing the deployment, enable `operator_rehearsal: true` inside the
`alerts` configuration and restart normally. The default is off. The service
binds `operator.sock` beside `credentials.sock`, with mode 0600 in an owned 0700
directory. Both peers must have the service UID; each request additionally proves
read access to the same private, owned vault key file using a fresh challenge.
The key is never transmitted. Missing, public, symlinked, non-regular or oversized
key files are refused. Failure to start this optional listener is logged and
leaves normal monitoring running. Disabling the listener stops new operator
connections; the running lifecycle still expires existing rehearsals.

Create a private JSON file containing the literal answers, one string per step.
For example, when the vault presents more than one matching name:

```json
[
  "Example App",
  "the repository is https://code.example/team/example.git",
  "https://eu.posthog.com/project/123/home",
  "use existing one",
  "secrets/posthog-api-key",
  "all",
  "no",
  "CREATE"
]
```

Choices depend on the real vault and server inventory. Inspect the prompts and
use their actual names; this example does not assert that a credential exists.
The file accepts at most 32 answers of 2,000 characters and is capped at 128 KiB.
The final `CREATE` runs real provisioning, including a vendor test and a metered
restricted diagnosis. Rehearsal is not a dry run.

```sh
python scripts/alert-setup-rehearsal.py \
  --socket /srv/state/application-alerts/operator.sock \
  --parent FULL_ACTIVE_REGISTRATION_UUID --account example-bot \
  --inputs /srv/operator/answers.json --journal /srv/operator/rehearsal.json

python scripts/alert-setup-rehearsal.py \
  --socket /srv/state/application-alerts/operator.sock \
  --journal /srv/operator/rehearsal.json --action status
```

The default key path uses the installation's vault-key resolver; `--key-file`
can select the same file used by the service. The client refuses before any
socket connection or journal change unless that key file passes its local gate.
A new journal is exclusively created with mode 0600 before the first request.
Keep it. The terminal prints prompts and replies and retains them in the journal.
Activation and held notices are stored by the real lifecycle in the local status
transcript, not sent to a fabricated human DM. Use `--action status` after the
worker runs: finishing the input list does not establish activation.

A rehearsal is explicitly bound to one active parent registration, account,
responsible agent and revision. It can provision only the parent's exact service,
API host, project, resolved repository and server. Its normalized name gains
`-rehearsal` and its initiating identity is the selected bot, labelled as a local
operator in the journal. The resulting private channel permits that bot and
server administrators; no human's identity or access is borrowed. The ordinary
DM duplicate guard remains intact. The sole exception allows one retained
rehearsal alongside its explicitly selected parent. The parent is never changed.

Answers call `DiscordAlerts.answer()` under the existing source lock. Completed
steps with identical inputs replay their recorded response. Changed inputs,
out-of-order steps and uncertain outcomes refuse automatic replay. After a lost
connection or restart, inspect `status`, then use `--action resume` explicitly.
Resume reconciles existing provisioning intents through the normal code. For an
interrupted draft answer, it presents the current prompt instead of guessing
whether to repeat that answer. Read it and append the next intended answer to
the input list. An interrupted draft `CREATE` can therefore require a new,
explicit `CREATE` after resume; an uncertain vendor invocation remains subject
to the normal no-blind-retry rule.

Sessions last one hour without renewal. The lifecycle revokes only that
rehearsal's monitoring at expiry and schedules the normal owned-destination
disable. This occurs when the lifecycle next runs, not at a guaranteed exact
second. `--action stop` requests the same revocation early. Neither action deletes
the channel. Keep it for inspection, then delete the rehearsal channel through
the normal authorized Discord path to exercise the real teardown. Confirm the
owned vendor destination is absent with an exact GET and a complete list before
claiming cleanup. A new rehearsal is refused while a prior one retains resources.
The local step and notice journal remains available after source teardown.

The bridge creates no second SQLite writer process. All database work is short,
synchronous work on the service connection, using its existing WAL mode and
5-second busy timeout; no transaction spans an awaited network call. Session
locks serialize terminal retries and source locks coordinate provisioning with
normal cleanup. Shutdown cancels in-flight answers, retaining their uncertain
step and existing provisioning journal for explicit recovery.

This exercises the production conversation logic and downstream pipeline. It
does not impersonate or test Discord's human DM transport, command authorization
or the delivery of a success notice to an actual person's DM. Those require a
separate human-originated acceptance check.

## Execution and recovery boundaries

- An alert room has one responsible agent/account. Admission binds guild,
  channel, webhook, source ID, registration revision and nonce. Unsubscribed rooms
  remain reserved even when the feature is disabled; retain the registration
  database when disabling it. Mentions and reactions cannot enter a normal
  coding session in these rooms. Human messages return status in Phase 1.
  Diagnosis text is bounded to 1,600 characters and delivered directly in one
  Discord message with its receipt marker in an embed footer, or a spoiler
  when the channel lacks Embed Links permission. This path does not call the reply
  shortener or retain an expandable remainder; output above the bound is
  truncated. All reactions, including expansion reactions on older or unrelated
  messages, are intentionally ignored in reserved rooms in Phase 1.
- Webhook identity and a nonce do not authenticate PostHog against someone
  holding the webhook credential. Treat every incident as untrusted input.
  Trusted code fetches only the selected issue from the registered API project.
- The diagnostic call uses the responsible agent's configured provider/model,
  a fresh temporary directory and no ordinary session, identity files, memory,
  coding tools or MCP credential context. It receives a bounded issue and exception-frame
  projection and tracked source sample. Source files are read, not executed;
  configuration files and symlinks outside the repository are excluded. This
  is a diagnostic sample, not a complete source review or test run.
- The OpenAI-compatible provider rejects tools, session resume and tool history
  on a restricted call before contacting the endpoint. Both its native Ollama
  and compatible HTTP paths reject responses containing tool calls. Claude CLI
  uses an empty built-in tool list,
  strict empty MCP configuration, disabled hooks and session persistence.
  Codex CLI uses read-only mode, no approvals or extra directories, disabled
  configured MCP servers/plugins and a wildcard deny hook for all tools.
  Unsupported providers fail closed. CLI flags and hooks require validation
  against the installed CLI versions when enabling or upgrading them.
- SQLite owns setup intents, registrations, receipts, leases, budgets and
  result delivery. The first create intent can dispatch once. A later attempt
  adopts a unique matching remote object or reports an unknown outcome.
  Ambiguous matches, incomplete searches and conflicting objects are held.
  Test invocations, disable and removal operations require a completed destination
  record for the exact setup revision, then a fresh private GET matching the destination
  ID, template, filters, nonce-bearing content and owned webhook URL. A missing
  record or changed object blocks mutation. Already-disabled owned destinations
  require no repeat PATCH. These reads detect changes; they cannot lock an object
  against a concurrent vendor-side edit between the read and the mutation.
- Receipts deduplicate both Discord message IDs and source/event IDs. Expired
  six-minute leases recover after restart; stale workers cannot commit a
  result. After three interrupted attempts the receipt produces a held result.
  A lost result-send acknowledgement is reconciled against the bot's own
  marked messages before posting again. Reconciliation searches the latest
  100 messages and fails closed if an uncertain send cannot be found.
- Twelve diagnoses per source per hour are allowed. Later receipts remain
  queued and visible, instead of falling into the normal bot-chain suppression.
  A queue at 1,000 outstanding receipts pauses intake and reports overflow.
  Events arriving while paused are not queued; review the vendor for that gap.
  The worker edits one status message through queued, waiting for stack trace, investigating, progress and
  the final diagnosis or held result. A rate-limit notice appears only after
  actual deferral. API request budgets are shared across sources on the same host.

## Channel deletion and durable cleanup

The channel owns whether the subscription exists. The registered agent remains
responsible for diagnosis; `sources.owner` still identifies that agent. Deleting
the channel revokes the subscription, including deletion by another person with
Manage Channels permission. An explicit unsubscribe keeps the channel reserved
so an old webhook cannot fall through to an ordinary coding session.

On each bot account's startup, reconciliation runs before its diagnosis worker
can claim receipts or send results. It runs again on channel deletion/update
and every 60 seconds while enabled. A completed channel-creation journal entry
is recovered even if a crash preceded copying its ID into the source row.
Gateway events trigger a fresh check; they are not the sole source of truth.
Restart reconciliation does not depend on a resumable gateway session replay.

Only an exact channel request returning HTTP 404 with Discord code 10003
(Unknown Channel), with guild access and the bot's membership freshly confirmed
before and after that response, establishes deletion. HTTP 403, lost guild
access, another 404 code, timeouts and server failures retain the registration.
A whole-guild access failure reports once per bot account and guild, rather than
once per channel. A later successful check clears that episode's notice guard.
Timeouts, connection failures, HTTP 429 and server errors get one retry after
one second before notifying. Each read attempt has a 15-second timeout. Permission
denials and missing resources are not retried in that check. Gateway server-loss
events also verify current access before notifying. Notices explain the reason;
exception types and stack locations go to the engine log without exception values.
These retries do not relax the specific channel-404 and fresh membership checks.

Absence from a guild channel list proves nothing: Discord omits channels the
client cannot view from HTTP listings. A gateway channel with
`CHANNEL_OBFUSCATED` (`1 << 17`) also preserves the registration; its ID, not its
name, identifies it. A connector-local ConnectionState subclass retains only
channel IDs and this flag from gateway create/update/guild-create payloads,
because discord.py 2.7 does not expose it on TextChannel. The ordinary event
parser still runs. Tests exercise that real parser; repeat this integration
check when upgrading discord.py. The code handles this flag regardless of when an
installation enables channel obfuscation. See Discord's
[channel visibility contract](https://docs.discord.com/developers/resources/channel#channel-object-channel-flags)
and [API error codes](https://docs.discord.com/developers/topics/opcodes-and-status-codes#json-json-error-codes).

After confirmed deletion, SQLite records teardown and revokes intake in one
transaction. Queued and leased receipts are cancelled, undelivered results are
cleared, and active diagnosis/result tasks are cancelled. The worker can still
serve other sources. Cleanup waits for those tasks and for the source's setup
lock before touching the vendor. A create already in flight must finish its
journal entry; setup then refuses subsequent webhook, destination or test
creation if revocation occurred while awaiting a response.

Cleanup covers every journaled destination revision, including prior rotations.
A completed creation record requires a fresh exact ownership check. An uncertain
creation is reconciled using the existing name plus full ownership fields and
recorded only when one exact object is found. Cleanup never issues a create or
test invocation. No match after an uncertain dispatch, multiple matches, missing
historical ownership context or a changed destination retain the recovery record
and require review. A channel creation with an unknown result and no recoverable
ID is reported and retained; absence cannot be invented from a missing listing.

Channel deletion soft-deletes each owned vendor destination with PATCH
`enabled: false, deleted: true`. A separate exact GET must return HTTP 404, and
the ID must also be absent from a complete paginated destination listing before
cleanup is confirmed. `deleted` is write-only; a PATCH response cannot confirm
removal. This is PostHog soft deletion, not a claim of physical data erasure.
Unsubscribe still uses PATCH `enabled: false` and an ownership GET confirming
the saved disabled state; its destination remains present.

Failures retry durably after 30 seconds, doubling up to one hour; after eight
failed attempts cleanup is held for operator review. Restart keeps attempt
counts and deadlines. `/alerts resume source_id:<id>` restarts cleanup without
re-enabling intake. Lost removal acknowledgements are reconciled using exact
GET 404 plus complete-list absence, without repeating a confirmed removal.
Each revision records disable and removal separately, including database
upgrades. Deletion after or during unsubscribe still removes every destination.

Successful deletion cleanup removes the subscription, its receipt/operation
records and its per-revision webhook vault references. The shared service key is
retained. The channel's webhooks were deleted by Discord with the channel; no
Discord webhook mutation is needed. Unsubscribe retains both the source row and
its room reservation. Cleanup failures retain ownership evidence and credentials
needed for a safe retry.

Completion and failure notices have their own durable outbox, surviving source
purge. They go to the responsible agent's resolved Discord home through the
registered bot account, never to the removed channel or another alert room. A
missing home or denied send retains the notice for retry. A lost send response
is reconciled against the latest 100 own marked messages; an unconfirmed send is
not repeated blindly. Configure a stable `routing.discord.home_channel` so access
changes to an alert room cannot hide its cleanup status. These lifecycle notices
are fixed engine messages and do not create a model turn.

When the feature is disabled, room reservations remain enforced but no adapter
cleanup runs. The next enabled startup performs reconciliation. Retain the
registration database and vault until cleanup completes.

## Acceptance before enabling a real source

1. Inspect the proposed channel, project, repository, event selection and key
   scopes. Provisioning must be explicitly confirmed by the human in setup.
2. Exercise the installed diagnostic provider with hostile synthetic evidence.
   Prove shell, edits, network tools and external MCP execution cannot run;
   check that no normal session resumes and no provisioning key is inherited.
3. With authorized credentials, create one test destination and send with
   asynchronous-function mocking disabled. Capture only projected IDs/status;
   never print destination inputs, bytecode, HTTP bodies or webhook URLs.
4. Read back the exact Discord author/webhook and test identifiers. Require a
   completed diagnosis and its delivered receipt before calling setup active.
5. Exercise a real selected lifecycle event, duplicate delivery and a restart
   during processing. Verify visible recovery, bounded calls, unsubscribe and
   old-webhook rejection. Clean up test resources only after inspecting them.
6. On an authorized temporary source, delete its channel while running and while
   stopped. On restart, confirm intake stops, queued/running work and unsent
   results are cancelled, every owned destination returns exact GET 404 and is
   absent from a complete listing, the source row is purged, and a completion
   notice appears in the responsible agent's home.
7. Remove view access without deleting the channel, and separately remove guild
   access. Confirm registration/destination preservation and one guild-level
   notice. Exercise an obfuscated gateway object and an omitted listing entry.
8. Interrupt provisioning during a destination create and fail a cleanup request.
   Confirm journal reconciliation creates no duplicate, cleanup survives restart,
   failures remain visible at home, and manual cleanup resume does not reactivate
   intake. Confirm unsubscribe retains its reservation while deletion removes it.

Offline tests use fake vendor responses, real SQLite, a private test Unix
socket and instrumented CLI doubles. They establish local behavior, not vendor
write scopes, real Discord permissions, successful delivery or actual CLI
enforcement. Those remain installation acceptance requirements.

Adapter contract and verified source references: [PostHog extra](../extras/posthog/README.md).

## Presentation compatibility

New registrations store `message_format: 2` before provisioning. Their webhook
shows an issue title, lifecycle event and PostHog link, followed by a spoiler
containing the machine envelope. Registrations without that setting keep the
exact v1 template for all ownership checks and subsequent cleanup; no existing
vendor object is rewritten by this upgrade. New format markers are not accepted
for old revisions, and old markers are not accepted for new ones.

Accepted webhook messages are deleted with the owned webhook token only after
receipt and deduplication state commit. A duplicate envelope must match the stored
source, revision, event, issue and lifecycle kind before deletion. Invalid or
foreign messages, inactive intake and queue overflow are not deleted. A failed
delete is logged without exception values and leaves the raw message in place;
no Manage Messages permission is needed. There is no retroactive history sweep.

New webhooks are named "PostHog alerts". Creation recovery still requires the
legacy unique name or a durably saved webhook ID; a friendly name alone cannot
establish ownership. When reconciliation confirms a channel is present, the
current webhook is renamed once per process only after its creation journal,
vault reference, bot owner, channel, server and current revision agree. A custom
or changed name is retained. This does not change the PostHog destination inputs,
webhook URL, legacy template comparison or cleanup ownership checks.

Each incident has one bot message with one embed, edited from queued through
waiting, investigation and result. Real-incident cards are red; declared drill
and setup cards are grey; held results are amber. The linked title uses the first
issue summary's app and description, falling back to its name. The title is
sanitized, bounded to 80 characters and retained across waits and restarts. Before
that metadata arrives, the linked title shows the app name.

A regular result has a one-line verdict and bounded Cause, Fix and Missing
evidence fields. Drill and setup results show only a verdict and the tracked
source file mapped from an in-app frame, or a truthful no-source explanation.
The restricted diagnosis and full stored result remain part of the acceptance
path; compact presentation does not bypass diagnosis or its success requirement.
The footer shows service, lifecycle kind and a short issue ID, without a protocol
marker. Rooms lacking Embed Links receive a bounded plain-text version of the
same content. No permission change is made.

Acknowledged sends are recovered by their persisted message ID and bot author.
New sends also use Discord's invisible, receipt-specific nonce (22 characters).
After a lost acknowledgement, a matching nonce in channel history can recover the
message; old status/start/result markers remain recognized for upgrade recovery.
Discord guarantees the nonce on Message Create, but it is optional on later reads.
If neither the persisted ID nor a matching nonce/legacy marker establishes the
message, the send remains uncertain and no duplicate is posted. No visible marker
or separate marker embed is emitted by new incident cards.

Text is redacted before trimming at readable boundaries, with an ellipsis. The
budgets count UTF-16 units too and stay within Discord embed and message limits.

The deterministic delivery test is labelled as a setup test. New PostHog
created/reopened notifications carry an explicit drill bit derived only from
`event.properties.test == true`; names containing "test" are not evidence.
Spiking and manual transitions can lack trigger-specific exception properties.
A second fixed `test=true` filtered query can classify the recent sample, including
for legacy destinations, only when its exception UUID matches the unfiltered
sample. The event IDs are compared internally and excluded from model input.
A sample with no filtered match is unmarked, not proven to be a production fault.
Empty windows or mismatched identities remain unknown. Both requests use the
same explicit seven-day UTC window. The sampled event UUID is also compared to
the bound envelope's event ID without adding an include group. Exact equality
confirms the triggering event's sampled classification; different or missing IDs
retain the sample-only caveat. Only that relation reaches the model, never the
UUID. This does not classify every event in the issue.
The diagnostic prompt distinguishes a marked drill from a production fault and
asks for verification of the reporting path. It never authorizes execution,
fixes, resolving an issue or changing the debug route.

## Selecting diagnostic source evidence

Exception frames take precedence over issue-title keywords. The adapter reads
one recent event through the narrowly allowed sample endpoint documented in the
[PostHog extra](../extras/posthog/README.md#exception-evidence). Source selection
matches frame path stems against the tracked file inventory, including beyond
the first 300 files. Known build prefixes such as `.output/server/chunks/routes/`,
`dist/` and `build/` are stripped for matching; generated `.mjs`/`.js` stems may
match a tracked TypeScript file. For example, `api/debug/boom.get.mjs` can select
`server/api/debug/boom.get.ts`.

A path with multiple matches is not guessed. Parent traversal, untracked files,
hidden/dependency paths, symlinks and escapes from the registered root cannot
supply snippets. At most four files and 5,000 characters per file are included.
Compiled line numbers are explicitly not presented as source-map resolutions.
When in-app frames exist but none resolves, no source snippets or unrelated file
inventory are passed to the model. The evidence says "No in-app frame maps to a
tracked file." and retains the unresolved frames. The bounded keyword fallback
remains only when no in-app frames were supplied. A recent sample is treated as
the triggering event only when its UUID exactly matches the bound event ID.


Lifecycle notifications can precede exception-query indexing. Before invoking
its diagnostic model, the worker waits for a nonempty exception sample for up to
90 seconds from its first read attempt. Confirmed empty reads schedule retries
after 5, 10, 15, 20 and 30 seconds, capped at that original deadline. A scheduled
receipt releases its lease and the worker; there is no per-source lock or task
sleep across the delay. The same status message says "Waiting for stack trace."

The deadline, next scheduled time, empty-read count and projected evidence are
stored in SQLite. Restarting between polls preserves the wait. A process killed
during a read or model call still follows the existing six-minute interrupted
lease recovery and three-interruption limit; neither restart nor lease recovery
resets the evidence deadline. Once obtained, the bounded projected sample is
checkpointed for model retries, without raw event/person/session identifiers or
captured variables. This uses two idempotent receipt-column migrations; existing
registrations, receipt IDs, state and destination ownership are preserved.

Scheduled evidence reads reuse their receipt's diagnosis reservation because no
model has run yet. Genuine interrupted attempts still consume another diagnosis
reservation. Every vendor request retains the shared host API budget. No filtered
drill query is sent for an empty sample; a nonempty sample receives the same fixed
filtered query and identity comparison as before. A returned exception without
application frames can proceed with an explicitly limited source fallback.

At the deadline, a previously observed empty response permits a limited keyword
diagnosis that plainly says the stack trace was not yet available. An empty read
cannot distinguish indexing delay from no matching events in the checked window.
An issue-summary HTTP 404 instead holds diagnosis with "could not find this issue".
Other read errors and timeouts before any sample response are held, not reported
as empty evidence. Network reads are bounded by the remaining wait time; service
load, Discord delivery, interrupted leases and model execution can add time to
the overall alert. Human-facing drill wording avoids implementation field names.

## Fresh source revisions and automatic fix PRs

Diagnosis fetches the registered GitHub origin's advertised default branch into
an independent bare cache under the alert data directory. It reads Git tree
objects at the pinned fetched revision, including files absent from the shared
checkout. A single reported release commit is preferred only when verified in
that fetched history. Fetch errors hold diagnosis instead of silently using an
old checkout. The shared clone's HEAD, index, working files and refs stay alone.
Origin URLs containing credentials or pointing outside GitHub are refused.

After diagnosis delivery is committed, a separate worker can ask the owning
agent for a fix. `auto_fix_pr: true` starts this automatically. Existing
registrations migrate to `false`, and setup requires an explicit yes/no choice. Created/reopened incidents qualify only when the triggering
sample is positively identified, has no declared-drill match, and resolved a
tracked source file. Setup checks, drills, unknown trigger classification,
missing source and held diagnoses get a short `No PR` explanation on the same
card. An explicit Fix it click permits source searching when no tracked frame
resolved; the other eligibility and publication gates still apply. Unmarked means no declared marker was found; it cannot prove intent.

The owning agent uses its configured model in a fresh session without native
provider tools. Its structured JSON actions can read source, write source and a
new regression test, request validation, or finish. The controller mediates all
paths. It supplies structured diagnosis sections and bounded evidence as data,
not a raw vendor payload or an ordinary agent conversation. The agent cannot
select shell commands, remotes, endpoints, credentials or publishing options.

Diagnosis first tries a fresh origin tree. For local/non-GitHub origins or a
fetch/authentication failure, it instead reads the local clone's committed HEAD
through Git objects, leaving the working tree untouched. The model context and
card footer explicitly say the source may be stale. Fix preparation still
requires a successful fresh fetch; stale evidence never supplies the fix base.

Repairs use new private clones under `application-alerts/fixes/`, on
`alert-fix/<issue-short>-<scope>` branches from a newly fetched default revision.
The first runner supports npm/pnpm repositories with Vitest, a `typecheck`
script and `test:unit` (or `test`). It also runs `lint` and `format:check` when
present. Nuxt projects receive an offline `nuxt prepare` in both private trees.
A new regression must produce an assertion failure on the original tree, pass
on the candidate, and then all gates must pass. Infrastructure failures do not
count as reproductions. The committed blobs are checked against the tested
hashes before publication, including after restart.

Gates run through macOS Seatbelt (`sandbox-exec`). Source, existing tests, Git
metadata and dependency files are read-only to test processes. Only generated
contents beneath pre-created build directories, private temporary directories
and tool caches are writable. Their root entries cannot be renamed or replaced.
The model cannot read or write artifact or dependency paths. Controller source
reads, writes and baseline regression copies walk directory descriptors with
`O_NOFOLLOW` on each component and refuse symlinks or multiply linked files.
Artifact directory identities are rechecked before controller access.
Network, external project/home reads, external writes, keychain access and
signals to processes outside the sandbox are denied. Provider transport and
controller-owned Git/GitHub operations remain outside that test sandbox.
Each gate has a fresh inherited sandbox permission identity. A trusted launcher
waits for the controller to verify that identity before executing repository code.
Cleanup enumerates matching same-user processes, freezes them, rescans for forks,
and kills them, including detached and reparented children. Controller access
resumes only after no matching process remains and artifact roots are unchanged.
A failed process census or cleanup quarantines the workspace and opens no PR.
This uses macOS sandbox/process APIs; inability to verify them fails closed.
Metadata access is scoped to the allowed trees and their ancestors.
Unsupported operating systems fail closed with `No PR`; they do not use an
unrestricted shell fallback.

Dependencies are copied privately from the registered clone's installed cache,
only when its committed manifests and lockfiles match the fetched base. No
installation or package-registry access occurs. Missing or mismatched caches,
dependency changes, workflow/configuration edits and modifications of existing
tests are refused. This initial runner does not automatically repair dependency
bugs or repositories requiring other test runners. Those cases receive a reason
for manual follow-up. Private dependency copies are not links into the shared
clone. Repository hooks do not run in the privileged controller; the discovered
repository gates run inside the sandbox instead.

The controller alone pushes the exact validated commit, without force, to the
registered origin. It opens a ready-for-review PR with the issue link, cause,
fix, regression and gate summary. It never merges, enables auto-merge, deploys,
or changes a PostHog issue. The card progresses through `Writing fix…` to
`PR #n ready` or `No PR: <reason>`. An existing issue PR is linked with its actual
state rather than claimed as a newly tested fix. The committed Discord message
ID is mandatory; the fix stage never creates a replacement message.

SQLite stores a project/repository/issue deduplication key, source revision,
lease, test proof, immutable commit, publication intents and card outbox.
No transaction is held over model, Git, gate or network work. One process owns
the existing store. Jobs have a 40-minute lease and a 35-minute execution ceiling;
a killed process resumes after the lease expires, at most three times. Completed
validation is reused only for its exact immutable commit. A lost PR response
is reconciled using paginated GitHub PR reads. If an earlier create intent has
no confirmed PR, it is held for review rather than posted again blindly.
Failed attempts retain candidate work locally for review. Repeated notifications
do not retry automatically. An owner click can request a new attempt, charged
to the same rolling limit. Durable per-issue cycles keep prior validation and
publication evidence; retries never discard uncertain publication intents.

`alerts.fix_runs_per_day` sets the per-registration rolling 24-hour cap (default
3, maximum 20). A registration-specific `fix_runs_per_day` takes precedence.
The cap counts first starts, including owner-requested retries, not receipt
arrival times or restart continuations.
Disabling, deleting or revising the source invalidates the fix lease before any
new publication operation. Cancelling that job does not stop the shared fix worker.
Gate cancellation waits for sandbox cleanup before ending the job. Service
shutdown retains the durable running lease for restart recovery. Requests already dispatched can still finish; their
saved intent remains available for review. Retained, committed non-drill cards gain manual controls on upgrade, including
older receipts without the new fix context. They are not automatically repaired
on upgrade. Older sampled evidence is checked for trigger identity; explicit
clicks can search the freshly fetched repository when prior frame mapping failed.

Residual risk: hostile exception text or repository content can still steer a
model toward a bad code change or a misleading regression. Isolation restricts
effects and the before/after gates establish an executable check, not semantic
correctness. Generated code, tests, private dependency caches and build tooling
remain review inputs. Human PR review and existing branch protection remain
necessary. A tested PR is the endpoint; resolving on merge is a separate feature.


### Manual Fix it and later settings

With `auto_fix_pr: false`, non-drill incident cards carry a persistent **Fix it**
button. The custom ID binds registration and issue, and the handler additionally
checks the exact saved message ID, bot, server, channel and registration revision.
Only the setup user's Discord identity can click; other users, including other
administrators and bots, receive an ephemeral refusal. No normal agent turn or
chat text can authorize the run. Setup checks and declared drills have no button.

The button is disabled until diagnosis delivery is committed and while the issue
has a pending/running fix. Restarts restore the view against the saved message
ID; the durable card outbox restores its enabled/disabled state. Concurrent
clicks share one issue job. If a PR exists, a further click returns its link. If
no PR was opened, the button is enabled again so the owner can retry. A retry of
an uncertain publication only reconciles the saved intent, without a blind new
POST. If no frame resolved, the explicit click allows the restricted fixer to
search literal text in tracked source and docs itself; it still cannot read
hidden files, dependencies or paths outside the private checkout.

Use `/alerts settings auto_fix_pr:true source_id:<app-name-or-channel-id>` to
enable automatic fixes, or pass `false` for manual buttons. The command is
restricted to the same setup owner, requires one active registration and updates
only this preference. It does not recreate resources, rotate a webhook, change
the source revision or alter destination ownership. Enabling it can start
eligible retained incidents that were awaiting a click. Turning it off returns
unstarted automatic jobs to manual mode; a running or explicitly requested repair
continues. The operator/rehearsal script uses the same yes/no setup question and
records that answer in its normal transcript.
