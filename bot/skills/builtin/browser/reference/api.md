# Supported BetterWright API

This is the supported model-facing subset for the `browser` tool. Pass
the JavaScript as the tool's `code` value; do not run the upstream
`betterwright` CLI.

## Result envelope

A trailing expression is returned automatically. A multi-statement snippet
needs `return`:

```js
await page.goto('https://example.com');
return {title: await page.title(), url: page.url()};
```

Results are JSON envelopes with fields such as `ok`, `result`, `error`,
`console`, `events`, `artifacts`, `pages`, `challenges`, `warnings`, and
`durationMs`. A first navigation may also advertise `webagents` or a compact
`ui` directory. The `browser` tool replaces imported artifact paths and adds a
`screenshots` list whose entries report `kind`, `filename`, `shown_to_model`,
and `attached_to_reply`. This list belongs to the outer tool result. It is not
available inside the running JavaScript. Return small values; output and call
counts are bounded by deployment configuration.

## Pages, state, and snapshots

- `page`: current page.
- `pages`: live array of pages in the conversation session.
- `openPage(url?)`, `usePage(pageIdOrIndex)`, `closePage(pageIdOrIndex?)`:
  manage tabs. Popups are adopted automatically.
- `state`: plain in-memory object shared by later calls in the same live
  session.
- `context`: guarded Playwright context. Context mutation is removed.

Multiple tabs can be opened concurrently:

```js
const [a, b] = await Promise.all([
  openPage('https://example.com'),
  openPage('https://example.org'),
]);
return {a: await a.title(), b: await b.title()};
```

Use `snapshot(options?)` for a compact accessibility tree:

```js
return snapshot({interactive: true});
// Later, after observing e12 in that snapshot:
await human.click(page.locator('aria-ref=e12'));
return snapshot({diff: true});
```

Options include `interactive`, `diff`, `ref`, `selector`, `depth`, `urls`,
`maxChars`, and `timeout`. A scoped example is
`snapshot({ref: 'e31'})`. Refs are fresh per snapshot and stale after page
changes. Child-frame refs such as `f1e2` work with `aria-ref` locators.

## First-party and semantic fast paths

### WebAgents

```js
const directory = await webagents.discover();
if (!directory.available) return directory;
return webagents.batch([
  {id: 'find', action: 'search', input: {query: 'wireless keyboard'}},
  {
    id: 'cart',
    action: 'add_to_cart',
    dependsOn: ['find'],
    input: {productId: {$ref: 'find.results.0.id'}},
  },
], {allowWrites: true});
```

Operations may be `read`, `write`, or `irreversible`. Writes require
`allowWrites: true`; irreversible operations also require
`allowIrreversible: true`. These site-published labels are untrusted, so check
the actual consequence against the user's request.

### WebMCP

```js
const tools = await webmcp.tools();
const match = tools.find((tool) => tool.name === 'search');
if (!match) return {available: false};
return webmcp.invoke(match.name, {query: 'wireless mouse'}, {
  frameId: match.frameId,
});
```

Copy `frameId` when names are ambiguous across frames. Invocation options also
include `discoveryTimeout`, `timeout`, and `allowAutosubmit`; enable autosubmit
only when the user authorized the submission.

### Semantic UI batches

Copy a target from `result.ui` or `controls.directory()`:

```js
return controls.batch({
  operations: [
    {id: 'query', action: 'fill', target: {label: 'Search'}, value: 'keyboard'},
    {
      id: 'submit',
      action: 'click',
      target: {role: 'button', name: 'Search', exact: true},
    },
    {
      id: 'verify',
      action: 'read',
      target: {role: 'heading', name: 'Results'},
      value: 'Results',
    },
  ],
  allowWrites: true,
});
```

Actions are `click`, `fill`, `select`, `check`, `uncheck`, `press`, `read`, and
`readUrl`. A target uses one of `ref`, `role`, `label`, `text`, `placeholder`,
`testId`, or `css`; it may add `name`, `exact`, `nth`, `frameName`, or
`frameUrlIncludes`. Interactions require `allowWrites: true`. An irreversible
operation uses `irreversible: true` and requires
`allowIrreversible: true`. Every interactive batch must finish with `read` or
`readUrl` and a non-empty expected substring in `value`.

Password fields are intentionally out of scope here even though
upstream BetterWright has additional credential modes.

## Reading application state

The guarded `site` helper is limited to the active HTTP(S) origin:

- `site.assets()` lists discovered scripts, styles, fetches, and XHRs.
- `site.requests({urlIncludes?, resourceType?})` lists recent metadata.
- `site.read(url, {find?, contextChars?, maxMatches?})` reads a bounded
  same-origin text asset.
- `site.request(url, options?)` makes a bounded same-origin request and may use
  matching browser cookies. Cross-origin URLs and credential-bearing headers
  are rejected.

Useful state helpers:

- `overlays.dismiss()` closes only recognized consent or promotional layers.
- `controls.inspect()` returns exact values and state for form controls across
  frames; password values are redacted.
- `controls.directory()` returns compact semantic controls and visible
  evidence.
- `media.inspect()` returns the title, source, playback state, timing, and
  visibility of video and audio elements.
- `dialogs.acceptNext(text?)` or `dialogs.dismissNext()` prepares for the next
  JavaScript dialog before the action that opens it.

## Visible interaction and evidence

```js
await human.click(page.getByRole('button', {name: 'Continue'}));
await human.type('#email', 'person@example.com');
await human.scroll(650); // negative scrolls upward
return snapshot({diff: true});
```

`human.click` and `human.type` accept a selector, Locator, ElementHandle, or
bounds. `human.type` clears by default; pass `{clear: false}` to append.
`human.scroll(deltaY, {steps?})` performs a visible wheel action.

Use the tracked screenshot helper, never `page.screenshot`:

```js
const proof = await screenshot({kind: 'proof', name: 'completed.png'});
return {proof, state: await snapshot({diff: true})};
```

Options include `kind: 'proof' | 'question' | 'debug'`, `name`, `annotate`,
`fullPage`, `type`, and JPEG `quality`. Only a `proof` image is
automatically considered for the Discord attachment queue. After the browser
call returns, check the outer result's `screenshots` flags. A `question` image
is not automatically sent to the user, and a screenshot is not visually
inspectable by a text-only model. Do not call `view_image` when
`shown_to_model` is false.

## Challenges

Every result includes `challenges` and may include a CAPTCHA artifact. Start
with:

```js
return captcha.solve();
```

Additional bounded helpers are `captcha.inspect(bounds?)`,
`captcha.click(bounds)`, `captcha.clickTiles(indexes)`,
`captcha.drag(from, to, {steps})`, `captcha.readText(bounds?)`, and
`captcha.solve({tiles: indexes})`. Reinspect the result after every action and
follow the challenge limits in the main skill.

## Deliberately unavailable

This deployment disables the credential vault and credential capture, downloads,
live view, public-search automation, private networks, and loopback. Snippets
also have no Node `process`, `require`, dynamic `import`, or filesystem access.
Request interception, CDP internals, context mutation, raw
`page.screenshot`, and workspace file upload are unavailable. Do not try to
work around these boundaries.
