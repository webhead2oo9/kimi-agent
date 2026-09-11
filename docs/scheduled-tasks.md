# Scheduled tasks

Kimi can run an approved procedure once or repeatedly without an incoming message.
Each task owns an automatically generated instruction skill, a schedule, saved notes,
and run history. [Scheduled Python](scheduled-python.md) can check conditions
before invoking an LLM or publish deterministic text and files. During LLM runs,
task skills are loaded before execution, remain available through
compaction, and stay separate from the normal shared and personal skill catalogs.

The built-in [scheduled-tasks skill](../bot/skills/builtin/scheduled-tasks/SKILL.md)
is a reusable management playbook, available through `skill_list` and
`load_skill` with `name: "scheduled-tasks"`. It covers guided setup, writing each
task's own procedure, complete source checks, conditional output, and recovery.

`discord_channels` returns up to 200 readable sources per page (configurable with
`limit`, from 1 to 200), listing regular channels and active threads before
archived threads. Pass the returned `next_cursor` as `cursor` to continue.
Omit `cursor`, pass an empty string, or use `"0"` to start at the first page.
A full page returns immediately: `has_more: true` means more sources may remain,
so the final continuation can be empty.
Configured, accessible posting destinations appear on every page. A
`sources_error` means source discovery failed; destinations can still be used,
but the empty source list does not establish that no sources are available.
Cursors are offsets into the current accessible inventory, not snapshots:
restart discovery if channels, thread archive status, or permissions change.
Archive traversal can revisit earlier entries when requesting subsequent pages.

## Enable a server

Add a `scheduled_tasks` mapping to the server's configuration fragment:

```yaml
---
bot_active: true
scheduled_tasks:
  enabled: true
  min_tier: staff
  timezone: Europe/Berlin
  destinations: [800000000000000001, 800000000000000002]
---
```

Tasks default to disabled, the creator tier defaults to `staff`, and destinations
default to empty. `min_tier` accepts `member`, `regular`, or `staff`. Destinations
are numeric IDs of text/announcement channels or existing threads; listing a parent
allows its accessible threads. Archived/locked threads cannot receive posts. The
same destination list enables the independent `discord_post` tool, including when
scheduled execution is disabled.

Both the task owner and bot must be able to access and post to a destination.
Deployment channel boundaries and the destination's `discord_post` tool denylist
still apply. Cross-server delivery, DMs, and creating forum posts are unsupported.

## Runner model

Operators can choose a dedicated model for all scheduled runs in
`config/models.yaml`, using the existing `models` catalog just like the coding
agent:

```yaml
roles:
  # Keep the existing chat, compaction, and other role assignments.
  scheduled: primary-chat
  scheduled_fallbacks: [fallback-chat]
```

Replace `primary-chat` and `fallback-chat` with configured model entry names.
The primary and any fallbacks must declare `text` and `tool_calling`; their
required credentials are checked at startup. Restart the bot after editing
model configuration.

