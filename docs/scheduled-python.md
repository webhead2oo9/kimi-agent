# Scheduled Python

Scheduled tasks can use Python to decide whether to invoke the task model, or to
produce text and files without a generation-model call. Kimi writes the script
during normal setup. The requester reviews `task.py`, the settings, and `SKILL.md`,
then approves that exact revision.

## Enable and operate

Enable [scheduled tasks](scheduled-tasks.md#enable-a-server) in the server and
configure a working [code execution sandbox](code-exec.md). `CODE_EXEC_ENABLED`
must be enabled, `run_code` must pass its startup probe, and the owner must meet
both the scheduled-task and code-execution access requirements. The offline
sandbox profile is also probed when normal code uses `host` or `netns`. A failed
offline probe disables scheduled Python and logs the reason; ordinary LLM tasks
remain available. Setup reports `python_available`.

There is no new environment variable or database migration. Existing definitions
default to `execution: "llm"`. Change modes through an approved task edit:

| Execution | Behavior |
| --- | --- |
| `llm` | The existing scheduled model runs the procedure. |
| `python_gate` | Python stays silent, publishes, asks for input, or requests the LLM. |
| `python_only` | Python determines the result. Requesting the LLM fails the run. |

Python always runs **offline**, including when normal `run_code` uses `host` or
`netns`. The application fetches declared inputs before launch. Public HTTPS inputs
use the application-side `fetch_url` download path; they do not use the code
sandbox's network namespace or consume its weekly network-run allowance. They
require access to `fetch_url`. Discord inputs require `get_channel_context` and
current owner/bot access, including deployment channel exclusions. Tool denylists
and tier restrictions apply to proposals and execution.

Runs reuse code execution's concurrency, CPU, memory, process, workspace, output,
and wall-time limits. Input acquisition has a separate 60-second ceiling. There
are still two scheduled workers and one outstanding execution per task. Previews
retain their two-minute ceiling, two-test concurrency limit, and one test per task.

Python uses a temporary owner-scoped job directory containing inputs and outputs;
ordinary workspace files, bot credentials, and the database are not mounted.
Configured read-only sandbox mounts remain available. The owner's existing
workspace `.venv` takes precedence over `CODE_EXEC_VENV_DIR`; the standard library
is used when neither exists. The workspace `.venv` is mounted read-only and
protected from concurrent workspace changes during the script.

Kimi can install missing packages using `run_code` during setup, subject to its
existing network policy and limits. Occurrences and previews do not install
packages. A missing or broken dependency fails the check and requires attention.
Package changes or expiry can affect later runs; approval does not pin versions.

## Inputs

`definition.python` contains `code` and up to 50 named `inputs`. Names start with a
lowercase letter, use lowercase letters/digits/underscores, and have at most 64
characters. Names are unique within a revision.

```json
{
  "inputs": [
    {
      "name": "discussion",
      "kind": "discord",
      "channel_id": "800000000000000001",
      "window": "since_success",
      "lookback_seconds": 86400
    },
    {
      "name": "release",
      "kind": "https",
      "url": "https://example.org/releases.json"
    }
  ]
}
```

Replace the example channel and URL with actual sources. HTTPS inputs are fixed,
unauthenticated GET requests: no custom headers, cookies, credential fields,
dynamic URL templates, or browser execution. The existing download policy rejects
private addresses, embedded credentials, unsafe DNS results, and unsafe redirects.
Bodies are provided unchanged as files; Python parses JSON, feeds, CSV, or other data.

Discord `window` defaults to `since_success`. The first read looks back
`lookback_seconds` (default 86,400); later reads start at the last committed window
end. `rolling` instead uses the selected lookback on every occurrence. Lookback
must be from 1 to 31,536,000 seconds. Reads follow pagination in a fixed half-open
window, including the start and excluding the end. Message objects include IDs,
author information, text, timestamps, message URLs, and attachment names;
attachment bytes are not fetched.

All inputs together are limited to 10 MiB and 100 Discord history pages, with up
to 100 messages per page. Existing download, redirect, file, and workspace limits
can be stricter. Failing any input or exhausting a limit fails the check. Partial
history is never silently presented as complete.

## Script contract

Read `/work/input.json`. It contains `task_id`, `revision`, `run_id`, `now` (UTC
with an offset), `initialized`, `state`, and an `inputs` mapping. Each input entry
has a relative `path` under `inputs/`. Discord entries also give `channel_id`,
`window_start`, `window_end`, and `count`; their files contain JSON arrays of
messages. HTTPS entries give `url`, `content_type`, and `size_bytes`.

Write one JSON object to `/work/result.json`:

| Field | Meaning |
| --- | --- |
| `outcome` | Required: `no_change`, `completed`, `invoke_llm`, or `needs_input`. |
| `state` | Required JSON object replacing saved user state; at most 64,000 characters. |
| `detail` | Required explanation, at most 4,000 characters; a nonempty question for `needs_input`. |
| `content` | Optional text/Markdown, at most 60,000 characters; only for `completed`. |
| `files` | Optional list of `{ "path": "outputs/file.csv", "description": "..." }`; only for `completed`. |
| `llm_context` | Optional observations, at most 64,000 characters; only for `invoke_llm`. |

`completed` requires text or files. State keys beginning with `_task_` are reserved
and must not be written by Python. Host input cursors are kept separately from
user state and cannot be overridden by the script or LLM handoff. The state limit
also applies after adding application cursors. JSON must contain finite values
and no duplicate keys. Source is limited to 100,000 UTF-8 bytes and the result file
to 1 MiB. Stdout/stderr are diagnostics, bounded by `CODE_EXEC_MAX_OUTPUT_BYTES`;
stdout is not a publication or result channel.

Files must be regular files beneath `outputs/`, with distinct filenames and no
symlinks or traversal. There is a ten-file, 25 MiB total ceiling, further limited
by `WORKSPACE_TOOL_MAX_ATTACHMENTS` (default five) and destination upload limits.
Files and text go to all approved default destinations using configured mentions;
Python cannot choose new destinations or recipients.

For `invoke_llm`, candidate state and context become untrusted observations under
the approved skill. The host uses the configured scheduled model, or ordinary
scheduled chat routing when no scheduled role is set. The LLM must finish through
`task_complete`. Requesting a handoff does not commit intermediate state.

## Examples

A gate for a public JSON endpoint returning a release `id`, `name`, and `url`:

```python
import json
from pathlib import Path

context = json.loads(Path("input.json").read_text())
release = json.loads(Path(context["inputs"]["release"]["path"]).read_text())
release_id = str(release["id"])
changed = release_id != context["state"].get("release_id")
result = {
    "outcome": "invoke_llm" if changed else "no_change",
    "state": {"release_id": release_id},
    "detail": "New release found" if changed else "Release unchanged",
}
if changed:
    result["llm_context"] = json.dumps(
        {"id": release_id, "name": release["name"], "url": release["url"]}
    )
Path("result.json").write_text(json.dumps(result))
```

Choose `python_gate` and write a skill explaining how the LLM should summarize the
release. For a conditional task's first silent check, the host records the Python
baseline without invoking the LLM or posting.

A Python-only CSV report counting messages by author from `discussion`:

```python
import csv
import json
from collections import Counter
from pathlib import Path

context = json.loads(Path("input.json").read_text())
messages = json.loads(Path(context["inputs"]["discussion"]["path"]).read_text())
counts = Counter(message["author_id"] for message in messages)
with Path("outputs/counts.csv").open("w", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["author_id", "messages"])
    writer.writerows(sorted(counts.items()))
Path("result.json").write_text(json.dumps({
    "outcome": "completed",
    "state": {"last_count": len(messages)},
    "detail": "Message counts computed",
    "content": f"{len(messages)} messages from {len(counts)} authors.",
    "files": [{"path": "outputs/counts.csv", "description": "Message counts by author"}],
}))
```

## Preview and recovery

**Test preview** runs the exact pending script on real inputs and copied state.
Direct Python output includes private sample files. A gate requesting the LLM
follows the model preview path; that LLM retains the restricted preview tool set
and cannot generate files. Tests leave task state, approvals, schedules, and
run/delivery history unchanged. Configured moderation still applies. Python-only
and skipped checks incur no generation or compaction calls; handoffs incur normal
model usage.

History identifies Python-only completion or Python-to-LLM handoff and Python
duration. Exceptions, timeouts, invalid results, denied inputs, and missing packages
put the task into its existing attention state. Inspect the failure, repair the
environment or approve a revised script, then resume/run it. Failure is not “no
change” and does not trigger automatic LLM fallback.

Successful `no_change` checks commit state and input cursors immediately. Published
results commit them after all destination messages succeed. `needs_input` asks
its question and pauses without committing state. Attachment bytes are saved before
temporary jobs are removed. **Retry delivery** resends those bytes without rerunning
Python or the LLM; existing uncertain-send and stale-state restrictions apply.

Code, input declarations, execution mode, source, or condition edits reset state,
as shown in approval. Schedule-only edits preserve it. Temporary jobs are cleaned
after runs/tests; expiry handles abandoned files and full privacy deletion includes
them. Existing task retention rules apply to code, state, runs, and saved output.
