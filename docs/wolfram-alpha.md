# Wolfram|Alpha

The optional `wolfram_alpha` tool gives the model access to Wolfram|Alpha's LLM API for mathematics, science, unit and date conversions, statistics, and computational factual queries. It's a searchable `MEMBER` tool: the model finds it through `browse_tools`, and normal registry tier, scope, pin, and block rules still apply at dispatch.

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

`WOLFRAM_ALPHA_CALL_COST_USD` is an optional deployment-known price per logical request, including its possible bounded retry, for the local paid-tool ledger. Attempted provider calls record the estimate even when the provider ultimately returns an error. Leave it blank when the deployment doesn't know its effective rate. Wolfram remains the billing authority.

## Tool behavior

The model supplies a required, single-line English `input` and may select `metric` or `nonmetric` units. The host sends the configured output cap as the LLM API's `maxchars`, independently enforces the same cap, and wraps successful text as untrusted context. Provider image URLs are returned only as text; the tool doesn't fetch or attach them. The AppID is sent in an HTTPS bearer header, not in the query URL.

The per-turn call allowance counts logical tool requests, including failed provider requests. Invalid model arguments are rejected before they spend the allowance. Transport and transient HTTP failures receive one bounded retry. Credentials, quota errors, timeouts, and provider failures return short safe errors without including the AppID, response body, or request URL. A bounded suggestion body from an uninterpretable-query response is returned as untrusted recovery context so the model can reformulate the query.

Review Wolfram's API and commercial terms before production use. API output can change over time, so callers must treat its content as computed provider data, not a stable serialization contract.