Scheduled runs use the shared [provider failover rules](providers.md#failover):
provider availability failures can move to backups in the listed order, while
invalid requests do not trigger a model switch. An omitted or empty
`scheduled_fallbacks: []` list means there is no backup model; applicable retries
still run against the primary.

This setting applies to scheduled execution in every enabled server. Task setup
and follow-up conversations still use chat routing, and compaction still uses
the compaction role. There is no per-task model field. A configured scheduled
model is independent of `/models` selection and chat scope overrides. Failures
use only `scheduled_fallbacks`; neither `chat_fallbacks` nor `coding_fallbacks`
is inherited.

Leaving `scheduled` unset preserves existing behavior: scheduled runs use chat
routing, including the active `/models` selection, scope overrides, and
`chat_fallbacks`. In that mode, `scheduled_fallbacks` is not used.

## Create and edit tasks

Ask Kimi naturally: “Every Friday at 9am, summarize development changes in
#announcements, but post only if something changed.” The `task_manage` setup action
loads a conversational wizard and remembers setup in the conversation. Kimi asks
for missing sources, timing, destinations, conditions, and output requirements.
Use `cancel_setup` to stop an unfinished wizard.

Kimi writes the dedicated skill while preparing a draft, plus a Python script when
that execution mode is selected. The preview includes the execution mode,
interpreted schedule, complete settings, `SKILL.md`, and `task.py` when applicable.
Click **Approve** to activate that exact revision, or **Reject** to reject it. Changed or already-used previews cannot be
confirmed. A draft without valid skill instructions cannot activate. The optional
[dashboard](dashboard.md) offers the same Test preview, Approve, and Reject
controls for drafts shown in a private chat; they apply the same decision path and
authority checks as the Discord buttons.

Owners can inspect, edit, pause, resume, delete, and run their tasks immediately.
Staff can manage tasks in their server, but execution always uses the owner's
current authority. Edits require a new confirmation and apply to subsequent runs;
an existing run keeps its approved revision. Changing sources, condition, Python
code, input declarations, or execution mode
resets saved comparison state, as shown in the preview.

Ask Kimi to copy a task's skill to personal skills with a chosen kebab-case name.
Only the creator can export it; the personal copy and task skill then change
independently. `/tasks` opens a private, paginated list of tasks. Select one to see
its schedule, next run, latest outcome, and management buttons. Owners see their
tasks; staff can see tasks in their server. Existing action arguments still work.

The **Manage** button on an approved proposal opens the same private controls.
Pause/resume, run-now, history, and eligible delivery retries do not require a model
response. A task needing input offers **Answer & resume**; deletion asks for a
confirmation. History includes links to published messages and a downloadable
record of run and delivery details. Every action checks current permissions.

**Edit** posts a short prompt in the current channel. Reply directly to that
message with the desired changes: its conversation is bound to the selected task
and private persisted context is available only to the requester. The resulting
edit still requires a new proposal approval.

## Test a proposal

Before approval, the requester can click **Test preview**. The test uses the
pending revision's execution mode and a copy of saved state (or an
empty baseline when the draft requests a reset). It reads real sources and returns
a private result: what it would publish, why it would stay silent, or what input or
unsupported action prevents completion. Sample posts and their destination IDs are
included in a downloadable preview; direct Python output can include private sample
files. The scheduled-task model runs only for LLM tasks or a Python gate's handoff.
A silent first check reports that it would
establish a baseline without posting.

Tests cannot publish messages or change the schedule, approval, saved task state,
or run/delivery history. Discord posts are captured in memory. Only Discord
history/search, member/channel discovery, and internet search are allowed for
reading by the LLM; browser actions, workspace tools, LLM-generated files, and other
tools are unavailable. Python reads its predefined inputs and may create files in
its isolated temporary workspace. Tool restrictions apply at dispatch, including tools registered while
a test is running. The test stops if the draft is decided or replaced, or access
is revoked. A test is not proof that a later run or an unsupported action will succeed.

Tests incur model/search usage when those services run and have a two-minute limit, with at most two
concurrent tests and one per task. They do not consume a scheduled occurrence;
approval remains a separate action. Preview working conversations are owner-scoped
and covered by the existing privacy deletion controls.

## Checks, memory, and notifications

Each occurrence starts fresh with the skill and up to 64,000 characters of saved
task state. The skill explains what to remember: a release identifier, message
cursor, previous findings, or the last reporting window. It can use Kimi's normal
available tools. Live-message thread handoff and starting a detached coding task
are unavailable during scheduled runs.

