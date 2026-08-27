# Visual rendering

Kimi exposes two searchable member-tier tools, `render_chart` and
`render_diagram`, for creating a Discord-ready PNG in one model tool call. They
render structured chart data and constrained Mermaid diagrams respectively. The
model never supplies Python,
JavaScript, HTML, CSS, browser navigation, filesystem paths, or arbitrary
Matplotlib/Mermaid configuration.

Visual rendering ships with the [persistent browser](browser.md) capability.
There is no second feature flag: when `BROWSER_ENABLED=true`, the browser
runtime, selected browser network sandbox, and Mermaid assets pass their startup
checks, Kimi registers `browser` and both visual tools. An older runtime that
predates the Mermaid bundle can still register `browser`; boot logs a specific
warning and leaves the visual tools unavailable until the operator reruns
the installer.

## Tool contract

Both tools are hidden from the provider schema until `browse_tools` loads them.
Their schemas contain only fields relevant to that visual kind. One successful
call creates, verifies, and queues one 1200×675 PNG for the
final Discord reply. Its required `alt_text` becomes the Discord attachment
description rather than being discarded after rendering, and deployments with
output moderation enabled screen that text before delivery.

A bar chart (the default chart type):

```json
{
  "title": "Weekly signups",
  "x_label": "Week",
  "y_label": "Users",
  "alt_text": "Weekly signups rise from 12 in W1 to 31 in W3.",
  "categories": ["W1", "W2", "W3"],
  "series": [
    {
      "name": "Signups",
      "values": [12, 18, 31]
    }
  ]
}
```

Set `chart_type` to `line` for a categorical line chart. Scatter charts use
numeric points and do not accept `categories`:

```json
{
  "chart_type": "scatter",
  "title": "Response time and payload size",
  "x_label": "Payload (KiB)",
  "y_label": "Milliseconds",
  "alt_text": "Response time generally increases with payload size.",
  "series": [
    {
      "name": "Requests",
      "points": [
        {"x": 10, "y": 42},
        {"x": 20, "y": 55},
        {"x": 40, "y": 81}
      ]
    }
  ]
}
```

A Mermaid diagram:

```json
{
  "title": "Request flow",
  "alt_text": "A request is validated and then receives a response.",
  "source": "flowchart LR\n  A[Request] --> B[Validate]\n  B --> C[Respond]"
}
```

The result contains only safe metadata: visual kind, chart type when relevant,
filename, title, alt text, dimensions, byte size, and attachment status. It
never exposes a host path, browser profile, HTML, SVG, or generated script.

The split avoids conditional JSON Schema branches, which are not portable
across every supported model API. `render_chart` never exposes Mermaid source,
and `render_diagram` never exposes chart fields. Inside `render_chart`, providers
that materialize both series representations may send an empty inactive array;
the validator ignores only that neutral placeholder.

## Supported visuals and limits

Charts support bar, line, and scatter forms with up to eight series, 250 points
per series, and 1,000 points total. Grouped bars are additionally limited to 50
unique categories. Numeric values must be finite and between -10^15 and 10^15.
Text fields, category labels, series labels, and optional scatter-point labels
have fixed length limits enforced both before and inside the renderer.

The fixed chart design uses the Okabe–Ito colorblind-safe palette with redundant
visual distinctions:

- grouped bars combine color with distinct hatch patterns;
- lines combine color with dash patterns and marker shapes;
- scatter series combine color with marker shapes;
- legends reproduce the same distinctions.

The model cannot select colors, fonts, dimensions, CSS, or arbitrary rendering
options. This keeps output predictable and prevents visual configuration from
becoming an executable surface.

Mermaid initially accepts these diagram headers:

- `flowchart` / `graph` with `TB`, `TD`, `BT`, `RL`, or `LR` direction;
- `sequenceDiagram`;
- `stateDiagram-v2`;
- `classDiagram`;
- `erDiagram`.

Source is limited to 12,000 characters and 300 lines. Frontmatter and Mermaid
initialization directives are rejected, as are click/link callbacks, URLs,
images/icons, HTML, custom styles/classes, CSS/font/theme directives, and
unsupported diagram families. The renderer uses Mermaid `securityLevel:
"strict"`, disables HTML labels, validates generated SVG while it is detached
from the live document, and rasterizes only the validated result. Raw SVG is
never attached.

One user turn can attempt at most four renders. Rendering is globally serialized
because a fresh Chromium process is the expensive resource. The accepted PNG
size uses `BROWSER_MAX_SCREENSHOT_BYTES`; the shared reply limit uses
`WORKSPACE_TOOL_MAX_ATTACHMENTS`.

