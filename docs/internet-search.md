# Internet search

`internet_search` is the member-tier core tool for searching the live web and
for reading pages the model already has URLs for. It registers when `EXA_API_KEY`
or `BRAVE_API_KEY` is set; with neither key configured, the model never sees it.

The model sees one tool and never learns which provider answered it. Provider
names, credentials, and cost stay out of a successful result, so which vendors a
deployment pays for does not leak into the conversation. A partial failure is
invisible for the same reason: when one provider is down and the other answers,
the model gets results rather than an incident report.

## Request modes

- `search` takes a `query` and returns up to `num_results` combined results. A
  query is capped at 400 characters and 50 words, the strictest limit any
  configured provider imposes.
- `contents` takes one or more absolute HTTP(S) `urls` and reads those pages.
  Only Exa can do this. A Brave-only deployment returns an error saying no
  page-reading provider is configured, rather than quietly turning the request
  into a search.
- `content_mode` is `highlights` by default. Asking for `text` sends the call
  only to a backend that can return full page text.
- Domain, publication-date, and country constraints are optional. Where a
  provider cannot apply one itself, Kimi applies it to that provider's
  results afterward, so a constraint is never dropped quietly. A result whose
  publication date the provider never reported cannot satisfy a date
  constraint, and is dropped.

Each result carries `title`, `url`, the useful `content`, and the publication
date or author where the provider reports them. Results and page text are
stamped as untrusted context. A search that genuinely found nothing says so
plainly:

```json
{"results": [], "message": "No matching results found."}
```

That is distinct from a timeout or a provider failure, which come
back as an `error` object. Without the distinction the model cannot tell "the
web has no answer" from "the search is broken", and will retry the wrong one.

## Provider behavior

Exa outranks Brave wherever both are configured. The default `blend` strategy
calls every eligible provider at once and interleaves their results into one
list. Duplicate URLs are collapsed by canonical form, and the higher-ranked
provider keeps the page: if Brave lists a page second and Exa lists it eighth,
the merged list keeps Exa's copy at Exa's position rather than Brave's earlier
one. If one provider fails, the other's results still come back.

To change strategies, edit `strategy` in `config/tools/internet_search.md`.
`failover` tries Exa, then Brave, and stops at the first provider that answers,
including one that answers with zero matches. Page reads always use failover
rather than blend, since there is nothing to interleave.

Each turn gets ten provider operations by default. A two-provider blend spends
two of them, so five blended searches use up a turn; Exa-only, Brave-only, and
failover calls spend one per provider actually called. Once the allowance is
gone the tool returns an error for the rest of the turn, and the next user
prompt starts a fresh one. The bounded transport retry inside a provider call
does not spend a second operation: the limit is there to cap how much searching
one turn can do, not to charge a provider for a flaky connection.

## Cost and privacy

Every provider response is priced on its own and written to the schema v1
`paid_usage_ledger`. A cost the provider reports wins, including a reported
zero. When it reports nothing, the configured per-call price for that provider
and mode is used; when that is unset too, the call is not billed locally.
`/usage` breaks paid tool spend out into its own column and counts it in the
estimated cost for the window.

A ledger row records provider, tool, dollars, turn, user, channel, and guild.
Queries and results are never written to it, so the ledger can be read for
spending without exposing what anyone searched for.

Bear in mind that the local numbers can undercount. If a request fails before
its cost is reported, or the ledger write itself fails, only the provider's own
dashboard has the full picture.

See [Configuration](configuration.md#internet-search-gated), [Tool Catalog](tools.md),
and [Database](database.md#model-paid-tool-and-bounded-tool-usage).
