# Integrations: what leaves the host

Scope: the market-data feeds of Phase 5, security prices and exchange
rates. This page answers one question for an operator or reviewer: with a
feed turned on, which bytes cross a network boundary, to where, and what
stays home. Controls are referenced by their `docs/SECURITY.md` id; their
implementation status is in [`CONTROLS.md`](CONTROLS.md). Ingestion
(upload, drop folder, email) is covered by [`ingestion.md`](ingestion.md),
household notification channels by [`notifications.md`](notifications.md).

## Short answer

**By default nothing leaves the host.** `PERFIN_PRICE_PROVIDER=none` and
`PERFIN_FX_PROVIDER=none` are the defaults; then prices and exchange rates
are only what people type in (`POST /securities/{id}/prices`, `POST
/fx-rates`), and the `refresh_prices` job returns without opening a
connection (`backend/tests/security/test_market_data_egress.py::test_feeds_are_off_by_default`,
`backend/tests/integration/test_market_data_refresh.py::test_refresh_is_scheduled_and_does_nothing_by_default`).
In the Phases 5+6 live run (default settings, live `perfin worker`), the
scheduled `refresh_prices` job finished in 4 ms without a connection, and
holdings were valued at the statements' own prices (`source = statement`).
Neither external feed has been run against its real service here; their
requests are tested against a mock transport (respx) only.

When an operator turns a feed on:

| Feed | Setting | Destination | What is sent | What is never sent |
| --- | --- | --- | --- | --- |
| Prices, generic JSON | `PERFIN_PRICE_PROVIDER=http_json` | the host of `PERFIN_PRICE_PROVIDER_URL`, nothing else | one `GET` per distinct ticker symbol of the instance, the symbol percent-encoded into the template's single `{symbol}` placeholder; optionally one instance credential header | quantities, holdings, cost basis, accounts, which or how many households hold a symbol, household or user identity, cookies |
| FX, ECB reference rates | `PERFIN_FX_PROVIDER=ecb` | `www.ecb.europa.eu:443` | one `GET` of the fixed URL `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml` per run | anything at all about the installation: no currency list, no amounts, no query |

Both run only in the worker, in the instance-level `refresh_prices` job
(`backend/app/workers/handlers/market_data.py`): every
`PERFIN_PRICE_REFRESH_MINUTES` (360, six hours, by default; 5 at the
least), and once more when a member asks for it (`POST
/investments/prices/refresh`, the "Refresh prices" button on the
Investments page). A request only queues the job: requests within the same
minute share one run for the whole instance, the job names no household,
and it asks the provider exactly what the scheduled run asks, so a request
tells the provider nothing about who made it and cannot be used to call
it more often than once a minute. The API process never contacts a feed.
`perfin config check` prints a warning naming the destination for each
enabled feed (`prices.provider_enabled`, `fx.provider_enabled`) and refuses
a misconfigured price provider (`prices.provider_invalid`, an error outside
dev mode).

What stays inside: when a run changes a household's prices, that household
gets a `price.updated` event carrying a count (never a symbol) on its own
event stream, and the open app refetches holdings and net worth.

## Prices: `http_json`

PerFin ships no vendor integration and no API key. The operator points the
generic adapter (`backend/app/integrations/prices/http_json.py`) at an
endpoint and says where the close sits in its JSON answer:

```
PERFIN_PRICE_PROVIDER=http_json
PERFIN_PRICE_PROVIDER_URL=https://quotes.example.com/v1/last/{symbol}
PERFIN_PRICE_PROVIDER_PRICE_FIELD=data.close
PERFIN_PRICE_PROVIDER_DATE_FIELD=data.date          # optional, default: today
PERFIN_PRICE_PROVIDER_CURRENCY_FIELD=data.currency  # optional
PERFIN_PRICE_PROVIDER_CURRENCY=USD                  # when no currency field
PERFIN_PRICE_PROVIDER_AUTH_HEADER=X-Api-Key         # optional, with the key
PERFIN_PRICE_PROVIDER_API_KEY=...                   # optional, secret
PERFIN_PRICE_PROVIDER_MAX_SYMBOLS=500
PERFIN_PRICE_REFRESH_MINUTES=360                    # how often the worker asks
```