## Execution and security boundary

The visual tools do not call the model-facing `browser` tool. Their shared
Python handler calls a dedicated `VisualService`, which launches one fixed-code
`web_browser/visual_bridge.mjs` process and exits after one request.

Each render receives:

- a fresh tmpfs browser home under `/work`;
- a read-only BetterWright, BetterChromium, Mermaid, bridge, system, and font
  runtime;
- one unique generated-output job directory mounted writable at `/output`;
- Bubblewrap namespaces, the browser seccomp policy, `prlimit`, and a transient
  user-systemd scope with aggregate task, memory, swap, and CPU limits.

It receives no persistent profile, cookies, credential vault, download access,
resolver, certificates, host-network share, VPN namespace/helper, or netns
lease. The namespace has no external network, and BetterWright also applies a
policy that denies every URL. Rendering therefore does not contend with the
persistent browser's user-profile lock or its physical VPN lease.

The bridge accepts one bounded structured JSON document on standard input. It
contains no `code` field and has no generic navigation interface. Mermaid input
is passed as data, never interpolated into HTML. The process writes one expected
`render.png`; the host reopens it without following symlinks, bounds its bytes,
checks every PNG chunk and checksum, verifies its decoded scanline structure,
and requires exactly 1200×675 pixels before queueing it.

Generated files then follow the ordinary delivery snapshot, containment, upload
limit, attachment-description, and workspace expiry paths documented in
[Workspace tools](workspace.md).

## Install and enable

Follow the [BetterWright installer guide](../bot/deploy/betterwright/README.md).
From `bot/` on the Linux host:

```sh
sudo sh ./deploy/betterwright/install.sh
```

The installer consumes the committed npm lock with `npm ci`, installs exactly
BetterWright 1.10.0 and Mermaid 11.17.2 into a staging tree, runs the explicit
BetterChromium setup, verifies versions, files, imports, shared libraries, and
permissions, then atomically renames the completed root-owned tree into place.
A failed install leaves the previous runtime in place. npm and network access
are needed only during this operator-run install or upgrade; bot startup and
visual rendering never install packages or contact a CDN.

Enable the shared capability:

```dotenv
BROWSER_ENABLED=true
```

Choose and configure the persistent browser's `host` or `netns` mode as
explained in [Persistent browser](browser.md). Visual jobs are offline in either
case. Restart Kimi and check the capability summary for both `persistent
browser` and `visual rendering`.

## Deployment verification

Before enabling or after every runtime upgrade, run as the unprivileged bot
account from `bot/`:

```sh
uv run python -m deploy.betterwright.smoke_test
```

The smoke test exercises the production browser boundary, persistent-profile
isolation, one chart render, one Mermaid render, and host-side PNG validation.
It removes its synthetic profiles and visual output afterward. This Linux smoke
test is the authoritative integration check; ordinary CI validates Python,
Node syntax, the npm lock/audit, command construction, and security contracts
without downloading BetterChromium.

## Diagnostics

| Symptom | Meaning and action |
|---|---|
| Neither tool registers | `BROWSER_ENABLED` is false, the shared runtime is unavailable, or the selected persistent-browser sandbox/network probe failed. Follow [Persistent browser](browser.md). |
| `browser` registers but the visual tools do not | The installed runtime lacks the exact Mermaid bundle or its ownership is unsafe. Rerun the current installer and restart. |
| `Visual rendering is supported only on Linux` | The production containment boundary is unavailable on this host. Use Linux for deployment; tests and validation can still run elsewhere. |
| Render timeout | The input was pathological or the Chromium boundary exceeded `BROWSER_CALL_TIMEOUT_SECONDS`; simplify it rather than raising limits first. |
| Invalid or incomplete PNG | The renderer failed or the runtime is incompatible. Run the deployment smoke test and reinstall the pinned runtime. |
| Attachment limit reached | The turn already queued `WORKSPACE_TOOL_MAX_ATTACHMENTS` files. Remove an attachment or render on a later turn. |
| Diagram syntax rejected | Use one supported Mermaid family and remove custom directives, styling, links, HTML, images, or external content. |

## Upgrade and rollback

Do not change either dependency to `latest`. Review BetterWright and Mermaid
release notes and advisories, update `deploy/betterwright/package.json`, refresh
and review `package-lock.json`, update the exact version assertions and this
document, run `npm audit --omit=dev`, and pass the production smoke test on a
test instance.

The installer stages before replacement, so an installation failure preserves
the prior runtime. For an application rollback, check out the previous release
and rerun that release's installer to reproduce its matching locked runtime.
Never retain an application/runtime combination that fails the smoke test.
