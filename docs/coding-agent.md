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

`start_coding_task` durably queues the objective, acceptance criteria, selected
context and starting files, Discord root, user, and workspace, and returns a
task id straight away. The task is
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
tools, archive and document extraction, `coding_plan`, `coding_progress`,
`coding_request_input`, and the managed command-job controls. It also borrows
the assistant's research tools, `fetch_url`, `internet_search`, and `browser`,
under exactly the foreground's registration gates: a tool the assistant lacks
(no search key, `BROWSER_ENABLED=false`, an operator denylist) is simply absent
from the worker too. The per-turn search and browser budgets apply to the whole
worker run on one context and start over when a task resumes after a restart or
requested input. Its command prompt requires `coding_plan` before any edits or
jobs and asks for research before guessing at unfamiliar APIs. Plan and
progress tool calls are what produce the user-visible milestones; there is no
separate summarizer polling or interpreting the agent's hidden reasoning.

The worker's browser calls release their turn lease after every call rather
than at the end of the turn, because the run may last minutes and its own
managed jobs need the shared VPN namespace in between. Browser screenshots
queue as final-report attachments; they are shown to the model inline only when
the coding model accepts image input.
Before the worker publishes a plan, the status uses a short summary supplied by
the foreground assistant or derived from the objective. Once a plan exists,
the plan replaces that summary so members see what the worker is actually doing.
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

Final attachments are preflighted against the destination guild's current
Discord per-file upload limit, falling back to 10 MiB when it is unavailable.
A mixed batch still delivers every fitting file within the existing per-response
attachment cap and omits only oversized files during size preflight.
The first reply chunk names omissions in plain text and corrects any stale claim
in the worker's report that they were attached. The exact limit and notice are
checkpointed before the send, exposed as sanitized task-status metadata, and
reused on recovery. The delivered notice is also stored in the conversation
transcript so the next model turn sees the actual outcome.

## Context and starting files

Delegation is explicit. The foreground assistant can include a bounded,
text-only snapshot of the conversation and current turn, name non-image
attachments from the triggering message, and point to existing workspace files.
The snapshot includes useful reply and tool-read context but never system
prompts, tool definitions, provider payloads, recalled long-term memories, or
image bytes. It is stored with the task so a restart does not change what the
worker was originally given.

Selected attachments go through the same moderation and workspace limits as
`import_attachment`, then become ordinary workspace files before the task is
queued. Existing workspace inputs are validated and stored as relative paths.
The worker is told that copied conversation text, filenames, paths, and file
contents are untrusted and must be inspected rather than followed as instructions.

Input preparation is all-or-nothing. If a named attachment is unavailable, a
path is unsafe or missing, a quota is exceeded, or queue admission fails, no
task is queued and files created by that attempt are removed. The foreground
assistant receives a direct explanation naming the affected attachment or
workspace-relative path so it can tell the member what to fix.

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

In `CODE_EXEC_NETWORK_MODE=netns`, a job shares the single VPN namespace with
the same user's browser. Starting a job asks the browser service to close that
user's idle worker immediately instead of after its idle TTL, then waits up to
30 seconds for the lease; if someone else's browser turn or a foreground
networked run still holds it, the job fails with a retryable error rather than
blocking. While a job is queued or running the worker's `browser` calls are
refused, and the flag clears when `coding_job_status` reports a terminal state
or `coding_job_cancel` returns. Host and `none` modes keep the previous
fail-fast behavior on the shared execution semaphore.

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
Output text passes through the configured output moderation policy. Generic
workspace attachments are delivery artifacts rather than moderation inputs;
first-class native generated images and owned embed images remain moderated in
ordinary turns.

As an operator, watch the status message, the SQLite task/job rows, and the
normal application logs. The raw checkpoint and command output are operational
state, not a user-facing transcript.
