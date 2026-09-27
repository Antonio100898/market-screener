# Personal calculations and dashboard AI

Status: proposed implementation of mandatory product decisions P-06, P-07, and P-10.
The owner requires the capability; the first function set and edit-confirmation
policy still need an owner decision. See [personal research](../product/personal-research.md).

## Boundary

Official facts and shared standard metrics have one server-owned definition.
A workspace contains private formulas, screening rules, selected columns, sort
order, filters, and display settings. The AI edits this configuration. It does
not change official facts, install packages, or deploy generated application code.

Reuse the existing calculations as named starter definitions with their exact
semantics. A personal copy has its own identity and revision. Updating a starter
definition cannot silently change a user's saved copy.

## Workspace storage and research

Use PostgreSQL JSONB for versioned workspace configuration: formulas, rules,
columns, layout, and saved query settings. Keep owner ID, workspace ID, schema
revision, and edit revision as typed fields. Retain separate records for growing
chat history, notes, audit events, and calculation results rather than rewriting
one unbounded JSON document. Credentials do not belong in workspace JSON.

This extends the existing PostgreSQL/private-definition design; it does not add
a second database. The AI requests structured edits through server tools which
validate ownership, schema, and expected revision before saving.

The agent also supports read-only company search, ticker/security resolution,
fundamental inspection, source inspection, historical change inspection, and
ad hoc calculation previews. Return data revision, source references, units,
periods, and missing-input states with research results. Clarify ambiguous ticker
identity. An unsupported ticker or missing history is an explicit coverage gap,
not permission to insert AI-generated fundamentals.

Ad hoc research does not automatically create a permanent column. Save a rule,
note, or column when requested. External research tools can be added with separate
provenance and permissions; external claims remain distinct from official facts.
The source jobs alone own shared data writes.

## AI provider connections: investigation required

Keep the research tool contract independent of the model provider. Evaluate two
funding paths: application-managed access and a user-connected provider. Both
would use the same workspace ownership checks and deterministic calculator.

Before choosing providers, verify available API/authentication methods, whether
account authorization is supported for this application, billing responsibility,
data handling, and supported tool interactions. A chat subscription, API key,
and third-party account connection are different possibilities; do not promise
they are interchangeable. OpenAI, Anthropic, and other providers are candidates,
not implemented or guaranteed integrations.

Store any provider secret separately with restricted server access; workspace JSON
holds only a connection reference. Never expose secrets to the model, browser
responses, or logs. Define revocation, usage limits, and clear billing ownership.
Switching provider must not silently change saved formula semantics or send private
research to a different provider without the user's choice.

## User flow

Example: “Add a column showing price divided by average annual EPS over three
completed years.” The AI resolves the relevant fields and periods, asks about
material ambiguity, and creates a structured edit. It displays a plain-language
definition and preview values with source periods and missing-input states.

The service validates the edit and evaluates it using one official data release.
An accepted edit creates an immutable definition revision and updates the workspace
in one transaction. Store the request, structured change, validation result,
definition version, and actor. Provide undo by selecting or creating a prior-equivalent
revision, retaining the history.

**Open choice:** clear requests can either apply after validation with undo, or
always wait for explicit preview acceptance. Do not hardcode this product choice.
Ambiguous requests cannot be executed on guessed financial meaning.

Deleting a column only hides its presentation. Deleting a calculation checks its
dependents; refuse an unresolved deletion or offer an explicit dependent edit.
Use an expected workspace revision to reject conflicting simultaneous edits.

## Bounded formula contract

Propose a small typed expression tree, stored as JSON, rather than arbitrary
executable code. Each definition records:

- Stable ID, owner, name, description, and immutable revision.
- Formula-language revision and operator tree.
- Referenced fields/definitions and fiscal-period selectors.
- Output type, unit/currency rules, missing-input policy, and display format.
- Assumptions, creation actor/time, and validation state.

