# Wolfram|Alpha

The optional `wolfram_alpha` tool gives the model access to Wolfram|Alpha's LLM API for mathematics, science, unit and date conversions, statistics, and factual questions with a computed answer. It is a searchable `MEMBER` tool: the model finds it through `browse_tools`, and the normal tier, scope, pin, and block rules still apply when it is called.

## Configuration

Create an AppID in the [Wolfram|Alpha Developer Portal](https://developer.wolframalpha.com/) and place it in the deployment's untracked `.env`:

```dotenv
WOLFRAM_ALPHA_APP_ID=your-app-id
```

The AppID is a `SecretStr` setting. It's never available through operator Markdown settings or per-tool configuration, and the tool isn't registered when it's blank.

Optional environment settings:

```dotenv
WOLFRAM_ALPHA_TIMEOUT_SECONDS=30
WOLFRAM_ALPHA_MAX_CALLS_PER_TURN=3
WOLFRAM_ALPHA_MAX_OUTPUT_CHARS=6800
WOLFRAM_ALPHA_CALL_COST_USD=
```

`WOLFRAM_ALPHA_CALL_COST_USD` is an optional price per request (a retry counts as the same request) that feeds the local paid-tool ledger and `/usage`. The estimate is recorded for every attempted call, even one the provider answers with an error. Leave it blank if you do not know your effective rate; Wolfram's own billing is the source of truth either way.

## Tool behavior

The model supplies a required single-line English `input` and may choose `metric` or `nonmetric` units. Kimi sends the configured output cap as the LLM API's `maxchars`, enforces the same cap itself, and wraps the answer as untrusted context. Any image URLs in the answer are returned as plain text; the tool does not fetch or attach them. The AppID travels in an HTTPS bearer header, never in the query URL.

The per-turn allowance counts tool requests, including ones the provider answers with an error. Invalid arguments are rejected before they spend the allowance. A transport or transient HTTP failure gets one retry. Credential, quota, timeout, and provider failures return short, safe errors that never include the AppID, response body, or request URL. When Wolfram cannot interpret the query, its suggestions are returned (size-capped, as untrusted context) so the model can rephrase.

Review Wolfram's API and commercial terms before production use. The API's output format can change over time, so nothing in the bot depends on its exact shape.