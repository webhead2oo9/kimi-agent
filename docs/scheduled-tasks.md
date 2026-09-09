# Scheduled tasks

Kimi can run an approved procedure once or repeatedly without an incoming message.
Each task owns an automatically generated instruction skill, a schedule, saved notes,
and run history. Task skills are loaded before execution, remain available through
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
  scheduled_fallbacks: []
```

Replace `primary-chat` with a configured model entry name. The primary and any
fallbacks must declare `text` and `tool_calling`; their required credentials are
checked at startup. Restart the bot after editing model configuration.

This setting applies to scheduled execution in every enabled server. Task setup
and follow-up conversations still use chat routing, and compaction still uses
the compaction role. There is no per-task model field. A configured scheduled
model is independent of `/models` selection and chat scope overrides. Failures
use only `scheduled_fallbacks` under the ordinary provider failover rules; they
do not silently switch to the chat model.

Leaving `scheduled` unset preserves existing behavior: scheduled runs use chat
routing, including the active `/models` selection and scope overrides.

## Create and edit tasks

Ask Kimi naturally: “Every Friday at 9am, summarize development changes in
#announcements, but post only if something changed.” The `task_manage` setup action
loads a conversational wizard and remembers setup in the conversation. Kimi asks
for missing sources, timing, destinations, conditions, and output requirements.
Use `cancel_setup` to stop an unfinished wizard.

Kimi writes the dedicated skill while preparing a draft. The preview includes the
interpreted schedule and attached complete settings and `SKILL.md`. Click **Approve** to activate that exact revision, or **Reject** to reject it. Changed or already-used previews cannot be
confirmed. A draft without valid skill instructions cannot activate.

Owners can inspect, edit, pause, resume, delete, and run their tasks immediately.
Staff can manage tasks in their server, but execution always uses the owner's
current authority. Edits require a new confirmation and apply to subsequent runs;
an existing run keeps its approved revision. Changing the sources or condition
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
scheduled-task model, the exact pending revision, and a copy of saved state (or an
empty baseline when the draft requests a reset). It reads real sources and returns
a private result: what it would publish, why it would stay silent, or what input or
unsupported action prevents completion. Sample posts and their destination IDs are
included in a downloadable preview. A silent first check reports that it would
establish a baseline without posting.

Tests cannot publish messages or change the schedule, approval, saved task state,
or run/delivery history. Discord posts are captured in memory. Only Discord
history/search, member/channel discovery, and internet search are allowed for
reading; browser actions, workspace tools, generated files, and other tools are
unavailable. Tool restrictions apply at dispatch, including tools registered while
a test is running. The test stops if the draft is decided or replaced, or access
is revoked. A test is not proof that a later run or an unsupported action will succeed.

Tests incur normal model/search usage and have a two-minute limit, with at most two
concurrent tests and one per task. They do not consume a scheduled occurrence;
approval remains a separate action. Preview working conversations are owner-scoped
and covered by the existing privacy deletion controls.

## Checks, memory, and notifications

Each occurrence starts fresh with the skill and up to 64,000 characters of saved
task state. The skill explains what to remember: a release identifier, message
cursor, previous findings, or the last reporting window. It can use Kimi's normal
available tools. Live-message thread handoff and starting a detached coding task
are unavailable during scheduled runs.

The agent must finish through `task_complete`:

| Outcome | Behavior |
| --- | --- |
| `completed` | Publish queued results and save the successful state. |
| `no_change` | Save the observation and record history without a destination post. |
| `needs_input` | Ask in the setup channel and pause future runs. |
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
daily, weekday, weekly, or monthly local calendar times. Store an explicit IANA
timezone. Intervals stay anchored to their starting time. Calendar schedules skip
nonexistent daylight-saving times and unavailable month dates; repeated local times
run once, at their first occurrence.

The wizard saves `catch_up` or `skip` for missed occurrences. Catch-up runs once,
coalescing the backlog; skip records occurrences more than 60 seconds late without
executing them. There are two scheduled workers, with one outstanding execution
per task. Normal turn budgets and workspace locks also apply.

SQLite revisions, occurrence claims, and a renewable process lease prevent duplicate
claims. Restart recovery does not replay interrupted agent actions. Generated
publication is saved separately, including attachment bytes (up to 25 MiB total
per run), so known delivery failures can be retried using `retry_delivery` without
calling the model again. Starting a newer run or changing saved state makes older
failed deliveries ineligible for retry. Already-sent chunks retain their message IDs. An uncertain
send requires inspecting the destination before deliberately starting a new run;
it cannot be automatically retried. The reported-change baseline advances only
after all destination messages succeed.

`get_channel_context` supports explicit channels, timestamp windows, ascending or
descending order, and continuation cursors. Keep its returned `window_end` as
`before` while paging. Each page contains at most 100 messages and a bounded text
payload; follow `next_cursor` to continue. `discord_text_search` supports filter-only
queries and date bounds, and reports pagination/indexing limits. History is fetched
live rather than kept in a separate Discord archive.

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
Kimi's ordinary reply only points to the pending approval and does not repeat the
skill or settings.

Previews show a readable schedule and the task's IANA timezone. Upcoming dates and
countdowns use Discord timestamps, displayed in each viewer's local timezone;
this does not change the task schedule. You may choose a different timezone per task.
Full settings and instructions remain attached as `task.json` and `SKILL.md`.

Only the person requesting that revision can test, approve, or reject it, including
when another staff member views its buttons. Approval replaces the proposal buttons
with **Manage**. The active card refreshes its status and next run; replaced approved
revisions point to management of the current task. Rejecting an edit keeps the previously approved
version running. Replaced pending previews are marked superseded.

Approved proposal threads remain unlocked for management. After rejection, Kimi
attempts to lock and archive the approval thread where the requester and bot have
the necessary authority. Missing permissions leave it open without undoing the
decision. Threads used by an approved task for publication, logging, or follow-up
questions also remain open when rejecting an edit.
Message edits retry after transient failures,
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
