# Fuel Finder compliance

Source of truth: the Fuel Finder developer portal (`https://www.developer.fuel-finder.service.gov.uk`),
pages **Developer guidelines** and **Terms and conditions of access** (the Aggregator Fair Use
Policy you agree to when registering). Read on **4 November 2026** from the portal's published
page text. **Re-check both pages before launch** and whenever the portal's release notes change.

## 1. Rate limits and polling frequency

**What the portal says**

| Item | Text (quoted or closely paraphrased) |
|---|---|
| Live limit, Information Recipient APIs | "**100 requests per minute per client.** **1 concurrent request** allowed per client." |
| Exceeding the limit | "Exceeding limits returns **HTTP 429** (Too Many Requests)." The older paragraph on the same page: a 429 is returned "for **each request made within the rest of the 5 minute time frame**", after which the limit resets. That paragraph still says "**X** requests within a 5 minute period": the number is a placeholder on the page. |
| Enforcement | "We reserve the right to **ban without notice** applications that regularly exceed or attempt to bypass the rate limits." The Aggregator may also "throttle access when required". |
| Higher limits | Contact the Fuel Finder team. |
| **Polling frequency (Fair Use Policy)** | If the data is used "for any purpose that promotes fuel retail opportunities to the motoring public, you must … **call the API with a frequency that on average in a single day is not more than 5 minutes**" (or use the most recent CSV), so that prices shown to the public don't differ materially from the Fuel Finder database. |
| Caching guidance (best practice) | Station data: cache 1 hour. Price data: cache 15 minutes. Use HTTP caching headers where available. |
| Response headers | Checked on a live response (4 Nov 2026): **no rate-limit headers** (no `X-RateLimit-*`, no `Retry-After` on success). Served via CloudFront London; `x-amz-cf-id` can be logged as a request ID. |

**What MileMap does**

- **Schedule:** every 5 minutes, incremental prices (since now − 2 h) and incremental stations
  (since the start of the previous day). Once an hour, a full prices download. Once a day, a full
  stations download, run at a different minute from the hourly prices download. Peak: about
  **20 requests in any 5-minute window** (about 4 a minute). Daily total about **1,150
  requests**, about 0.8 a minute on average.
  - This is **inside the limit by a factor of more than 100** (100 a minute allows 144,000 a day).
  - It **meets the Fair Use polling requirement** (an API call every 5 minutes on average) that
    applies once the public map shows prices. Polling less often would *breach* it, so the
    5-minute cadence stays.
- **One request at a time**, with at least 0.75 s between requests (at most 80 a minute even in
  a burst). Runs never overlap: a lock file stops a second run starting while one is in progress.
- **On HTTP 429:** stop the run immediately, with no retries. Every further request in the
  5-minute window would also get 429 and could count as "attempting to bypass" the limit. Record
  a cool-down until at least 5 minutes (plus jitter) after the 429. Following runs skip their API
  calls until it has passed, and log that they skipped. Any `Retry-After` header is honoured if
  it is longer.
- **On 5xx and timeouts:** up to 3 retries with exponential backoff, always within the run's hard
  deadline.
- **Hard deadline per run** (see `docs/raw-storage.md` and the collector code): 45 s for a normal
  5-minute run, 120 s for runs that include a full download.
- **Monitoring:** every run's request count, retries, 429s, durations and errors go into its
  manifest, as the guidelines recommend ("track rate limit usage", "alert on unusual usage
  patterns").

## 2. Security (Developer guidelines)

| Guideline | MileMap |
|---|---|
| Never expose client secrets or access tokens in client-side code or public repositories | Credentials only in `.env` (git-ignored, mode 600) and, on Azure, the platform's secret store. The public site never calls the Fuel Finder API. |
| Store secrets in environment variables / a secrets manager | Yes |
| Separate credentials per environment and per application | One production application today. Create separate credentials if a test environment or a second app is added. |
| Rotate secrets regularly and immediately if compromised | To do: set a rotation reminder (every 6 months). |
| Request minimum scopes | Read-only Information Recipient access only. |
| Keep access tokens short-lived; issue new tokens rather than reusing old ones | Access tokens are used only within their 1-hour lifetime and renewed 2 minutes before expiry. The refresh token (48 hours) is used to renew. Expired tokens are never reused. Between runs, the current token is cached in a file only the user account can read (`data/state/`, git-ignored), so we don't request a new token every 5 minutes. |
| Don't log secrets or tokens; mask in logs and errors | All error text passes through redaction. Tests check that secrets never appear in error messages. |
| HTTPS / TLS 1.2+ | Yes (httpx default). |
| Monitor token usage for anomalies | Token generations and refreshes are counted in each manifest. |

## 3. Displaying prices (applies from Phase 2)

From the Fair Use Policy, as checklist items for the public site:

- [ ] **Near real-time:** API polled every 5 minutes (see above). The site's current-prices file
      is rebuilt from the latest successful poll, and the page shows "prices as of HH:MM".
- [ ] **No amending or hiding** price information to promote or hinder any trader, and no "false
      image of the fuel retail ecosystem".
- [ ] **Report a discrepancy:** link to `https://www.fuel-finder.service.gov.uk/motorist/price-report`
      on every station panel (or forward complaints received to the aggregator).
- [ ] **Unbiased presentation:** no filtering or selective display that favours suppliers;
      comparisons based on objective criteria (price, distance) only.
- [ ] **Use the data as provided:** no modification that could mislead. **Don't alter timestamps or
      other metadata.** We store timestamps exactly as received and only *convert* time zones for
      display. Flags such as "price outside 100–300p" or "effective time after submission time"
      are added alongside the data and never overwrite it.
- [ ] **Closure status** is shown next to prices (58 temporarily closed stations still have
      prices; see `docs/history-depth.md`).
- [ ] **Attribution** in the footer: "Contains public sector information licensed under the Open
      Government Licence v3.0."

## 4. ⚠️ Open question: publishing the open archive

Phase 5 plans to publish the full price history as an open dataset. Two pieces of portal text
pull in different directions:

- **Terms and conditions:** "By registering as an Information Recipient you are agreeing to the
  terms and conditions of the **Open Government Licence** as well as the Fair Use policy." OGL v3.0
  allows copying, publishing, distributing and adapting the information, with attribution.
- **Developer guidelines → Compliance and Legal → Data Usage:** "**Don't redistribute raw API
  data**", alongside "Attribute data sources appropriately".

**Until this is resolved:**
- Collecting and archiving for our own analysis, and showing prices on the map under the Fair
  Use Policy, are clearly allowed. The collector goes ahead.
- **No raw Fuel Finder data is published** (no dataset release, no public R2 path). The raw bucket
  stays private.
- **Action:** email the Fuel Finder Team (fuel.finder@ve3.global) asking whether a cleaned,
  attributed historical price dataset may be published under OGL v3.0, and record the answer here.
  A possible middle ground, if needed: publish derived and aggregated data (daily station
  medians, change events) rather than raw API responses.

## 5. Other sources

To be added as each source is integrated (Phase 1: DESNZ, FRED/EIA Brent, Tankerkönig).

## Change log

| Date | Change |
|---|---|
| 2026-11-04 | First version: Developer guidelines + Fair Use Policy read; schedule confirmed; open question on archive publication. |
