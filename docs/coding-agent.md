# Durable coding agent

The optional coding agent takes on repository-scale, multi-file, and
investigate-edit-verify work without holding the Discord response loop open
while it runs. The ordinary assistant stays the generalist: it can still reach
for `run_code` when a small calculation or script will do, and it calls
`start_coding_task` when the work is big enough that it should carry on
independently.

The feature is off by default, and it registers only when all three of these
hold:

- `CODING_TASKS_ENABLED=true`;
- `roles.coding` names a text-and-tool-calling model in `config/models.yaml`;
- code execution is enabled and its complete Linux sandbox profile passes the
  startup probe.

There is no fallback to the chat model. If the dedicated role or the sandbox is
missing, the coding controls simply aren't exposed.

## Lifecycle and status

`start_coding_task` durably queues the objective, acceptance criteria, Discord
root, user, and workspace, and returns a task id straight away. The task is
then held until the Discord boundary has worked out the reply's final channel
or thread, delivered the acknowledgement there, attempted the initial status
message, and released the worker. Workers are bounded globally and there is
only ever one writer per workspace, so any excess work waits in FIFO order,
subject to the per-user and per-workspace queue limits.

Once the queue commit succeeds, the foreground ReAct turn ends
deterministically. The bot posts a short task-id acknowledgement without making
another chat-model call, then creates the editable status message before the
worker becomes claimable; from there, progress and completion arrive through
the durable task delivery path. A `move_to_thread` call later in the same tool
batch can still supply the routing request, but every other later call is
recorded as skipped, which keeps the provider transcript well-formed without
doing any more foreground work. If the turn deadline expires right after a
committed task acknowledgement, the acknowledgement wins. Queue rejection and
cancellation stay ordinary tool errors, so the chat model can explain them to
the user.

When a reply is moved into a thread, whether explicitly or automatically, the
status, acknowledgement, progress, and final report all go to that thread. If
Discord can't create it, all four fall back to the original channel.

The worker runs on a least-privilege registry: bounded workspace read/write
tools, `coding_plan`, `coding_progress`, and the managed command-job controls.
Its command prompt requires `coding_plan` before any edits or jobs. Plan and
progress tool calls are what produce the user-visible milestones; there is no
separate summarizer polling or interpreting the agent's hidden reasoning.
Discord gets one status message, edited no more often than the configured
minimum interval, followed by one normal final reply. Final delivery is durable
and retried after a transient Discord failure: retries use persisted
exponential backoff, stop after ten attempts or 24 hours, and fail immediately
if the channel is missing or permission has been lost. An authorized caller can
use `coding_task_retry_delivery` to reset an exhausted delivery.

The status, message, cancel, and delivery-retry tools let the generalist inspect
or steer a task, and recover a final report once the Discord target is
restored. Steering is appended to the task journal and reaches the model at the
next ReAct boundary, so it can't interrupt a provider call or a command
midway. Text-only follow-up replies can still be delivered while the coding
worker owns its workspace, but follow-ups that use workspace tools or deliver
local attachments serialize behind that writer lease so concurrent file access
stays safe.

## Managed command jobs

The coding agent can't submit arbitrary inline commands directly. It first
writes a script into the user's workspace, then starts that path through
`coding_job_start`. The job runs through the same systemd, Bubblewrap, seccomp,
rlimit, quota, network-mode, and workspace-lock boundary as `run_code`, with
coding-specific wall and CPU ceilings. That includes the code-execution
workspace-accounting policy: preflight and final scans, five-second in-flight
polling by default, and bounded retries for transient disappearing-entry races.
Without an explicit `wait_seconds`, `coding_job_status` does one event-driven
wait for up to the configured job lifetime, so a long build doesn't burn
repeated model iterations; passing `wait_seconds=0` gives a non-blocking status
read instead.

The application owns every job handle. Cancellation waits for the sandbox to
tear down, and systemd also gets `RuntimeMaxSec` as a manager-side backstop. A
process restart marks any running job whose fate is uncertain as
`interrupted`, and the recovered agent is told to inspect the workspace rather
than blindly replay it.

## Recovery and cancellation

After every completed tool batch, the worker stores its conversation
checkpoint, provider state, current plan, and event cursor. On startup,
interrupted workers are requeued, while tasks paused for requested input stay
paused. A steering message atomically resumes a paused task; an unanswered
pause expires at the original total deadline. Only one worker may resume a
given workspace at a time.

Members can use `/stop`, or send a bot-directed message containing exactly
`stop`, `cancel`, or `abort` (case-insensitive). That lane runs before normal
turn admission and cancels both the foreground response and any coding work in
the current root. `/stop scope:all` covers all of that member's active work,
and `task_id` targets one owned coding task. Partial workspace changes are
preserved so they can be inspected or resumed later. `/privacy` cancellation
goes through the same teardown path before deletion, and the privacy barrier
keeps recovery from racing a pending deletion.

## Failure boundaries

Provider calls have their own timeout inside the task's total deadline. A model
failure, an exhausted iteration budget, a task deadline, a command failure, or
an explicit cancel all produce a terminal durable state rather than wedging the
Discord turn. Task usage is attributed to its own `coding:<task-id>` turn.
Output text passes through the configured output moderation policy, and
background attachments are withheld when file-content moderation can't be
completed safely.

As an operator, watch the status message, the SQLite task/job rows, and the
normal application logs. The raw checkpoint and command output are operational
state, not a user-facing transcript.
