# Public data source register

Research status: **27 October 2026**. This register is for a public-data-only UK water
performance Genie. A source being publicly viewable is not enough: every production source also
needs a clear reuse licence, a stable retrieval route and enough metadata to reproduce a number.

The most important discovery is [Stream](https://www.streamwaterdata.co.uk/pages/annual-performance-reports).
From 2023-24 it publishes one standardised, machine-readable APR dataset per participating company.
The datasets are public, expose ArcGIS REST APIs and are individually licensed CC BY 4.0. This is a
better reusable sector source than the "industry datashare" workbooks that circulate between
companies without a public landing page.

## Priority legend

| Priority | Meaning |
|---|---|
| P0 | Required for the first useful England-and-Wales release |
| P1 | High-value extension after the core performance model works |
| P2 | Context or a separate national adapter; do not block the first release |
| Hold | Public access or reuse has not yet been proved; do not ship it |

## Core source register

| ID | Priority | Public source | Coverage and format | Questions enabled | Access, licence and ingestion notes |
|---|---:|---|---|---|---|
| `STREAM_APR` | P0 | [Stream Annual Performance Reports](https://www.streamwaterdata.co.uk/pages/annual-performance-reports) | Participating England and Wales companies; standardised CSV/table datasets from 2023-24 onward, with older APR documents linked | Revenue, costs, debt, capex, leakage, interruptions, flooding, emissions, headcount and other APR-table comparisons | Individual datasets expose download and ArcGIS FeatureServer APIs. Verified on the United Utilities 2024-25 item: 10,515 records, public, CC BY 4.0, with publisher/data update dates. The licence is set per item, not per platform: on 27 October 2026 the Southern Water 2024-25 item showed a custom licence of "N/A" and one South Staffs 2024-25 CSV item showed no licence, so each item is checked at retrieval. Coverage is not complete for every company/year (Thames Water has no Stream APR item), so keep `COMPANY_APR` as a fallback. |
| `COMPANY_APR` | P0 | Each company's APR tables; starting links are in [Company APR discovery links](#company-apr-discovery-links) | Company-specific Excel/CSV regulatory tables; years vary | The configured company's detailed history and any company/year missing from Stream | Publicly downloadable is not automatically openly licensed. Record the terms shown by each publisher. Prefer a Stream copy where one exists. Never ingest sheets marked for Ofwat only, debt-instrument registers, personal data or other restricted content. The `company_apr_v1` parser reads Ofwat's APR pro forma tables by content and skips debt-instrument registers. The Thames Water workbooks are registered on hold: they state no licence, and the Thames Water website terms reserve all rights. |
| `OFWAT_WCPR` | P0 | Ofwat Water Company Performance Report open data: [2024-25 report workbook](https://www.ofwat.gov.uk/wp-content/uploads/2025/10/WCPR-data-report-2024-25-external-V0.2-Final.xlsx), [2024-25 normalised workbook](https://www.ofwat.gov.uk/wp-content/uploads/2025/10/WCPR_2024-25-Open-Data-Machine-Readable-Normalised-PostFDUpdate.xlsx), [2023-24 report workbook](https://www.ofwat.gov.uk/wp-content/uploads/2024/10/WCPR-data-report-2023-24.xlsx), [2023-24 normalised CSV](https://www.ofwat.gov.uk/wp-content/uploads/2024/10/WCPR_2023-24-Open-Data-Machine-Readable-Normalised.csv) | Ofwat's comparable company-level performance and expenditure measures. Current machine-readable releases found for 2023-24 and 2024-25; use the report archive for earlier years | Peer rank, trend, target/outturn, expenditure and sector position on comparable KPIs | Ofwat explicitly states that data on the WCPR data pages is released under OGL v3. Prefer the normalised asset over charts or PDFs. Preserve the release/version because Ofwat can publish post-determination updates. |
| `OFWAT_PR24_OUTCOMES` | P0 | [PR24 final determination models](https://www.ofwat.gov.uk/regulated-companies/price-review/2024-price-review/final-determinations-models/) and [performance commitment definitions](https://www.ofwat.gov.uk/regulated-companies/price-review/2024-price-review/pr24-final-determinations-performance-commitment-definitions/) | 2025-30 common and bespoke performance commitments, PCLs, ODI rates, deadbands/caps/collars and company outcomes data; Excel models plus definition documents | "What is our target?", "what is the reward/penalty exposure?", "how do AMP8 targets compare?" and forecast bill questions | Ingest Key Dataset 1 (Outcomes data), the PCL models and definitions as distinct sources. Ofwat's browser check may block automated discovery; download assets through a recorded manual bootstrap if needed, then hash and archive them. Ofwat publications generally carry OGL v3; retain any third-party-rights exceptions stated on the asset. |
| `OFWAT_PR19_ODI` | P0 | [Ofwat in-period ODI determinations](https://www.ofwat.gov.uk/regulated-companies/price-review/in-period-odi-determinations/) and [2024-25 final ODI determinations](https://www.ofwat.gov.uk/regulated-companies/price-review/2024-price-review/blind-year-reconciliation-for-2024-25/odi-final-determinations-for-2024-25/) | 2020-25 performance commitments and final reward/penalty decisions; public pages, models and company determinations | "Did we meet the AMP7 target?", annual/five-year ODI position and reconciliation questions | Ofwat publishes a consolidated `Outcome-performance-commitments-and-ODIs-data_2020-25v0.2.xlsx` at `https://www.ofwat.gov.uk/wp-content/uploads/2026/03/`, apparently from the in-period ODI determinations page ("Final view of actual performance for 2020–25"). The landing page blocks automated reading, but the workbook's own Licence sheet states the Open Government Licence, and the pipeline checks that statement for every version it loads (`conditional` status). Retrieve the publisher's copy only; never use a copy obtained elsewhere. |
| `RAG_DEFINITIONS` | P0 | [Ofwat RAG 4 APR table definitions](https://www.ofwat.gov.uk/publication/rag-4-14-guideline-for-the-table-definitions-in-the-annual-performance-report/) | Definition and reporting rules for APR line references, units and table semantics | Provenance explanations, metric definitions and safe cross-company comparison | Treat as reference metadata, not a numeric fact source. Version the guideline because definitions and table layouts change. Ofwat publication/OGL terms apply subject to stated exceptions. |
| `EA_EPA` | P1 | [Environment Agency environmental performance report 2024](https://www.gov.uk/government/publications/water-and-sewerage-companies-in-england-environmental-performance-report-2024/water-and-sewerage-companies-in-england-environmental-performance-report-for-2024) and the linked company data/EPA results | Nine English water and sewerage companies; EPA results and company reports from 2011 onward; HTML tables and linked data | EPA stars, pollution incidents, permit compliance, self-reporting, WINEP delivery, sludge and supply-demand balance | OGL v3 is shown on GOV.UK. The 2024 page links historical EPA results, star ratings and company-specific reports. This source excludes water-only companies and is calendar-year based for several measures; do not silently join it to financial-year APR facts. |
| `EA_EDM` | P1 | [Event Duration Monitoring: storm overflows annual returns](https://www.data.gov.uk/dataset/19f6064d-7356-466f-844e-d20ea10ae9fd/event-duration-monitoring-storm-overflows-annual-returns) | Site-level English storm overflow counts, durations and monitor operation; annual datasets from 2020 onward | Which sites spill most, company totals, duration trends and monitor reliability | OGL/public-sector data via data.gov.uk; confirm the licence on each dataset release. Store the asset/site identifier, company, receiving water, reporting calendar year and release version. Weather affects spill counts, so avoid causal claims without context. |
| `DWI_COMPANY` | P1 | [DWI company performance data](https://www.dwi.gov.uk/what-we-do/annual-report/drinking-water-2025/company-performance-data/) and [DWI annual report archive](https://www.dwi.gov.uk/what-we-do/annual-report/) | England and Wales company size and annual drinking-water compliance at treatment works, reservoirs and supply zones; currently presented through Power BI; annual | Compliance with drinking-water standards, sample failures, events and peer comparisons | DWI states annual updates and shows OGL v3. A stable supported bulk download/API must be identified before automated ingestion; until then use this as a cited document/dashboard source, not a scraped table. DWI's raw-water dataset is a possible later, much larger analytical source, not a v1 company KPI source. |
| `CCW_COMPLAINTS` | P1 | [CCW quarterly complaint performance](https://www.ccw.org.uk/advice-and-support/complaint-performance/), [annual household complaint reports](https://www.ccw.org.uk/publication/household-complaint-handling-report-2025/) and [Water Mark](https://www.ccw.org.uk/publication/water-mark-2025/) | England and Wales household complaints; quarterly HTML tables and annual reports/Excel | Complaint volumes and rates per 10,000 connections, complaints escalated to CCW, trend and customer-experience context | Quarterly values are explicitly unaudited and may be revised; annual audited results should replace them when released. Capture publication/retrieval dates. Confirm CCW reuse terms before redistributing extracted tables; public display alone is not a licence. |
| `COMPANY_BOUNDARIES` | P1 | [Stream water company boundaries](https://www.streamwaterdata.co.uk/datasets/ef5f5968a9ee449d8373baf8be7237ea_0/explore) | Public spatial supply and wastewater service areas based on Ofwat geographical boundary data | "Which company serves this place?", maps, and geographic filtering | Use the dataset's API/download and its stated licence. Keep water-supply and wastewater boundaries distinct; a postcode can have different suppliers. Boundaries are context, not evidence that an incident or asset belongs to a company. |
| `WICS_RETURN` | P2 | [WICS Annual Return and Regulatory Accounts](https://wics.scot/publications/scottish-water/annual-return-regulatory-accounts) and [Scottish Water performance report 2024-25](https://wics.scot/publications/scottish-water/performance/scottish-water-performance-report-2024-25) | Scottish Water regulatory return tables from 2002-03 onward and regulator performance reports | Scottish Water finance, service, investment, leakage and quality questions | Scotland is not governed by Ofwat APR/PC schemas. Build a separate WICS adapter and map only genuinely equivalent metrics into the canonical model. Verify reuse terms per asset. |
| `NI_AIR` | P2 | [Utility Regulator annual information reporting 2024](https://www.uregni.gov.uk/publications/annual-information-reporting-2024-0), [NI Water publications](https://www.niwater.com/about-us/our-publications/) and [OpenDataNI/Data.gov.uk search](https://www.data.gov.uk/search?q=NI+Water+Annual+Information+Return) | Northern Ireland Water Annual Information Returns, cost/performance reports and annual accounts | NI Water service, cost, investment, leakage, quality and target questions | Northern Ireland has a separate price-control and AIR schema. Build a separate adapter. Prefer the OpenDataNI asset and its explicit licence over extracting the very large public PDF. |

## High-value context, not first-release fact tables

These sources answer useful questions but require additional semantic or spatial work. They should
not delay a reliable company-performance core.

| Candidate | Use | Why it is not P0/P1 structured performance data |
|---|---|---|
| [Discover Water](https://www.discoverwater.co.uk/) | Public-friendly cross-check of bills, service, quality and environment measures | It is a derived presentation layer. Use the original Ofwat/EA/DWI/CCW/Stream facts for numeric answers. |
| Company WRMPs and annual reviews | Long-term supply-demand balance, leakage, PCC, metering, drought resilience and delivery | Published separately by each company, often as PDF/Excel with inconsistent layouts. Add a document source and a curated structured extract later. |
| Company DWMPs | Sewerage capacity, flooding and long-term drainage plans | Long-horizon plan data, not observed annual performance; company formats vary. |
| [EA Water Quality Explorer](https://environment.data.gov.uk/water-quality/view/landing) | Ambient river/coastal sampling near a company area | Measurements are not automatically attributable to a water company. Requires catchment/assets and careful causal wording. |
| [EA Hydrology API](https://environment.data.gov.uk/hydrology/doc/reference) | Rainfall, river flow and level context for drought, leakage and storm-overflow analysis | Contextual time series; spatial alignment and weather-normalisation rules are needed. |
| WINEP programme data and EA delivery reports | Environmental scheme commitments and delivery | Scheme-level releases and reporting periods need a dedicated model; EPA already supplies a useful company-level view. |
| Ofwat/EA/DWI enforcement decisions | Regulatory investigations, penalties and undertakings | Event/document data, not a stable KPI series. Index as dated documents with regulator, company, case and outcome. |
| Company annual report/APR narrative PDFs | Management explanation, risks and assurance | Use for cited narrative only. Numeric answers must come from governed structured tables. |

## Sources on hold

| Candidate | Status | Decision |
|---|---|---|
| "APR Industry Datashare" workbooks | Copies circulate for 2021-22 to 2025-26, but no public publisher page or reuse licence has been established. They may have been supplied through an industry-only share. | **Do not redistribute or make them a dependency.** Replace with Stream + Ofwat WCPR open data. If a public Stream/Ofwat landing page for the same exact file is found, record it and its licence before use. |
| Copies of `Outcome performance commitments and ODIs data_2020-25v0.2.xlsx` obtained elsewhere | Ofwat now publishes this workbook itself (see `OFWAT_PR19_ODI`), and its Licence sheet states the Open Government Licence. Copies from other places have no verified provenance. | **Use only the publisher's copy**, retrieved from Ofwat through `OFWAT_PR19_ODI`. Never ingest a copy from another location. |
| DWI embedded Power BI extraction | Public dashboard and OGL page are verified, but a stable documented bulk endpoint has not been verified. | Cite/view now; automate only after DWI supplies or documents an export/API. |
| CCW HTML/Excel data | Public and valuable, but the reuse licence for extracted tables has not yet been confirmed. | Keep out of redistributed data packages until terms are confirmed; linking/citing is fine. |

## Questions the source model should support

| Question family | Examples | Primary sources |
|---|---|---|
| Own performance | "How are we performing?" "Which targets did we miss?" | `OFWAT_WCPR`, `STREAM_APR`/`COMPANY_APR`, `OFWAT_PR24_OUTCOMES` |
| Peer comparison | "Rank us on leakage and interruptions." "Who improved most?" | `OFWAT_WCPR`; `STREAM_APR` only where line, unit, service type and period are comparable |
| Targets and incentives | "What is our PCL?" "What ODI penalty did we incur?" | `OFWAT_PR24_OUTCOMES`, `OFWAT_PR19_ODI` |
| Finance and delivery | "How have revenue, totex, debt and capex changed?" | `STREAM_APR`, `COMPANY_APR`, `RAG_DEFINITIONS` |
| Operational service | "What happened to leakage, mains repairs, supply interruptions or sewer flooding?" | `OFWAT_WCPR`, APR tables |
| Environment | "What is our EPA rating?" "Which overflows spill most?" | `EA_EPA`, `EA_EDM` |
| Drinking water | "How compliant was drinking water?" "How many events/failures?" | `DWI_COMPANY` |
| Customers | "Are complaints rising?" "How do customers rate us?" | `CCW_COMPLAINTS`, Ofwat C-MeX/BR-MeX data |
| Geography | "Which company serves this area?" | `COMPANY_BOUNDARIES` |
| Plans and resilience | "Will supply meet demand?" "Are WINEP/WRMP schemes on track?" | WRMP/WINEP documents and curated extracts; EPA company-level delivery |
| Provenance | "Where exactly did that figure come from?" | Every fact table's source/version/record fields; `RAG_DEFINITIONS` for meaning |

## Required provenance contract

Every ingested asset should create an immutable source manifest record with at least:

- `source_id`, publisher, landing-page URL, exact asset/API URL and stated licence;
- reporting period, publication date, source version and retrieval timestamp;
- byte size and SHA-256 for downloaded files, or ArcGIS item/service ID plus service update time for API data;
- parser version, row count and load status;
- source locator on every fact: file + sheet + cell/line reference, API item + object/record ID,
  HTML table + row, or document + page/table;
- original metric label, unit, price base, service type, period basis and any restatement flag;
- `is_comparable` and the explicit crosswalk rule used for a peer comparison.

Keep financial years (`2024-25`, ending 2025) separate from calendar years (`2024`). Keep PR19/AMP7
and PR24/AMP8 definitions separate even where labels look similar. A restated value must not silently
overwrite the originally published value.

## Company APR discovery links

Use Stream first. These official company assets are fallbacks, historical sources and useful test
fixtures. URLs can move; the production manifest should store both the landing page and exact asset.

### Yorkshire Water

- [2025-26 APR tables](https://www.yorkshirewater.com/media/qmehdlaa/apr-data-tables-2025-2026-1.xlsx)
- [2024-25 APR tables](https://www.yorkshirewater.com/media/n33llnvc/2024-25-apr-tables-excluding-tables-3a-3i.xlsx)
- [2023-24 APR tables](https://www.yorkshirewater.com/media/q4tcodxh/2023-24-annual-performance-report-data-tables-excluding-table-3s.xlsx)
- [2022-23 APR tables](https://www.yorkshirewater.com/media/1npliecf/2022-2023-landing-page.xlsx)
- [2021-22 APR tables](https://www.yorkshirewater.com/media/1atdzjtm/copy-of-2022-apr-2021-2022-excel-tables-excluding-3a-i-1.xlsx)
- [2020-21 APR tables](https://www.yorkshirewater.com/media/y00b1clx/2021-apr-2021-2022-excel-tables-excluding-3a-i.xlsx)
- [2019-20 APR tables](https://www.yorkshirewater.com/media/udxdgeu1/yky-apr-2019-20-tables-version-20-feb21.xlsx)

### Thames Water

- [2025-26 APR tables](https://www.thameswater.co.uk/media-library/dvrggetx/2025-26-apr-tables.xlsx)
- [2024-25 APR tables](https://www.thameswater.co.uk/media-library/5s0gwt45/2024-25-annual-performance-report-tables-excluding-tables-3a-3i.xlsx)
- [2023-24 APR tables](https://www.thameswater.co.uk/media-library/bglculfn/2023-24-annual-performance-report-tables-excluding-tables-3a-3i-jan-25-updates.xlsx)
- [2022-23 APR tables](https://www.thameswater.co.uk/media-library/xvih3h42/2022-23-annual-performance-report-tables-excluding-tables-3a3iv11-updated-jan-2024.xlsx)
- [2021-22 APR tables](https://www.thameswater.co.uk/media-library/srkhsr2a/2021-22-apr-tables-excluding-tables-3a-3i.xlsx)
- [2020-21 APR tables](https://www.thameswater.co.uk/media-library/e0gf4m3v/2020-21-apr-tables-excluding-3a-3i.xlsx)

### Anglian Water

- [2025-26 APR tables](https://www.anglianwater.co.uk/siteassets/household/corporate/plans-and-reports/2026-reports/annual-performance-report-2026-data-tables.xlsx)

### Dŵr Cymru Welsh Water

- [2025-26 APR tables](https://corporate.dwrcymru.com/-/media/project/files/page-documents/corporate/library/annual-performance-reports/2025-2026/1a-apr-excel-tables-v2.ashx)
- [2024-25 APR tables](https://corporate.dwrcymru.com/-/media/project/files/page-documents/corporate/library/annual-performance-reports/2024-2025/v2/apr-tables-parts-1-to-11-excluding-part-3-v7.ashx)
- [2023-24 APR tables](https://corporate.dwrcymru.com/-/media/project/files/page-documents/corporate/library/annual-performance-reports/2023-2024/apr--tables-parts-1-to-11-excluding-part-3-v7.ashx)
- [2022-23 APR tables](https://corporate.dwrcymru.com/-/media/project/files/page-documents/corporate/library/annual-performance-reports/2022-2023-v2/apr-tables-parts-1-to-11-excluding-part-3-v3.ashx)
- [2021-22 APR tables](https://corporate.dwrcymru.com/-/media/project/files/page-documents/corporate/library/annual-performance-reports/2021-2022/january-2023/apr-tables-202122-tables-excluding-tables-3a3i-v6-24-january-2023.ashx)
- [2020-21 APR tables](https://corporate.dwrcymru.com/-/media/project/files/page-documents/corporate/library/annual-performance-reports/2020-2021/annual-performance-report-2020-2021-tables-parts-1-to-9-excluding-part-3-v2.ashx)

### United Utilities

- [APR open-data landing page](https://www.unitedutilities.com/corporate/about-us/performance/annual-performance-report/APR-open-data/?page=1#page1)

United Utilities publishes its standardised APR data through Stream (items
`f49708822eca408480eedf255532613a` for 2023-24, `e0fbbbae41f24b29a362d3c73ef356ba` for 2024-25 and
`784c8906976d4b7c93099c8426ed0374` for 2025-26). No copies are kept in this repository. The three
items show schema drift: 2023-24 uses `Line_description`, 2024-25 uses `Line_Description`, and
2025-26 has no `Item_reference` field and separates attribute parts with ` | ` instead of ` - `.
Version and validate the Stream schema by reporting year; do not depend on one year's exact headers.

## Access notes

- Ofwat frequently presents a browser-verification page. Do not attempt to bypass it. A person can
  download a named public asset once, after which the pipeline can verify and archive it by checksum.
- Direct `wp-content` asset URLs can work when the Ofwat landing page is blocked, but the landing
  page and its OGL statement must still be recorded.
- Website paths are discovery aids, not durable identifiers. Prefer publisher dataset/item IDs and
  archived raw copies with checksums.
