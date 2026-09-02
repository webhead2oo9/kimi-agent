# Internet search

`internet_search` is the member-tier core tool for searching the live web and for reading pages the model already has URLs for. It registers when `TINYFISH_API_KEY`, `EXA_API_KEY`, or `BRAVE_API_KEY` is set; with no key configured, the model never sees it. TinyFish is the one provider that is free at any wallet balance, so a deployment with no search budget can still offer the tool by setting `TINYFISH_API_KEY` alone.

The model sees one tool and never learns which provider answered. Provider names, credentials, and cost stay out of a successful result, so which vendors you pay for does not leak into the conversation. A partial failure is invisible for the same reason: when one provider is down and another answers, the model gets results rather than an incident report.

## Request modes

- `search` takes a `query` and returns up to `num_results` combined results. A query is capped at 400 characters and 50 words, the strictest limit any configured provider imposes.
- `contents` takes one or more absolute HTTP(S) `urls` and reads those pages. TinyFish and Exa can do this; Brave cannot. A Brave-only deployment returns an error saying no page-reading provider is configured, rather than quietly turning the request into a search.
- `content_mode` is `highlights` by default. Asking for `text` sends the call only to a backend that can return full page text, and the two entry points are judged separately: TinyFish search returns snippets and is skipped for a `text` search, while TinyFish page reads return whole pages and are eligible.
- Domain, publication-date, and country constraints are optional. Where a provider can't apply one itself, Kimi applies it to that provider's results afterward, so a constraint is never dropped quietly. A result whose publication date the provider never reported can't satisfy a date constraint, and is dropped.

Each result carries `title`, `url`, the useful `content`, and the publication date or author where the provider reports them. Results and page text are stamped as untrusted context. A search that genuinely found nothing says so plainly:

```json
{"results": [], "message": "No matching results found."}
```

That is distinct from a timeout or a provider failure, which come back as an `error` object. Without the distinction the model can't tell "the web has no answer" from "the search is broken", and will retry the wrong one.

## Provider behavior

TinyFish outranks Exa, which outranks Brave, wherever more than one is configured. The default `blend` strategy calls every eligible provider at once and interleaves their results into one list. Duplicate URLs are collapsed by canonical form, and the higher-ranked provider keeps the page: if Brave lists a page second and Exa lists it eighth, the merged list keeps Exa's copy at Exa's position. If one provider fails, the others' results still come back.

To change strategies, edit `strategy` in `config/tools/internet_search.md`. `failover` tries TinyFish, then Exa, then Brave, and stops at the first provider that answers, including one that answers with zero matches. Page reads always use failover rather than blend, since there's nothing to interleave.

Ranking the free provider first doesn't on its own make a deployment cheaper. Under `blend` every configured provider is called on every search, so Exa and Brave are still billed for calls whose results TinyFish already covered. Pairing `TINYFISH_API_KEY` with `strategy: failover` is what actually keeps the paid providers idle until TinyFish fails.

Two provider-specific gaps are worth knowing. TinyFish search has no result-count parameter, so it contributes at most one page of results however high `num_results` goes; a blend with Exa makes up the difference. It also has no safesearch parameter, so `INTERNET_SEARCH_SAFESEARCH` constrains Brave only, and a TinyFish-first deployment isn't applying it to the provider it reaches first.

Each turn gets ten provider calls by default (`INTERNET_SEARCH_MAX_BACKEND_CALLS_PER_TURN`). A two-provider blend spends two of them, so five blended searches use up a turn; Exa-only, Brave-only, and failover calls spend one per provider actually called. Once the allowance is gone the tool returns an error for the rest of the turn, and the next user message starts a fresh one. A retry after a transport error inside one provider call does not spend a second one: the limit caps how much searching a turn can do, not how flaky a connection is.

## Cost and privacy

Every provider response is priced on its own and written to `paid_usage_ledger`. A cost the provider reports wins, including a reported zero. When it reports nothing, the configured per-call price for that provider and mode is used; when that is unset too, the call isn't billed locally. `/usage` breaks paid tool spend out into its own column and counts it in the estimated cost for the window.

A ledger row records provider, tool, dollars, turn, user, channel, and guild. Queries and results are never written to it, so the ledger can be read for spending without exposing what anyone searched for.

The local numbers can undercount. If a request fails before its cost is reported, or the ledger write itself fails, only the provider's own dashboard has the full picture.

TinyFish is free and reports no cost, so TinyFish calls write no ledger rows and the bot exposes no TinyFish cost settings. If TinyFish starts metering these endpoints, cost reporting and configuration must be added before `/usage` can attribute that spend.

See [Configuration](configuration.md#internet-search-gated), [Tool Catalog](tools.md), and [Database](database.md#model-paid-tool-and-bounded-tool-usage).
