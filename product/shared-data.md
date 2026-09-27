# Shared official data

## Accepted owner decisions

- **P-02:** Every client uses the same shared official company data. A user's
  formulas or local machine cannot change the official base.
- **P-03:** Automatic jobs retain downloaded filings in S3. PostgreSQL owns the
  application's fundamentals and shared calculated fields.
- **P-04:** Default research uses the latest adjusted official numbers. Corrections
  must reach historical periods, dependent ratios, and every client's view.
- **P-05:** The server refreshes sources automatically, independent of UI requests.
- **P-09:** Track historical revisions and let users inspect which figures changed,
  their previous and new values, and the evidence for each adjustment.

## Meaning of official and calculated

A reported fact and a calculated ratio are different records. Preserve the
reported value, unit, period, scope, filing identity, and source location. Store
the definition and dependencies of a calculated value. Label market quotes and
FX by their provider; they are not company-reported fundamentals.

Keep missing evidence distinct from zero. Preserve historical observations so a
change is explainable. Historical evidence is not the default current answer.
Every personal view must use the same official inputs for the same data revision.

## Database ownership

**P-17:** Python owns the PostgreSQL application schema and migrations through
SQLAlchemy and Alembic. FastAPI, ingestion jobs, financial calculations, and agent
tools use that owner. Next.js calls FastAPI and does not use Prisma or query the
application database directly. Authentication-provider tables may remain owned by
their provider; they cannot become a second owner of financial or workspace data.

One migration chain prevents Python and TypeScript tools from making conflicting
schema changes. Generated API types can provide frontend type safety without giving
the frontend direct database access.

## Historical change history

Keep the latest accepted value as the default, with a change history available
from the figure. Preserve every observed revision, not just the first and latest.
For each change show:

- Company, metric, financial period, unit/currency, and reporting scope.
- Previous and replacement values, with the numerical difference when comparable.
- Both source filings or document revisions and their exact source locations.
- Source publication/change time when known, when we detected the change, and
  when the replacement became active in Market Screener.
- The source's explanation, when available; otherwise say the reason is unknown.

Distinguish company corrections from regulator/source changes, our extraction
fixes, and changes to calculation rules. Do not label a parser fix as a company
restatement. Record withdrawals and invalidations even without a replacement
number. A unit or scope change must not be presented as a simple numerical change
unless the comparison is valid.

Example: 2024 profit was reported as 100 million, then corrected to 90 million
in a later filing. The current view shows 90 million. Its history shows
100 → 90 million, a decrease of 10 million, and links to both reports.

Show any disclosed adjustment components when the filing provides them. Do not
invent a reconciliation or infer a company's reason from the numerical difference.
Keep dependent ratio changes traceable to the changed inputs.

History coverage must be honest: we can show revisions captured by the system or
recovered from available source documents. If a source overwrote a number before
we captured it and no earlier copy is available, mark that gap; do not invent the
old value or claim a complete edit history.

## Freshness constraint and proposed interpretation

The owner's requirement is latest adjusted data, never silently stale data.
Literal zero delay is impossible: sources publish asynchronously, jobs take
time, and networks fail. A refresh interval cannot guarantee instant freshness.

**Proposal requiring an owner decision:** define a measurable freshness target
per source, show the last successful source check and financial period, mark
known pending updates, and withhold affected current ratios once their inputs
are known unreliable. Historical values may remain accessible with explicit
status. Decide whether merely old, but not known wrong, values stay visible
during an outage. A timestamp alone must not be called a freshness guarantee.

Two requests made across a publication boundary may use different revisions.
Consistency means the same revision gives the same official data. Default live
views must discover new revisions and move to them together within the agreed
freshness target; they cannot remain on a pinned revision indefinitely.

## Source lifecycle facts

An amendment can be a separate filing. A later regular report can also revise an
older comparative period; not every correction requires an amended old report.
For example, [this SEC-filed report](https://www.sec.gov/Archives/edgar/data/1819395/000175392624001844/g084515_ars.pdf)
discloses non-reliance on earlier statements without planning to amend those
earlier reports. Thus ingestion must inspect historical periods in new reports.

SEC staff can correct or remove accepted submissions. Previous daily indexes
do not capture all subsequent removals; rebuilt full/quarterly indexes incorporate
them. [SEC access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

EDINET exposes withdrawal, document-information editing, and disclosure status.
Historical list entries can change. Handle these source states explicitly.
[EDINET API specification, sections 3-1-3 through 3-1-7](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf)

## Proposed ingestion rule

Discover incrementally, reconcile source metadata periodically, and download new
or changed document revisions. Reuse unchanged verified objects. Do not reload
the entire universe daily. Do not interpret a stored document ID as a permanent
ban on checking or fetching a corrected version.

S3 objects are immutable captured revisions. The external source is not assumed
immutable. A changed source object receives a new content hash and revision;
its earlier bytes remain separate evidence, subject to withdrawal/access policy.

The implementation details are in [the architecture](../docs/production-architecture.md).

## Acceptance checks

- A new amendment revising a prior year's earnings updates that period and all
  dependent default ratios for every client after publication.
- A regular annual filing with revised comparative figures has the same effect.
- An amendment that changes only unrelated disclosure does not erase valid facts.
- A withdrawal/non-reliance event does not leave affected values labelled current.
- Reprocessing the same source revision creates no duplicate facts or downloads
  when the verified object is already present.
- A parser upgrade rebuilds from S3 without downloading unchanged filings.
- Two clients requesting the same revision receive the same shared values.
- Source outages and a known pending correction cannot masquerade as fresh data.
- A sequence of revisions, such as 100 → 90 → 95, remains inspectable with each
  source and activation time. Reprocessing a revision does not duplicate history.
- A parser correction is labelled as our extraction change, not a company revision.
- Unknown reasons, unavailable earlier versions, and incomparable bases are explicit.
