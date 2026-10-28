# Data issues and decisions

Source, licensing and comparability decisions, with stable IDs. Technical failures for a
particular run are in the `ops.reject` and `ops.quality_result` tables, not here.

Each entry records what was observed, what was decided and what would change the decision.
Add new entries at the end; never reuse an ID.

## DEC-001 Stream licences are checked per item

- **Observed (27 October 2026):** most Stream APR items state CC BY 4.0 in their licence text.
  The Southern Water 2024-25 item (`ccd2a005a25e457caa6b85b929ebe9c3`) states "N/A", and a
  South Staffs Water 2024-25 CSV item (`0690221de5774c33bab091608c03463a`) states no licence.
- **Decision:** every retrieval reads the item's licence text and ingests the item only if it
  links to or names CC BY 4.0, or the item records a `licence_override` (DEC-009). The South
  Staffs CSV item is on hold; South Staffs 2024-25 is covered by its CC BY 4.0 Feature Service
  item. The Southern Water item is loaded under DEC-009.
- **Changes if:** Southern Water republishes 2024-25 under an open licence.

## DEC-002 Company APR workbooks are on hold

- **Observed:** company APR workbooks are publicly downloadable, but none registered here
  carries a recorded reuse licence. On 27 October 2026 the six Thames Water workbooks (2020-21
  to 2025-26) contained no licence statement, and the Thames Water website terms say "All such
  rights are reserved" and that content may not be stored in an electronic retrieval system
  without Thames Water's prior written consent.
- **Decision:** company workbooks are not loaded unless their reuse terms are recorded, with one
  exception: the Thames Water workbooks, loaded by operator decision (DEC-009). Stream items are
  used where they exist.
- **Changes if:** a company states open reuse terms for its workbook, or gives written consent;
  record the terms in `sources.yml` and replace the override with a `licence_verification` route.

## DEC-003 Ofwat landing pages are recorded, not fetched

- **Observed:** Ofwat landing pages answer automated clients with HTTP 403 and a browser check.
  Asset URLs under `/wp-content/uploads/` download normally.
- **Decision:** the registry records each landing page for provenance and fetches only the
  asset URL. If an asset also starts returning a browser check, the run reports it as blocked
  and prints the manual bootstrap command. Nothing tries to get past the check.

## DEC-004 PR24, PR19 and RAG assets need a licence statement in the asset

- **Observed:** Ofwat publications are generally released under the Open Government Licence
  v3.0, but the landing pages for these assets could not be read automatically.
- **Decision:** `OFWAT_PR24_OUTCOMES`, `OFWAT_PR19_ODI` and `RAG_DEFINITIONS` are `conditional`.
  Each asset version is used only when an Open Government Licence v3.0 statement is found inside
  the asset (recorded in `silver.licence_evidence` with its location), or when an operator
  supplies it through manual bootstrap with the landing-page licence recorded.

## DEC-005 The 2024-25 WCPR CSV moved

- **Observed (27 October 2026):** the 2024-25 WCPR normalised CSV URL returned HTTP 404. The
  post-final-determination update workbook at the same location downloads normally.
- **Decision:** the CSV asset is on hold with this reason; the post-final-determination
  workbook is the 2024-25 release.

## DEC-006 South West Water reporting areas

- **Observed:** South West Water publishes Stream items for its South West area (`SWB` in the
  data), its Bristol area (`BRL`) and the whole company (`SBB`).
- **Decision:** all three map to company `SWB` with `reporting_area` set. Only `whole_company`
  rows can be ranked against other companies.

## DEC-007 Welsh Water 2023-24 company code

- **Observed:** the Dŵr Cymru 2023-24 Stream item uses company code `DCWW` and year `2023/24`.
- **Decision:** the item register maps it to `WSH`; the parser checks the code in every record
  against the registered source code.

## DEC-008 Scotland and Northern Ireland

- **Decision:** `WICS_RETURN` and `NI_AIR` are on hold until separate adapters exist. Ofwat
  parsers never read them and Genie must say these jurisdictions are not covered.

## DEC-009 Two datasets are loaded without an open licence

- **Observed (27 October 2026):** Thames Water has no Stream APR item, and its own APR workbooks
  state no licence; its website terms reserve all rights and require prior written consent before
  content is stored in an electronic retrieval system. The Southern Water 2024-25 Stream item's
  licence is "N/A" (DEC-001).
- **Decision:** the operator chose to load both, so that Thames Water finance (including
  non-appointed profit) and Southern Water 2024-25 are available. The registry records each as an
  operator licence override: `licence_verification: operator_override` on the six Thames assets
  (source licence `NO-OPEN-LICENCE`) and `licence_override` on the Southern item. Every version
  loaded this way records `licence_id = 'NO-OPEN-LICENCE'` and the reason in `licence_evidence`,
  and Gold shows that licence on each value.
- **Changes if:** Thames Water gives consent or states open terms, or Southern Water relicenses
  the item as CC BY 4.0 (then remove the override), or the operator withdraws the decision (set
  the assets or item to `hold`).
