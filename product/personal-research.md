# Personal research

## Accepted owner decisions

- **P-06:** Every user can ask the AI inside their dashboard to create, edit, or
  remove calculations, ratios, rules, and displayed columns. This capability is
  mandatory, not an optional future extension.
- **P-07:** Personal formulas and presentation are separate from shared official
  data. Different users may reach different calculated results from the same facts.
- **P-10:** Each user has a persistent private workspace. Its AI can search and
  inspect fundamentals for any supported ticker, run calculations, and research
  with the user. Shared fundamentals are updated only by ingestion/derivation
  jobs; agent edits are limited to the user's private workspace.

Graham rules and the owner's current formulas are existing research choices.
They must not become the only allowed research method. Preserve their existing
behavior during migration and make them identifiable calculation definitions.

## Required outcomes

- A user can express a research change in natural language and see which formula,
  assumptions, units, and periods the resulting column uses.
- Changes survive a new browser, another machine, and a service restart.
- A user's change affects their workspace, not another user's rules or the
  shared official fact set.
- Updated official facts flow into personal calculations. A saved formula must
  not freeze old financial inputs unless explicitly saved as historical research.
- Missing inputs, unsupported requests, and calculation failures are visible.
  AI must not invent reported facts to make a formula work.
- Removing a column and removing a calculation are distinct actions. A formula
  used by other columns cannot disappear without resolving those dependencies.

## Return Quality

**P-11:** Include the median annual FCF/revenue over the latest ten fiscal years
in Return Quality. Higher positive FCF margins increase the score when the other
inputs stay fixed. P-11 originally gave it equal weight with ROE, ROIC, and
RONTA. P-16 supersedes only that weighting: FCF/revenue now weighs 22.5%, while
ROE, ROIC, and RONTA each weigh 15.83%. The inclusion and definition remain.

The calculation uses annual total free cash flow (operating cash flow less cash
CapEx, as shown in the detail cash table) divided by same-year revenue, expressed
as a percentage. Its window ends at the cash table's latest completed fiscal
year. The UI shows the median and valid year count; missing pairs and non-positive
revenue are excluded without reaching into older years or substituting zero.

**P-16:** Every ticker receives a numeric score from 0 to 100. For each available
ROE, ROIC, RONTA, FCF/revenue, CapEx/OCF and debt/equity are percentile-ranked
against companies with the same usable input. ROE, ROIC and RONTA each weigh
15.83%; FCF/revenue weighs 22.5%; CapEx/OCF weighs 20%; debt/equity weighs 10%,
or 20% when D/E is at least 1. Its added 10% comes from ROE alone. Lower
CapEx/OCF and debt/equity rank higher.

Missing inputs remain missing and are omitted from the average. A return whose
operating-income numerator was assumed absent is unavailable, not an observed
zero. Other disclosed estimates retain their assumption labels. Missing OCF or FCF
makes Return Quality 0, even if other return inputs exist. Show available-input count
and the missing inputs beside the score.

Divide by `1 + min(debt/equity, 1)` when a nonnegative ratio is available. Debt
can therefore reduce the score by at most 50%. Missing or invalid leverage is
explicitly unassessed, never displayed as debt-free.
Investigate extraction bugs before accepting missing inputs as genuine gaps.
Shared facts, strict Graham grades and reported figures are unchanged by this
scoring convention. This replaces the earlier positive-only harmonic mean and
the minimum-input requirement.

**P-13:** Divide the resulting score by `1 + median(Total CapEx / OCF)` over
the same ten fiscal-year slots. A median of 50% divides the score by 1.5.
Use each year's Total CapEx magnitude and operating cash flow from the detail
table. Total CapEx can include accrual-based investment; disclose that basis
in the column tooltip. This is not a cash-only CapEx measure. Exclude missing
pairs and non-positive OCF and show the valid year count. Under P-16, no usable
pair means the penalty is explicitly unassessed; it does not remove the score.
An actual zero CapEx median leaves the score unchanged. The FCF/revenue
contribution and debt adjustment remain.

## Proposed implementation, not owner decisions

**P-15 — accepted model constraint:** The product agent must not use an OpenAI
model. Evaluate GLM, DeepSeek, Qwen, Llama, or another suitable alternative.
Select for reliable task completion and total cost. Model, provider, and whether
to self-host remain separate decisions; no GPU hosting is implied.

Store workspace configuration as JSON in PostgreSQL JSONB, with ownership and
revision keys in typed columns. This is the recommended first approach; MongoDB
and a separate database are not required merely because the configuration is JSON.
The agent edits through validated application tools, not direct database access.
Research notes and saved calculations remain private and distinct from official facts.

Use a bounded formula language and a standard column renderer. The AI produces
structured edits which a deterministic service validates and evaluates. It does
not need permission to execute Python, SQL, or JavaScript supplied by a user.

Keep immutable formula revisions, an audit record, and undo. Initially support
per-company formulas over an explicit set of facts, periods, and standard metrics;
unsupported functions require a clear response, not improvised execution.
The owner must approve the first supported function set and edit-confirmation UX.

Evaluate service-funded AI and user-supplied provider connections separately.
Provider choice must not change workspace ownership or calculation meaning.
Non-OpenAI provider connections require verification of supported
authentication, terms, and billing. Do not assume a consumer subscription can
fund third-party application API calls. No provider integration is promised yet.

Detailed contract: [personal calculations](../docs/personal-calculations.md).

## Acceptance checks

- The agent can find a supported ticker, inspect fundamentals and revision history,
  and return a calculated result with its inputs and sources, without writing to
  shared official data or another user's workspace.
- Users A and B define different earnings multiples over the same official facts
  without changing each other's results or columns.
- An AI edit previews the exact formula and its missing-data behavior.
- A correction to an input changes dependent personal results under the same
  definition revision, with the new official data revision recorded.
- Filtering and sorting use the user's formula over the full eligible universe,
  not just the current page.
- Conflicting edits from two sessions cannot silently overwrite one another.
- An unsupported or hostile formula cannot read secrets, issue network requests,
  mutate official facts, or access another user's workspace.
