# Scheduled tasks

Kimi can run an approved procedure once or repeatedly without an incoming message.
Each task owns an automatically generated instruction skill, a schedule, saved notes,
and run history. Task skills are loaded before execution, remain available through
compaction, and stay separate from the normal shared and personal skill catalogs.

The built-in [scheduled-tasks skill](../bot/skills/builtin/scheduled-tasks/SKILL.md)
is a reusable management playbook, available through `skill_list` and
`load_skill` with `name: "scheduled-tasks"`. It covers guided setup, writing each
task's own procedure, complete source checks, conditional output, and recovery.

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

## Create and edit tasks

Ask Kimi naturally: “Every Friday at 9am, summarize development changes in
#announcements, but post only if something changed.” The `task_manage` setup action
loads a conversational wizard and remembers setup in the conversation. Kimi asks
for missing sources, timing, destinations, conditions, and output requirements.
Use `cancel_setup` to stop an unfinished wizard.

Kimi writes the dedicated skill while preparing a draft. The preview includes the
interpreted schedule and attached complete settings and `SKILL.md`. Click **Approve** to activate that exact revision, or **Deny** to reject it. Changed or already-used previews cannot be
confirmed. A draft without valid skill instructions cannot activate.

Owners can inspect, edit, pause, resume, delete, and run their tasks immediately.
Staff can manage tasks in their server, but execution always uses the owner's
current authority. Edits require a new confirmation and apply to subsequent runs;
an existing run keeps its approved revision. Changing the sources or condition
resets saved comparison state, as shown in the preview.

Ask Kimi to copy a task's skill to personal skills with a chosen kebab-case name.
Only the creator can export it; the personal copy and task skill then change
independently. `/tasks` also provides list, inspect, history, pause, resume,
run-now, retry-delivery, and delete actions without requiring a model response.

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
calling the model again. Already-sent chunks retain their message IDs. An uncertain
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

Only the person requesting that revision can approve or deny it, including when
another staff member views its buttons. Either decision removes both buttons and
edits the preview into a receipt. Denying an edit keeps the previously approved
version running. Replaced pending previews are marked superseded.

After a decision, Kimi attempts to lock and archive the approval thread where the
requester and bot have the necessary authority. Missing permissions leave the thread
open without undoing the decision. Threads used by an approved task for publication,
logging, or follow-up questions also remain open, including when denying an edit.
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