The agent must finish through `task_complete`; Python supplies the equivalent
outcome in its [result file](scheduled-python.md#script-contract):

| Outcome | Behavior |
| --- | --- |
| `completed` | Publish queued results and save the successful state. |
| `no_change` | Save the observation and record history without a destination post. |
| `needs_input` | Ask in the setup channel and pause future runs. |
| `read_failed` | Application preflight/input reads exhausted temporary retries; recurring tasks keep their schedule, while one-time tasks require attention. |
| Failure/interruption | Record the problem and require attention before retrying actions. |

For conditional tasks, setup chooses a silent first baseline or an initial post;
silent is the default. The runtime suppresses queued publication on that first
silent check and on every `no_change` result. A failed check must never be reported
as “unchanged.” Use the resume/run-now `answer` argument to supply requested input.

Every occurrence has internal history. Each task can optionally select a configured
log channel for outcomes. Log messages never ping, and a log failure does not rerun
the task. Destination posts may notify explicitly selected users or roles, subject
to current Discord permissions. **`@here` and `@everyone` notifications are always
disabled; there is no override.**

## Timing and recovery

Schedules support a single timestamp, fixed intervals of at least 60 seconds, and
daily, weekday, weekly, or monthly local calendar times. New submissions require
an explicit IANA timezone. Setup reuses a timezone explicitly given in the current
conversation or an unchanged timezone when editing. If it is missing or ambiguous,
the assistant should ask before drafting; the server default is only a suggestion.
The application validates the timezone field but cannot verify that the assistant
asked the conversational question. Older stored definitions without a timezone
retain their original UTC interpretation.

Use `task_manage` with `action: "validate_schedule"` and a `schedule` object to see
the interpreted recurrence, timezone, and next three occurrences before drafting.
Each occurrence includes an application-calculated `native_time` string with both
Discord's full date (`F`) and relative time (`R`); setup, draft, and inspect return
the same interpretation. Reuse those strings when presenting concrete run times.
Intervals stay anchored to their starting time. Calendar schedules skip
nonexistent daylight-saving times and unavailable month dates; repeated local times
run once, at their first occurrence.

The wizard saves `catch_up` or `skip` for missed occurrences. Catch-up runs once,
coalescing the backlog; skip records occurrences more than 60 seconds late without
executing them. Separate deployment pools default to two LLM executions, two Python
executions, and two runs publishing concurrently. Configure them with
`SCHEDULED_TASK_LLM_MAX_CONCURRENCY`, `SCHEDULED_TASK_PYTHON_MAX_CONCURRENCY`, and
`SCHEDULED_TASK_DELIVERY_MAX_CONCURRENCY`. Shared provider/sandbox caps, normal turn
budgets, and workspace locks still apply.

Admission rotates across owners and selects their oldest eligible occurrence, with
one executing occurrence per owner and one outstanding occurrence per task.
A Python gate reserves bounded handoff capacity before starting, then releases its
Python slot while waiting for an LLM slot. The handoff queue is bounded by the
Python pool size; Python-only checks remain eligible when it is full. Waiting
gates keep their run record and do not rerun their script on admission.

Different runs publish concurrently, with ordered chunks within each run and state
committed only after every required destination succeeds. A delayed chunk blocks
later chunks of that run. Approval-card updates and publication run independently;
neither waits in the lease-renewal loop.

Application-owned preflight and declared input reads can retry temporary timeouts,
connection/DNS failures, and HTTP 408, 429, 500, 502, 503, or 504 responses before
Python or model execution begins. There are at most three attempts, with waits of
one and four seconds; a longer `Retry-After` is honored only if it fits the read
deadline. Declared inputs share one 60-second deadline across attempts and preserve
the same Discord history window. Permission denials and invalid inputs require
attention immediately. Reads performed by model tools are part of execution and
do not use this retry policy.

After temporary read retries are exhausted, history records `read_failed` without
changing saved state or input cursors. Recurring tasks retain their next occurrence;
one-time tasks require attention. The home channel receives one notice at the start
of an outage and one after a successful occurrence, including required publication.
Every failed occurrence still appears in history and the configured log channel.
Outage tracking survives restarts and clears when a replacement definition is
approved. Skipping a missed occurrence does not report recovery. Python/model
execution and uncertain publication are never replayed by this read retry policy.

Pausing stops future occurrences, interrupts active execution and preview tests,
and cancels pending publication. Already-sent messages remain in Discord. Resume
schedules the next future occurrence; it does not continue the interrupted run or
restore cancelled delivery chunks. Inspect history and destinations before using
**Run now** to deliberately start over. If a one-off time has passed, use **Run now**
or approve a new schedule; a one-off task requiring attention can also resume
immediately.

SQLite revisions, occurrence claims, and a renewable process lease prevent duplicate
claims. Restart recovery does not replay interrupted agent actions. Generated
publication is saved separately, including attachment bytes (up to 25 MiB total
per run), so known delivery failures can be retried using `retry_delivery` without
calling the model again. Starting a newer run or changing saved state makes older
failed deliveries ineligible for retry. Already-sent chunks retain their message IDs. An uncertain
send requires inspecting the destination before deliberately starting a new run;
it cannot be automatically retried. The reported-change baseline advances only
after all destination messages succeed.

`get_channel_context` supports an optional channel name, mention, or ID in the
task's server, timestamp windows, ascending or descending order, and continuation
cursors. Omit the channel to use the run's current channel. Search permissions
and exclusions apply; channel IDs from other servers are rejected. Duplicate
accessible names and archived threads require an exact ID. Keep its returned
`window_end` as `before` while paging. Each page contains at most 100 messages and a bounded text
payload; follow `next_cursor` to continue. `discord_text_search` supports filter-only
queries and date bounds, and reports pagination/indexing limits. History is fetched
live rather than kept in a separate Discord archive.

To read around a search result, pass its `channel_id` and use its `message_id`
as `around_message_id` in `get_channel_context`. The limit includes the selected
message and its neighbours. Use the returned `older` or `newer` arguments for further pages;
do not combine the initial around-message read with timestamps or a cursor.

## Stored data

The additive schema migration introduces task definitions, immutable skill revisions,
setup markers, saved state, runs, attachment bytes, delivery records, and a runner
lease in the existing database. Back up that database using the normal
[database procedure](database.md). Terminal run records and their attachments are
pruned after 30 days, except while delivery is pending. Definitions and current state
persist until deleted. Full `/privacy` deletion removes the owner's tasks, skills,
setup markers, runs, and saved outputs; separately exported personal skills retain
the existing [personal-skill lifecycle](personal-skills.md).

Published responses are mapped to their destination conversations. Those conversations
contain the published output and do not inherit the task's private working context.

## Approval messages

The wizard stays in the current conversation. A draft is delivered separately in a
quiet approval thread by default; a current thread is reused. New approval threads
do not automatically answer messages. Ask to keep approval in-channel to opt out.
If thread creation is unavailable, the preview appears in the current channel.
Kimi's ordinary reply uses a labeled **Review task** link to the pending approval and does not repeat the
skill or settings.

Previews show a readable schedule and the task's IANA timezone. Upcoming dates and
countdowns use Discord's full-date and relative timestamp pair, displayed in each viewer's local timezone;
this does not change the task schedule. Task lists, management details, and history
use the same pair for concrete instants; recurrence descriptions retain wall-clock
time and the IANA timezone. You may choose a different timezone per task. Downloaded
settings and history use ISO timestamps with explicit offsets, without Discord markup.
Full settings are attached as readable `task-details.md`, with the complete
instructions in `SKILL.md`.

Only the person requesting that revision can test, approve, or reject it, including
when another staff member views its buttons. Approval replaces the proposal buttons
with **Manage**. The active card refreshes its status and next run; replaced approved
revisions point to management of the current task. Archived approval receipts stay
fixed; `/tasks` always shows current status. Rejecting an edit keeps the previously approved
version running. Replaced pending previews are marked superseded.

After approval or rejection, Kimi posts a short sign-off and then locks and archives
the approval thread where the requester and bot have the necessary authority.
Use `/tasks` to reopen private management controls. Missing permissions leave the
thread open without undoing the decision. Threads used by an approved task for
publication, logging, or follow-up questions also remain open.
Successful sign-offs are recorded so retrying a failed closure does not repeat them.
Message edits and thread closure retry after transient failures,
including after a restart; approval itself is never repeated. Older previews remain
usable and are updated on interaction.

## Replies to scheduled output

Replies continue from the published message. While its task and run records remain,
Kimi receives an application-generated origin hint with the task's name at that
revision, task/run IDs, revision, and publication time. The hint remains available
through conversation compaction. It includes no private skill, comparison state,
or working transcript. Ordinary cross-channel posts do not receive this hint.
Replying does not run or modify the task; changes require an explicit request and
the normal task-management authorization. Deleted/pruned origin records simply
leave the public conversation without the hint.
