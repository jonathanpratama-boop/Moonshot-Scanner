# SEC fixture provenance

`CIK0000320193_submissions_trimmed.json` is a trimmed copy of a real public SEC EDGAR response:

- URL: https://data.sec.gov/submissions/CIK0000320193.json (Apple Inc.)
- Retrieved: 2026-09-30T10:26:06Z (file write time) from the build container, one request, HTTP 200, 164,825 bytes,
  SHA-256 of the full response `b5ad70866ae367210f2fc851d4d90bb87a93814df9ad8e939c7e2ae5f0ce47e7`.
- That single connectivity check was sent with a User-Agent that identified the tool but did **not**
  include a contact email, i.e. it did not fully follow SEC's declared-User-Agent format. The ASTRA
  adapter itself refuses to send any request until `ASTRA_SEC_USER_AGENT` includes a contact email.
- Trimming: kept top-level identity fields (`cik`, `entityType`, `sic`, `sicDescription`, `name`,
  `tickers`, `exchanges`, `fiscalYearEnd`, `stateOfIncorporation`, `formerNames`) and 15 rows of
  `filings.recent` (the 12 most recent filings plus the 3 most recent 8-Ks), all columns unchanged;
  `filings.files` emptied. Addresses, phone, EIN and older filings were dropped.

It is used only by tests (parser + fake HTTP transport). It is not a live observation for ASTRA.
