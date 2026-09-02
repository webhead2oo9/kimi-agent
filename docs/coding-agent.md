# Durable coding agent

The optional coding agent takes on repository-sized work: many files, investigate-edit-verify loops, long builds and test runs. It does that in the background, so a Discord reply is not held open for an hour. The ordinary assistant stays the generalist. It still reaches for `run_code` when a small calculation or script will do, and calls `start_coding_task` when the work is big enough to carry on independently.

The feature is off by default. It only registers when all three of these are true:

- `CODING_TASKS_ENABLED=true`
- `roles.coding` points to a text-and-tool-calling model in `config/models.yaml`
- Code execution is enabled and its full Linux sandbox profile passes the startup probe

There is no fallback to the chat model. If the dedicated role or the sandbox is missing, the coding tools are simply not offered to the model, and nothing else changes.

## What you get

A coding task runs in the background with its own ReAct loop. It can:

- Read and write many files across the workspace
- Run long builds and tests through managed command jobs
- Use research tools (`internet_search`, `fetch_url`, `browser`) under the same gates as the foreground assistant
- Maintain a visible plan that members can watch
- Accept additional input when the operator or model asks for it
- Survive bot restarts and resume where it left off

In return, you accept longer latency, separate model spend, and the need to monitor a background worker. The foreground assistant is still the right choice for quick questions, small edits, or anything that should finish inside one Discord turn.

## Requirements and activation

You need three things before the tools appear:

1. Enable the feature in your environment.
2. Configure a dedicated `coding` role in `models.yaml` that supports text and tool calling. The coding model never inherits from `chat`.
3. Have a working code execution sandbox (`CODE_EXEC_ENABLED=true` and a passing probe).

If any of these are missing, `start_coding_task` and the related controls stay hidden. The ordinary `run_code` tool continues to work normally.

## How a task starts

The foreground assistant calls `start_coding_task` with the objective, acceptance criteria, selected context, and starting files. The call returns a task id immediately. The task is durably queued, the foreground turn ends cleanly, and the bot posts a short acknowledgement with the task id.