Initial proposed operators: arithmetic, comparisons, Boolean combinations,
conditions, explicit fiscal-period selection, and bounded historical aggregates.
Exclude arbitrary code, SQL, imports, file/network access, loops, and recursion.
An unsupported request produces a clear limitation, not a guessed equivalent.
Cross-company rankings or portfolio functions need explicit operators and scope;
they are not silently substituted for a per-company calculation.

The field catalog owns meaning: annual versus TTM EPS, basic versus diluted,
statement currency, quote currency, per-share basis, receipt ratio, and availability.
The AI uses that catalog, not display labels, to identify fields. Corporate-action
and FX handling must remain explicit and coherent with the selected evidence.

Validation rejects unknown references, incompatible units/currencies, cycles,
excessive expression size, unbounded history, and unsupported language revisions.
Evaluation returns a value or a typed reason such as `MISSING_INPUT`,
`ZERO_DENOMINATOR`, `BASIS_MISMATCH`, `INPUT_INVALIDATED`, or `LIMIT_EXCEEDED`.
Missing evidence propagates by default. Any permitted zero assumption is explicit,
private, and disclosed; it cannot alter a shared strict grade.

Preserve separate formatting and arithmetic. A displayed rounded value is not an
input to the next formula. A ratio's meaning is defined by its inputs, not its name.

## Server execution and filtering

The deterministic evaluator owns previews, list/detail results, and exports.
React renders the returned values and column schema without reimplementing formulas.
The AI is used when editing; normal refreshes run the stored formula without asking
an AI to recalculate financial values.

Calculate over the full eligible universe before applying filters, counts, sorting,
and pagination that depend on personal fields. Calculating only the visible page
would return the wrong screen. Safe shared-only filters may reduce work when doing
so cannot change the semantics of the requested operation.

Start with bounded server evaluation over stored normalized inputs. Cache results
by owner/workspace, official release, definition/dependency revisions, language,
and evaluator revision. Materialize expensive runs with `PENDING`, `READY`, or
`FAILED` states and limits on time, memory, rows, and operators. Never paginate a
partially computed result as if it were the full screen.

Official data publication does not wait for every user's formulas. A user's result
must match the displayed official release. While a new result is computing, show
pending/unavailable rather than label an older result current. Invalidate caches
when inputs, definitions, or engine semantics change.

## Isolation and agent tools

Authentication supplies owner identity; client-provided owner IDs do not grant
access. Scope workspace reads, writes, AI context, result caches, and job status.
Use narrow agent tools for company/fundamental/evidence/history lookup, reading
this workspace, validating edits, previewing calculations, and applying authorized
workspace changes. The permission to research shared data is read-only.

Treat filings, notes, and chat attachments as data, not permission to execute
instructions embedded within them. The agent has no raw database or filesystem
tool and no ability to change source facts. Bound token usage and formula work per
user so a single request cannot block other clients or source ingestion.

Private AI transcripts and audit records need a defined retention policy. Deleting
a personal rule does not delete shared evidence. Shared/public formula publishing
is outside the first proposal unless the owner requests it.

## Version changes

Additive field/operator support can preserve existing definitions. A changed
operator meaning needs a new language/evaluator revision and an explicit migration.
Saved definitions never silently acquire a new interpretation. Evaluate migrated
definitions against known-answer fixtures and show relevant changed results.

The API contract, application build, official release, and personal definition
revision remain independent. A user adding a column does not require a frontend
deployment because the renderer consumes a validated column schema.

## Completion evidence

Verify an AI-created column, an edited formula, a removed column, a dependency-aware
formula removal, and undo through the dashboard. Test two users, two concurrent
sessions, a source correction, unsupported formulas, hostile instructions in
source text, missing/zero inputs, mixed currencies, and full-universe sorting.
Test legacy default formulas against the pinned current UI payload. Record exact
function coverage and owner-approved gaps; scaffolding alone does not satisfy P-06.
