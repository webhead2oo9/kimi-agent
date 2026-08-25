# Durable coding agent

The optional coding agent handles repository-scale, multi-file, and
investigate-edit-verify work without holding the Discord response loop open.
The ordinary assistant remains the generalist: it can use `run_code` for a
small calculation or script, or call `start_coding_task` when the work should
continue independently.

This feature is off by default. It registers only when all three conditions are
true:

- `CODING_TASKS_ENABLED=true`;
- `roles.coding` names a text-and-tool-calling model in `config/models.yaml`;
- code execution is enabled and its complete Linux sandbox profile passes the
  startup probe.

There is no chat-model fallback. If the dedicated role or sandbox is absent,
the coding controls are not exposed.

## Lifecycle and status

`start_coding_task` durably queues the objective, acceptance criteria, Discord
root, user, and workspace, then returns a task id immediately. The task remains
held until the Discord boundary has resolved the reply's final channel or
thread, delivered the acknowledgement there, attempted the initial status, and
released the worker. Workers are bounded globally and enforce one writer per
workspace; excess work waits FIFO, subject to the per-user and per-workspace
queue limits.

After a successful queue commit, the foreground ReAct turn ends
deterministically. The bot posts a short task-id acknowledgement without making
another chat-model call, then creates the editable status message before making
the worker claimable; progress and completion come from the durable task
delivery path. A `move_to_thread` call later in the same tool batch may still
supply that routing request; every other later call is recorded as skipped so
the provider transcript remains well-formed without performing more foreground
work. A committed task acknowledgement wins a turn deadline that expires
immediately afterward. Queue rejection and cancellation remain ordinary tool
errors so the chat model can explain them.

When a reply is explicitly or automatically moved into a thread, the status,
acknowledgement, progress, and final report all use that thread. If Discord
cannot create it, all four fall back to the original channel instead.

The worker uses a least-privilege registry: bounded workspace read/write tools,
`coding_plan`, `coding_progress`, and managed command-job controls. Its command
prompt requires `coding_plan` before edits or jobs. Plan and progress tool calls
are the source of user-visible milestones; no separate summarizer polls or
interprets the agent's hidden reasoning. Discord receives one status message
that is edited at the configured minimum interval, followed by one normal final
reply. Final delivery is durable and retried after a transient Discord failure.
Retries use persisted exponential backoff, stop after ten attempts or 24 hours,
and fail immediately for a missing channel or lost permission. An authorized
caller can use `coding_task_retry_delivery` to reset an exhausted delivery.

The status, message, cancel, and delivery-retry tools let the generalist inspect
or steer a task and recover a final report after the Discord target is restored.
Steering is appended to the task journal and enters the model at the next ReAct
boundary, so it cannot interrupt a provider call or command in the middle.
Text-only follow-up replies remain deliverable while the coding worker owns its
workspace. Follow-ups that use workspace tools or deliver local attachments
still serialize behind that writer lease so concurrent file access stays safe.

## Managed command jobs

The coding agent cannot submit arbitrary inline commands directly. It first
writes a script into the user's workspace, then starts that path through
`coding_job_start`. The job runs through the same systemd, Bubblewrap, seccomp,
rlimit, quota, network-mode, and workspace-lock boundary as `run_code`, with
coding-specific wall and CPU ceilings. With no explicit `wait_seconds`,
`coding_job_status` performs one event-driven wait up to the configured job
lifetime, so a long build does not consume repeated model iterations. Passing
`wait_seconds=0` performs a non-blocking status read.

The application owns every job handle. Cancellation awaits sandbox teardown,
and systemd also receives `RuntimeMaxSec` as a manager-side backstop. A process
restart marks an uncertain running job `interrupted`; the recovered agent is
told to inspect the workspace and never blindly replay it.

## Recovery and cancellation

After every completed tool batch the worker stores its conversation checkpoint,
provider state, current plan, and event cursor. Startup requeues interrupted
workers while tasks paused for requested input remain paused. A steering message
atomically resumes a paused task; an unanswered pause expires at the original
total deadline. Only one worker may resume a workspace at a time.

Members can use `/stop`, or send a bot-directed message containing exactly
`stop`, `cancel`, or `abort` (case-insensitive). That lane runs before normal
turn admission and cancels both the foreground response and coding work in the
current root. `/stop scope:all` covers all of that member's active work;
`task_id` targets one owned coding task. Partial workspace changes are preserved
so they can be inspected or resumed later. `/privacy` cancellation uses the
same teardown path before deletion and the privacy barrier prevents recovery
from racing a pending deletion.

## Failure boundaries

Provider calls have their own timeout inside the task's total deadline. A model
failure, exhausted iteration budget, task deadline, command failure, or explicit
cancel produces a terminal durable state instead of wedging the Discord turn.
Task usage is attributed to its own `coding:<task-id>` turn. Output text passes
through the configured output moderation policy; background attachments are
withheld when file-content moderation cannot be completed safely.

Operators should watch the status message, SQLite task/job rows, and normal
application logs. The raw checkpoint and command output are operational state,
not a user-facing transcript.