The worker does not start until that acknowledgement and the first status message have actually been posted to Discord. At most `CODING_TASK_MAX_CONCURRENCY` workers run at once (2 by default), and only one task can write to a given workspace at a time, so extra tasks wait their turn in the order they arrived. Per-workspace and per-user queue caps, task and job time limits, and the other knobs are listed under [Durable coding tasks](configuration.md#durable-coding-tasks) in the configuration guide.

Once the queue succeeds, the foreground assistant is done. Progress and completion arrive later through the durable delivery path in `app/coding_delivery.py`.

## Status messages and visibility

Discord shows one editable status message. Before a plan exists, the status shows a short summary supplied by the foreground assistant or derived from the objective. Once the worker publishes a plan, that plan replaces the summary so members see what the worker is actually doing.

The status updates no more often than `CODING_STATUS_MIN_INTERVAL_SECONDS` (10 s by default). When the task finishes, one normal final reply is delivered. Both the status and the final reply are durable: if Discord is unreachable they retry with increasing delays, give up after ten attempts or 24 hours, and stop early if the channel is gone or the bot has lost permission to post there.

When a reply is moved into a thread (explicitly or automatically), the status, acknowledgement, progress, and final report all follow. If Discord cannot create the thread, everything falls back to the original channel.

## What the worker can do

The worker gets a smaller tool set than the foreground assistant:

- The bounded workspace read/write, archive, and document tools
- `coding_plan`, `coding_progress`, `coding_request_input`, and the managed job controls
- The assistant's research tools (`internet_search`, `fetch_url`, `browser`) when the foreground has them registered

If the assistant lacks a tool because of credentials, settings, or policy, the worker lacks it too. Search and browser call budgets cover the whole worker run rather than a single turn, and they reset when a task resumes after a restart or after requested input arrives.

The worker's prompt requires it to publish a plan with `coding_plan` before editing files or starting jobs, and tells it to research unfamiliar APIs instead of guessing. Its plan and progress calls are what members see as milestones; there is no separate hidden summarizer.

## Managed command jobs

The coding agent cannot run an arbitrary inline command. It first writes a script into the workspace, then starts that file with `coding_job_start`. The job runs inside exactly the same boundary as `run_code` (systemd cgroup, Bubblewrap, seccomp, rlimits, quotas, network mode, and the workspace lock), with its own longer wall-clock and CPU ceilings (`CODING_JOB_MAX_SECONDS`, `CODING_JOB_MAX_CPU_SECONDS`).

In `CODE_EXEC_NETWORK_MODE=netns`, a job needs the single VPN namespace, which the same user's browser may be holding. Starting a job asks the browser service to close that user's idle browser worker, then waits up to 30 seconds for the namespace to free up. If someone else's browser turn or a foreground networked `run_code` still holds it after that, the job fails with an error the worker can retry later. While a job is queued or running, the worker's own `browser` calls are refused so the two cannot deadlock.

The bot process owns every job. Cancelling waits for the sandbox to tear down fully. If the bot restarts while a job is running, that job is marked `interrupted`, and the recovered worker is told to look at what the workspace contains now rather than replaying the job blindly.

## Input and context

The worker only sees what the foreground assistant hands over. That can be a size-limited, text-only snapshot of the conversation, non-image attachments from the triggering message, and existing workspace files named by path. The snapshot never includes system prompts, tool definitions, provider payloads, long-term memories, or image bytes.

Selected attachments go through the same moderation and workspace limits as `import_attachment`. Existing workspace inputs are validated and stored as relative paths. The worker is told that copied conversation text, filenames, paths, and file contents are untrusted and must be inspected.

Preparing the inputs is all-or-nothing. If a named attachment is unavailable, a path is unsafe, a quota is exceeded, or the queue is full, no task is queued and any files created by that attempt are removed. The foreground assistant gets a plain explanation so it can tell the member what to fix.

## Recovery, steering, and cancellation

After every completed batch of tool calls, the worker saves a checkpoint: its conversation so far, provider state, current plan, and position in the event log. On startup, interrupted workers are put back in the queue and pick up from their last checkpoint. Only one worker may resume a given workspace at a time.

If a task's owner is blocked (see [`/moderation`](tools.md)) while the task is still queued, the scheduler finishes it as cancelled instead of running it. The stored message is neutral and does not mention the block, because it is posted to the channel the task was started in; the block itself is only logged. A task that is already running is not interrupted by a block and ends on its own.

Tasks paused for requested input stay paused until a steering message resumes them. An unanswered pause expires at the task's original total deadline.

Members can cancel with `/stop`, or by sending the bot a message that says exactly `stop`, `cancel`, or `abort` (any capitalisation). That check runs before the normal "should the bot reply?" gates, and it cancels both the foreground response and any coding work in the current conversation. `/stop scope:all` cancels all of that member's active work everywhere. Partial workspace changes are kept so they can be inspected or picked up later.

`/privacy` → **Delete my data** uses the same teardown before deleting anything, and holds a per-user barrier so a task cannot be recovered while its owner's deletion is in progress.

## Failure boundaries and monitoring

Each model call has its own timeout (`CODING_PROVIDER_CALL_TIMEOUT_SECONDS`) inside the task's total deadline (`CODING_TASK_MAX_SECONDS`). A model failure, running out of iterations (`CODING_TASK_MAX_ITERATIONS`), hitting the deadline, a failed command, or an explicit cancel all leave the task in a final recorded state; none of them can wedge a Discord turn. Token usage for a task is recorded under its own `coding:<task-id>` turn, so `/usage` shows it separately from chat.

The worker's output text goes through the same output moderation as chat replies. Workspace files it attaches are not moderated (they are delivered as-is), while generated images in ordinary turns keep their usual moderation.

As an operator, watch the status message, the coding task and job tables in SQLite, and the normal application logs. The raw checkpoint and command output are working state for recovery, not a transcript meant for users.

## Practical notes

- Coding tasks use separate model spend from ordinary chat turns.
- A long-running task can hold the workspace lock for minutes. Members who need to work in the same workspace will wait.
- The foreground assistant can still answer questions and make small edits while a coding task runs, as long as it does not need the workspace lock.
- If you expect frequent large jobs, pick a `coding` model with a generous context window and reliable tool calling; it matters more here than raw speed.
- Because delivery retries, a final report can arrive hours later if Discord was unreachable. After ten attempts or 24 hours it is dropped.

When the work is small enough to finish inside one turn, keep using the ordinary assistant and `run_code`. When the work needs sustained focus, many files, or long builds, hand it to the coding agent.
