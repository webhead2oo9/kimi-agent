# Durable coding agent

The optional coding agent handles repository-scale, multi-file, and investigate-edit-verify work without holding the Discord response loop open. The ordinary assistant stays the generalist: it can still reach for `run_code` when a small calculation or script will do, and it calls `start_coding_task` when the work is big enough that it should carry on independently.

The feature is off by default. It only registers when all three of these are true:

- `CODING_TASKS_ENABLED=true`
- `roles.coding` points to a text-and-tool-calling model in `config/models.yaml`
- Code execution is enabled and its full Linux sandbox profile passes the startup probe

There is no fallback to the chat model. If the dedicated role or the sandbox is missing, the coding controls simply are not exposed.

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

The worker does not start until the Discord boundary has delivered that acknowledgement and created the initial status message. Workers are globally bounded, and only one writer may hold a workspace at a time, so extra tasks wait in FIFO order.

Once the queue succeeds, the foreground assistant is done. Progress and completion arrive later through the durable delivery path.

## Status messages and visibility

Discord shows one editable status message. Before a plan exists, the status shows a short summary supplied by the foreground assistant or derived from the objective. Once the worker publishes a plan, that plan replaces the summary so members see what the worker is actually doing.

The status updates no more often than the configured minimum interval. When the task finishes, one normal final reply is delivered. Both the status and the final reply are durable: they retry with exponential backoff, stop after ten attempts or 24 hours, and fail fast if the channel is gone or permissions are lost.

When a reply is moved into a thread (explicitly or automatically), the status, acknowledgement, progress, and final report all follow. If Discord cannot create the thread, everything falls back to the original channel.

## What the worker can do

The worker runs on a least-privilege registry. It receives:

- The bounded workspace read/write, archive, and document tools
- `coding_plan`, `coding_progress`, `coding_request_input`, and the managed job controls
- The assistant's research tools (`internet_search`, `fetch_url`, `browser`) when the foreground has them registered

If the assistant lacks a tool because of credentials, settings, or policy, the worker lacks it too. Search and browser budgets cover the entire worker run and reset when a task resumes after a restart or requested input.

The command prompt requires `coding_plan` before edits or jobs and directs the worker to research unfamiliar APIs instead of guessing. Plan and progress calls produce the user-visible milestones. There is no separate hidden summarizer.

## Managed command jobs

The coding agent cannot submit arbitrary inline commands. It first writes a script into the workspace, then starts that path with `coding_job_start`. The job runs through the exact same systemd, Bubblewrap, seccomp, rlimit, quota, network mode, and workspace lock boundary as `run_code`, with coding-specific wall and CPU ceilings.

In `CODE_EXEC_NETWORK_MODE=netns`, a job shares the single VPN namespace with the same user's browser. Starting a job asks the browser service to close that user's idle worker immediately, then waits up to 30 seconds for the lease. If someone else's browser turn or a foreground networked run still holds it, the job fails with a retryable error. While a job is queued or running, the worker's own `browser` calls are refused.

The application owns every job handle. Cancellation waits for the sandbox to tear down. A process restart marks any running job whose fate is uncertain as `interrupted`, and the recovered worker is told to inspect the workspace rather than blindly replay it.

## Input and context

Delegation is explicit. The foreground assistant can include a bounded, text-only snapshot of the conversation, name non-image attachments from the triggering message, and point to existing workspace files. The snapshot never includes system prompts, tool definitions, provider payloads, long-term memories, or image bytes.

Selected attachments go through the same moderation and workspace limits as `import_attachment`. Existing workspace inputs are validated and stored as relative paths. The worker is told that copied conversation text, filenames, paths, and file contents are untrusted and must be inspected.

Input preparation is all-or-nothing. If a named attachment is unavailable, a path is unsafe, a quota is exceeded, or queue admission fails, no task is queued and any files created by that attempt are removed. The foreground assistant receives a direct explanation so it can tell the member what to fix.

## Recovery, steering, and cancellation

After every completed tool batch, the worker stores its conversation checkpoint, provider state, current plan, and event cursor. On startup, interrupted workers are requeued. When the scheduler claims a queued or requeued task whose owner has since been blocked, it finishes the task as cancelled instead of running it; a task already running is not interrupted by a block and ends on its own. Tasks paused for requested input stay paused until a steering message resumes them. An unanswered pause expires at the original total deadline. Only one worker may resume a given workspace at a time.

Members can cancel with `/stop`, or by sending a bot-directed message containing exactly `stop`, `cancel`, or `abort` (case-insensitive). That lane runs before normal turn admission and cancels both the foreground response and any coding work in the current root. `/stop scope:all` covers all of that member's active work. Partial workspace changes are preserved so they can be inspected or resumed later.

`/privacy` cancellation goes through the same teardown path before deletion, and the privacy barrier keeps recovery from racing a pending deletion.

## Failure boundaries and monitoring

Provider calls have their own timeout inside the task's total deadline. A model failure, exhausted iteration budget, task deadline, command failure, or explicit cancel all produce a terminal durable state rather than wedging the Discord turn. Task usage is attributed to its own `coding:<task-id>` turn.

Output text passes through the configured output moderation policy. Generic workspace attachments are delivery artifacts rather than moderation inputs. First-class generated images remain moderated in ordinary turns.

As an operator, watch the status message, the SQLite task and job rows, and the normal application logs. The raw checkpoint and command output are operational state, not a user-facing transcript.

## Practical notes

- Coding tasks use separate model spend from ordinary chat turns.
- A long-running task can hold the workspace lock for minutes. Members who need to work in the same workspace will wait.
- The foreground assistant can still answer questions and make small edits while a coding task runs, as long as it does not need the workspace lock.
- If you expect frequent large jobs, consider a dedicated `coding` model with a generous context window and tool-calling reliability.
- The durable delivery path means final reports can arrive hours later if Discord was unreachable. The retry budget is finite.

When the work is small enough to finish inside one turn, keep using the ordinary assistant and `run_code`. When the work needs sustained focus, many files, or long builds, hand it to the coding agent.