### The request, byte by byte

- **Method and URL**: `GET`, the template with `{symbol}` replaced by the
  symbol, percent-encoded as one path or query segment (`/` becomes `%2F`).
  The template is checked at start-up: https only, exactly one `{symbol}`,
  no other placeholder, the placeholder not in the host, no user-info, no
  fragment. The rest of the template (path, a fixed query) is the
  operator's and the same for every request.
- **Which symbols**: the distinct `security.symbol` values across all
  households, sorted, at most `PERFIN_PRICE_PROVIDER_MAX_SYMBOLS` per run.
  A symbol that is shared by several households is asked for **once**, so
  the provider cannot count holders or tell households apart. Only symbols
  matching `^[A-Za-z0-9.^/:_-]{1,32}$` with at least one letter or digit
  are sent (a symbol of dots alone is never put into a URL).
- **Headers**: `Host`, `User-Agent: PerFin`, `Accept: application/json`,
  `Accept-Encoding: identity`, `Connection` and, only when configured, the
  credential header. No cookies, no `Authorization` unless the operator
  names it as the credential header, no request id, no version string.
- **Body**: none.
- **Credential**: an instance credential of the operator's account with the
  provider. It goes in the configured header only (never in the URL, so it
  is not in proxy or access logs), is a `SecretStr` (never in `repr`, logs
  or config-check messages), and header names that would change the request
  (`Host`, `Cookie`, `Content-Length`, ...) or carry line breaks are
  refused.

The test `test_price_request_carries_only_the_symbol` asserts the exact
header set, the empty body and that the query is the template's own;
`test_symbols_are_percent_encoded_and_invalid_ones_never_sent` and
`test_api_key_goes_in_its_header_only` cover encoding and the credential.

What the provider learns: that some PerFin instance at the egress address
holds or watches these symbols. Operators who consider their symbol list
sensitive should keep the feed off and enter prices by hand.

### The answer

Read with numbers as `Decimal` (never `float`), capped at 256 KiB, 10 s
timeout. The close must be a positive decimal that fits `NUMERIC(28, 10)`
(rounded half-even to 10 places), the date an ISO date not after today and
not before 1900-01-01 (an older one is dropped, as are FX rates outside that
window; SECURITY.md F8) and
the currency an ISO 4217 code. A quote applies only to securities with the
same symbol **and** currency; a non-200 answer or an unusable body skips
that symbol (logged as a count, never with the symbol).

### Choosing a provider and a pace

Any quote service that answers one symbol with JSON over https fits the
adapter. What to check with the provider you pick:

- **One request per symbol**: the URL must take the symbol in its path or
  query and answer with that symbol's latest price. A bulk endpoint (many
  symbols per request) is not what the adapter calls.
- **Where the price is**: the dotted path to the number
  (`PERFIN_PRICE_PROVIDER_PRICE_FIELD`: `price`, `data.close`, `0.close`
  for a one-element array). If the answer carries the price's day as an ISO
  date, name it (`PERFIN_PRICE_PROVIDER_DATE_FIELD`); a Unix timestamp is
  not read, and without a date field the price is dated today.
- **The key in a header**: most services accept their key in a request
  header (`PERFIN_PRICE_PROVIDER_AUTH_HEADER` and
  `PERFIN_PRICE_PROVIDER_API_KEY`; a value such as `Token abc...` goes in
  the key as a whole). Prefer that to a key in the URL, which ends up in
  proxy logs.
- **What it covers**: services differ on mutual funds (a fund's price is
  its end-of-day NAV, published once a day), on symbols outside the US,
  and on options and bonds, which most do not quote by the symbol a broker
  prints. A symbol the provider does not know is skipped and its holding
  keeps the last price PerFin has (a statement's, an export's, a manual
  one).
- **Its limits**: a run is one request per distinct symbol of the
  instance. With 40 symbols, `PERFIN_PRICE_REFRESH_MINUTES=15` is about
  3,800 requests a day; set the pace and
  `PERFIN_PRICE_PROVIDER_MAX_SYMBOLS` within the plan you have.

No provider has been called from this project's own test environment (the
requests are tested against a mock transport), so check the three settings
against the provider's current documentation, then `perfin config check`
and the first `refresh_prices` run in the worker's log (it logs counts:
symbols asked, quotes read, prices written).

**What a refreshed price means.** A price feed moves prices, not
holdings. Units and cost basis are those of the last import of each
account (a statement or a positions export), so between imports a market
value is an estimate: the latest price times units the account may no
longer hold. `GET /investments/holdings` names the last import's day per
account (`positions_as_of`) and each price's source and time, and the
Investments page says so above the table.

### Manual prices win

A fetched quote creates the day's `price` row or replaces an earlier
fetched one (`source` is the provider name, `http_json`); a `manual` row is
never replaced (`backend/app/integrations/prices/store.py`).

## Exchange rates: `ecb`

`backend/app/integrations/fx/ecb.py` downloads the European Central Bank's
daily euro reference rates. The URL is a constant in the code, never taken
from settings or input. The request has no query and the headers `Host`,
`User-Agent: PerFin`, `Accept: application/xml`, `Accept-Encoding:
identity`, `Connection` (`test_ecb_request_is_the_fixed_url_and_nothing_else`).
Every PerFin installation asks exactly the same question.

The answer (64 KiB cap) is parsed with `defusedxml` with DTDs, entities and
external references forbidden (SECURITY.md C6;
`backend/tests/unit/test_market_data.py::test_ecb_refuses_hostile_or_empty_documents`).
Only the newest day is used. Because conversions never triangulate,
`backend/app/integrations/fx/store.py` stores, per household, `1 C = r[B] /
r[C] B` for each currency `C` its accounts and securities use against its
base currency `B` (`Decimal`, 34 digits, rounded once half-even to 12
places). A day with a **manual** rate for the pair, in either direction, is
left alone; a fetched rate of the day is replaced.

## Transport rules (both feeds)

Both feeds use `app.core.http.SafeHttpClient` (SECURITY.md B5):

- the egress allowlist of the client is the feed's own `host:port` only
  (the template's host; `www.ecb.europa.eu:443`), https only, no plain http
  even in dev mode;
- DNS is resolved by PerFin and **every** address of the answer must be
  public: loopback, RFC 1918, ULA, CGNAT, link-local (including the cloud
  metadata address `169.254.169.254`) and their IPv4-mapped / NAT64 / 6to4
  forms are refused, and the connection is pinned to the vetted address
  (`test_price_host_resolving_to_internal_addresses_is_never_contacted`,
  `test_ecb_host_resolving_to_internal_addresses_is_never_contacted`,
  `test_mixed_answer_with_one_private_address_is_refused`);
- redirects only to the same scheme, host and port
  (`test_redirect_to_another_host_is_refused`), compressed bodies refused,
  bodies capped while reading;
- in the Compose deployment the worker has no route out (SECURITY.md H2):
  with `HTTPS_PROXY` set the request is tunnelled through the `egress`
  allowlist proxy, which applies the same address rules. Add the feed's
  host to `deploy/egress-allowlist.txt` (examples are in the file).

## Not covered here

- Notification channels (SMTP, webhooks, ntfy, ...) are a later phase.
- The optional Have I Been Pwned password check (k-anonymity, 5 hex
  characters of a SHA-1) is described in AUTH.md "Passwords".
- OIDC discovery and token calls go to the configured IdP only (AUTH.md,
  SECURITY.md B5).
