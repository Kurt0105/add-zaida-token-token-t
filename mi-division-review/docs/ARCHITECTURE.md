# mi-division-review — Architecture & Build Contract

This file is the single source of truth for how the plugin fits together. Every
file in the plugin must agree with it. If code and this document disagree, fix
one of them in the same change.

## 1. Design principles (non-negotiable)

1. **Deterministic before intelligent.** Every number in every output comes from
   the Python engine (`engine/mi_engine`). Claude decides *layout* (which table
   is the P&L, which column is 2027 budget) and writes *narrative*; it never
   types, re-keys, plugs, or hand-computes financial figures.
2. **The language model is never the accounting system.** Extraction produces
   raw cells; a declarative mapping file turns cells into canonical records; the
   engine applies the mapping. Numbers keep a `source_ref` back to file/sheet/cell
   or file/page/table.
3. **Reconcile or stop.** Every analysis entry point calls
   `reconcile.require_reconciled()`. If reconciliation status is `FAIL`, the
   analysis raises `UnreconciledError` and nothing executive is produced —
   unless the caller passes `allow_unreconciled=True`, in which case every
   output carries `"stamp": "UNRECONCILED — NOT FOR DECISION"`.
4. **Sources are read-only.** Budget sources (local files and cloud documents)
   are never edited. A PreToolUse hook enforces this.
5. **Placeholders are labelled.** Hurdle rates and tolerances ship as
   *placeholders*. Any output that applies a threshold reports
   `thresholds_status` = `"placeholder"` or `"approved"`.
6. **Separate builder and challenger.** The agent that builds the plan is never
   the only agent that evaluates it (`board-challenge-agent`).
7. **Two grades.** Reports state whether they are *decision-grade* (reconciled,
   fit to identify strategic issues) and/or *audit-grade* (reconciled AND
   extracted from a native workbook with formulas AND source hashes verified).
   A rendered PDF is never audit-grade.

## 2. File layout

```
<repo root>/.claude-plugin/marketplace.json      # marketplace "mi-division" listing this plugin
mi-division-review/
├── .claude-plugin/plugin.json
├── .mcp.json                                    # stdio server "mi-finance-engine"
├── README.md  CONNECTORS.md  requirements.txt
├── commands/   budget-review.md monthly-reforecast.md board-pack.md community-review.md
│               land-gate.md challenge-plan.md capital-map.md
├── agents/     financial-reconciler.md community-analyst.md land-capital-analyst.md
│               peer-benchmark-agent.md board-challenge-agent.md
├── skills/     homebuilder-finance/ variance-analysis/ board-writing/ data-quality/   (SKILL.md + reference files)
├── hooks/hooks.json
├── hooks/scripts/guard_sources.py               # PreToolUse
├── hooks/scripts/validate_output.py             # PostToolUse
├── engine/mcp_server.py                          # zero-dependency MCP stdio server
├── engine/mi_review.py                           # CLI, same functions as MCP tools
├── engine/mi_engine/{__init__,canonical,config,extract,normalize,validate,reconcile,
│                     variance,reforecast,community,capital_map,land,scenarios,benchmarks,io}.py
├── config/thresholds.default.json  config/account-map.default.json
├── schemas/budget.schema.json community.schema.json reconciliation.schema.json
│           mapping.schema.json land-deal.schema.json land-gate.schema.json peer-benchmarks.schema.json
├── templates/pl-template.csv metrics-template.csv land-deal-template.json mapping-template.json
│             peer-benchmarks-template.json
├── examples/generate_sample.py  examples/sample-division/  examples/broken-division/
├── tests/test_*.py                               # stdlib unittest; run: python3 -m unittest discover -s tests
├── evals/<case>/prompt.md + graders/*.md
└── docs/ARCHITECTURE.md  docs/DATA-REQUEST.md
```

Runtime: Python ≥ 3.9, stdlib only for the engine core. Optional:
`openpyxl` (xlsx), `pdfplumber` (pdf), `jsonschema` (stricter validation). A
missing optional library produces a clear error naming the `pip install` line;
it never crashes the MCP server.

## 3. Canonical dataset (`schemas/budget.schema.json`)

File name: `canonical.json`. Shape:

```json
{
  "meta": {
    "schema_version": "1.0",
    "division": "Sample Division",
    "currency": "USD",
    "amount_unit": "dollars",
    "extraction_method": "canonical_template | native_xlsx_mapping | pdf_table_mapping | llm_transcribed",
    "sources": [{"source_id": "src1", "path": "/abs/path/file.xlsx", "type": "xlsx|csv|pdf|json",
                 "sha256": "...", "extracted_at": "ISO-8601"}],
    "warnings": ["..."],
    "unmapped_labels": [{"label": "...", "source_ref": "..."}]
  },
  "pl": [ PLRecord ],
  "metrics": [ MetricRecord ]
}
```

**PLRecord** — one P&L account line:

| field | type | notes |
|---|---|---|
| year | int | required |
| scenario | enum | required: `actual`, `forecast`, `budget`, `reforecast` |
| division | string | required |
| community | string \| null | null = division level |
| month | int 1–12 \| null | null = full year |
| account_number | string \| null | |
| account_name | string | raw label as it appears in the source |
| line_item | enum | canonical item, see below |
| amount | number | whole dollars, sign convention below |
| is_subtotal | bool | default false; true for "Total G&A"-style rows |
| presentation | enum | `detail` (default) or `summary` (short-form summary page) |
| source_ref | string | e.g. `src1!P&L 2027!N14` or `src1#p3:t1:r14c7` |

**Canonical P&L line items** (`canonical.PL_LINE_ITEMS`):
`revenue, cost_of_sales, gross_profit, other_income, warranty, general_conditions,
g_and_a, selling, interest, pre_tax_income`.
Memo items (never in the identities): `incentives, land_sale_revenue, land_sale_cost`.

**Sign convention:** revenue and every expense (`cost_of_sales, warranty,
general_conditions, g_and_a, selling, interest, incentives, land_sale_cost`)
are stored **positive**. `other_income`, `gross_profit`, `pre_tax_income` are
signed (negative = loss). The normalizer flips signs when a source shows
expenses as negatives and records a warning.

**MetricRecord** — operating metrics, one row per (year, scenario, division,
community, month):

`year, scenario, division, community (null = division), month (null = full year),
community_status (pre_open|new|active|close_out|null), units (closings), sales,
starts, months_selling, revenue, cost, gross_profit, margin (fraction, 0.215 =
21.5%), incentives, land_owned (lots), land_controlled (lots), option_deposits ($),
land_investment ($), wip ($), investment ($ total inventory: land+WIP+finished),
interest ($), headcount, source_ref`. All metric fields are optional numbers or
null. A MetricRecord must have at least one metric populated.

## 4. Engine API (Python, `engine/mi_engine`)

All functions are pure (no prompts, no network). Paths are absolute.

### canonical.py
```python
SCHEMA_VERSION = "1.0"
PL_LINE_ITEMS, MEMO_LINE_ITEMS, EXPENSE_ITEMS, SIGNED_ITEMS, METRIC_FIELDS, SCENARIOS, COMMUNITY_STATUSES
def empty_dataset(division: str, sources: list | None = None) -> dict
def load_dataset(path: str) -> dict
def save_dataset(ds: dict, path: str) -> None
def pl_summary(ds, year, scenario, community=None, month=None, presentation="detail") -> dict[str, float]
    # For each line_item: sum of non-subtotal rows if any exist, else the subtotal row.
    # month=None -> annual rows; if none exist, sum of monthly rows (adds "_derived_from_months": True).
    # community=None -> division-level rows only.
def reported_subtotals(ds, year, scenario, community=None, month=None, presentation="detail") -> dict[str, float]
def metrics_for(ds, year, scenario, community="*", month=None) -> list[dict]   # "*" = every community row (excludes division row)
def division_metrics(ds, year, scenario, month=None) -> dict | None            # the community=null row, if present
def periods(ds) -> list[tuple[int, str]]                                        # distinct (year, scenario), sorted
def communities(ds, year=None, scenario=None) -> list[str]
```

### config.py
```python
def load_config(path: str | None = None, cwd: str | None = None) -> dict
```
Merge order: `config/thresholds.default.json` → `$MI_REVIEW_CONFIG` → `<cwd>/mi-review.config.json` → explicit `path`.
Adds `cfg["_meta"] = {"status": "placeholder"|"approved", "sources": [...]}`. Status is
`approved` only when the merged config has non-empty `approved_by` and `approved_on`.
Also exposes `load_account_map(cfg, cwd)` reading `config/account-map.default.json`
overridden by `<cwd>/mi-review.account-map.json`.

**thresholds.default.json keys** (all PLACEHOLDER values; the file says so):
```json
{
  "status": "placeholder", "approved_by": null, "approved_on": null,
  "reconciliation": {"abs_tolerance_dollars": 5000, "rel_tolerance": 0.0005,
                      "margin_tolerance_pp": 0.1, "summary_vs_detail_severity": "fail",
                      "asp_min": 150000, "asp_max": 3000000},
  "materiality": {"abs_dollars": 250000, "pct": 0.05},
  "community": {"roi_hurdle": 0.25, "exit_roi_floor": 0.10, "min_gross_margin": 0.20,
                "min_monthly_sales_pace": 3.0, "max_incentive_pct": 0.08,
                "max_years_supply_owned": 3.0, "max_years_supply_total": 6.0,
                "ranking_weights": {"sales_pace": 0.2, "gross_profit": 0.2, "incentive_pct": 0.2,
                                     "wip": 0.1, "roi": 0.3}},
  "land_gate": {"irr_hurdle": 0.20, "borderline_band": 0.03, "accelerate_spread": 0.05,
                "min_gross_margin": 0.20, "downside_irr_floor": 0.10, "discount_rate": 0.12,
                "max_renegotiation_discount": 0.25},
  "scenarios": {"downside": {"asp_pct": -0.05, "volume_pct": -0.15, "unit_cost_pct": 0.03,
                              "incentive_pp": 0.02, "delay_months": 3},
                "upside":   {"asp_pct": 0.03, "volume_pct": 0.10, "unit_cost_pct": -0.02,
                              "incentive_pp": -0.01, "delay_months": 0}},
  "output_root": "mi-review-output"
}
```

### extract.py
```python
class MissingDependency(Exception)        # message includes the pip install line
def sha256_file(path) -> str
def detect_format(path) -> str            # canonical_csv | canonical_xlsx | canonical_json | xlsx | csv | pdf
def parse_number(value) -> float | None   # "(1,234)"->-1234, "$1,234.5"->1234.5, "12.5%"->0.125, "—"/"-"/""->None
def extract_tables(path, source_id="src1") -> dict
    # {"source": {source_id, path, type, sha256, extracted_at},
    #  "tables": [{"table_id": "S:<sheet>" | "p<page>:t<n>", "sheet"|"page", "n_rows", "n_cols",
    #              "rows": [[cell values as str|number|None]], "cell_refs": [["A1",...]] | None,
    #              "formulas": [[str|None]] | None}]}
    # xlsx: openpyxl, values (data_only=True) + formulas (data_only=False). pdf: pdfplumber tables per page.
def load_canonical_template(paths: list[str], division: str | None = None) -> dict
    # Accepts pl-template.csv / metrics-template.csv style CSVs, an xlsx with sheets "pl" and "metrics",
    # or a canonical.json. Returns a canonical dataset (extraction_method="canonical_template").
def apply_mapping(raw: dict, mapping: dict, account_map: dict, cfg: dict) -> dict
    # Deterministically converts raw tables to a canonical dataset using mapping.json (schema below).
def register_source(source: dict, output_root: str) -> None
    # Appends {path, sha256, registered_at} to <output_root>/.sources.json (dedup by path).
def verify_sources(ds: dict) -> list[str]  # warnings when a source's current sha256 differs from meta
```

**mapping.json** (`schemas/mapping.schema.json`) — written by Claude/the
financial-reconciler, applied by the engine:
```json
{
  "division": "Sample Division",
  "amount_multiplier": 1000,
  "expense_sign": "positive | negative | auto",
  "tables": [
    {"table_id": "S:P&L", "record_type": "pl", "presentation": "detail",
     "header_rows": 3, "label_column": 0, "account_number_column": null,
     "community": null, "skip_labels": ["^\\s*$"],
     "row_overrides": {"Total G&A": {"is_subtotal": true, "line_item": "g_and_a"}},
     "columns": {"13": {"year": 2027, "scenario": "budget", "month": null},
                 "1":  {"year": 2027, "scenario": "budget", "month": 1}}},
    {"table_id": "p7:t1", "record_type": "metrics", "header_rows": 2, "label_column": 0,
     "community_status": {"Dunmore Station": "new"},
     "columns": {"2": {"field": "units", "year": 2027, "scenario": "budget"},
                 "3": {"field": "revenue", "year": 2027, "scenario": "budget", "multiplier": 1000}}}
  ]
}
```
For `pl` tables the row label is mapped to a line_item with the account map
(unmapped labels go to `meta.unmapped_labels`, never silently dropped). For
`metrics` tables the row label is the community name; a label matching
`^total` becomes the division row (community=null). Column keys are 0-based
column indexes as strings. Per-column `multiplier` overrides
`amount_multiplier`; multipliers never apply to counts (`units, sales, starts,
land_owned, land_controlled, headcount, months_selling`) or to `margin`.

### normalize.py
```python
def map_label(label: str, account_map: dict) -> tuple[str | None, bool]   # (line_item, is_subtotal)
def normalize_community(name: str | None, aliases: dict) -> str | None
def normalize(ds: dict, cfg: dict, account_map: dict) -> dict             # returns ds; appends meta.warnings
```
`account-map.default.json`: `{"subtotal_prefix": "^\\s*total\\b", "rules": [{"pattern": "...", "line_item": "..."}],
"community_aliases": {}}` — first matching case-insensitive regex wins. Defaults cover
typical homebuilder labels (home sales revenue, cost of sales / land / house
cost, gross profit, other income / land sale profit, warranty, general
conditions / indirect construction, G&A, selling / commissions / marketing /
model costs, interest, pre-tax income, incentives / concessions / buydowns).

### validate.py
```python
SCHEMAS = {"budget", "community", "reconciliation", "mapping", "land-deal", "land-gate", "peer-benchmarks"}
def validate(obj, schema_name: str) -> list[str]   # [] when valid; uses jsonschema if installed else built-in subset
```
Built-in subset supports: type (incl. arrays of types, "integer"), required,
properties, additionalProperties (bool), items, enum, const, minimum, maximum,
pattern, anyOf, oneOf, minItems, $ref to `#/$defs/...`.

### reconcile.py
```python
class UnreconciledError(Exception): report: dict
def reconcile(ds: dict, cfg: dict) -> dict
def require_reconciled(ds: dict, cfg: dict, allow_unreconciled: bool = False) -> dict
    # returns the report; raises UnreconciledError when status == "FAIL" and not allowed.
def stamp_for(report: dict) -> str | None        # "UNRECONCILED — NOT FOR DECISION" when FAIL, else None
```
Tolerance for a comparison of `expected` vs `actual`:
`tol = max(abs_tolerance_dollars, rel_tolerance * abs(expected))`; pass if `abs(diff) <= tol`.

Checks (run for every (year, scenario), every community that has P&L rows,
every month present, each presentation present):

| check_id | formula | severity on miss |
|---|---|---|
| `gp_identity` | revenue − cost_of_sales = gross_profit (P&L) | fail |
| `pti_waterfall` | gross_profit + other_income − warranty − general_conditions − g_and_a − selling − interest = pre_tax_income | fail |
| `subtotal_tieout` | Σ detail rows of a line_item = its reported subtotal row | fail |
| `monthly_rollup` | Σ months = annual (per line_item / metric) when both exist | fail |
| `community_rollup` | Σ community metrics (units, revenue, cost, gross_profit, investment) = division metrics row | fail |
| `pl_vs_metrics` | P&L division revenue = Σ community metric revenue (and division GP) | fail |
| `metric_gp_identity` | metric revenue − cost = gross_profit | fail |
| `margin_check` | reported margin = gross_profit / revenue within `margin_tolerance_pp` | warn |
| `summary_vs_detail` | each line_item in `presentation=summary` = same item in `detail` | `summary_vs_detail_severity` |
| `asp_sanity` | asp_min ≤ revenue/units ≤ asp_max | warn |
| `source_integrity` | source sha256 unchanged | warn |
| `unmapped_labels` | no unmapped labels in meta | warn |

A check whose inputs are absent is `skipped` (with reason), never `pass`.

Report (`schemas/reconciliation.schema.json`, file `reconciliation.json`):
```json
{"status": "PASS|PASS_WITH_WARNINGS|FAIL", "division": "...",
 "tolerance": {"abs_tolerance_dollars": 5000, "rel_tolerance": 0.0005, "margin_tolerance_pp": 0.1},
 "summary": {"checks_run": 0, "passed": 0, "warnings": 0, "failures": 0, "skipped": 0},
 "checks": [{"check_id": "gp_identity",
             "scope": {"year": 2027, "scenario": "budget", "community": null, "month": null, "presentation": "detail"},
             "status": "pass|warn|fail|skipped", "formula": "revenue - cost_of_sales = gross_profit",
             "expected": 0, "actual": 0, "diff": 0, "tolerance": 0,
             "message": "plain-English statement of what does / does not tie", "source_refs": ["..."]}],
 "failures": [ /* the failing checks again, for quick reading */ ],
 "decision_grade": true, "audit_grade": false, "grade_reasons": ["..."]}
```
`decision_grade` = status != FAIL. `audit_grade` = status == PASS and
`extraction_method` in (`native_xlsx_mapping`, `canonical_template` from xlsx/csv
exported from the model) and no `source_integrity` warning and no
`llm_transcribed` data. PDF-sourced data is never audit-grade.

### variance.py
```python
def yoy(ds, base: dict, comp: dict, cfg: dict, allow_unreconciled=False) -> dict
    # base/comp = {"year": 2026, "scenario": "forecast"}
    # {"base","comp","reconciliation_status","stamp",
    #  "lines": [{"line_item","base","comp","delta","delta_pct","favorable": bool|None,"material": bool}],
    #  "kpis": {"base": KPI, "comp": KPI},
    #  "bridges": {"revenue": Bridge, "gross_profit": Bridge, "g_and_a": Bridge}}
def revenue_bridge(ds, base, comp) -> dict
def gross_profit_bridge(ds, base, comp) -> dict
def overhead_bridge(ds, base, comp) -> dict
```
KPI = `units, asp, revenue, gross_profit, gm_pct, ga_pct_revenue, selling_pct_revenue,
pti, pti_margin, headcount, ga_per_head, ga_per_closing, incentive_pct`.
`favorable`: revenue/income/profit up = true; expense up = false.
`material`: `abs(delta) >= materiality.abs_dollars or abs(delta_pct) >= materiality.pct`.

**Revenue bridge (exact, community level):** for communities in both periods
`volume_i = (U1_i − U0_i) × ASP0_i`, `price_i = (ASP1_i − ASP0_i) × U1_i`;
`new = Σ R1_i` for communities only in comp; `closed = −Σ R0_i` for communities
only in base. Components: `same_community_volume, same_community_price,
new_communities, closed_communities`. Also a division-level two-way split
`volume = (U1−U0)×ASP0`, `asp = (ASP1−ASP0)×U1` and an `asp_mix_vs_price` note:
how much of the division ASP change comes from mix (new/closed communities and
volume shifts) versus like-for-like price. **Gross-profit bridge:** same
structure with GP per unit. **Overhead bridge:** `headcount_effect =
(HC1−HC0) × G&A_per_head0`, `cost_per_head_effect = (per_head1 − per_head0) × HC1`,
and a flag `ga_up_while_headcount_down`.

Bridge shape: `{"total", "components": [{"name","amount","explanation"}],
"check": {"sum_of_components", "diff"}, "communities": [...], "notes": [...]}`.
`check.diff` must be 0 (within 1e-6); assert in code.

### reforecast.py
```python
def reforecast(ds, year, through_month, cfg, method="budget_remaining",
               actual_scenario="actual", budget_scenario="budget", allow_unreconciled=False) -> dict
```
method: `budget_remaining` (FY = YTD actual + remaining budget months),
`run_rate` (FY = YTD actual × 12/through_month), `blend` (average). Output per
line_item and per community metric: `ytd_actual, ytd_budget, ytd_variance,
ytd_variance_pct, fy_budget, fy_reforecast, fy_variance, flag`. Flags: material
YTD variance; trend worsening (last 3 months variance worse than YTD average).

### community.py
```python
def community_economics(ds, year, scenario, cfg, allow_unreconciled=False) -> dict
```
Per community: `community, community_status, units, sales, starts,
months_selling, sales_pace (sales / months_selling; months_selling defaults to
count of monthly rows with sales > 0, else 12 with a data_gap note), revenue,
asp, cost, gross_profit, gm_pct, gp_per_unit, incentives, incentive_pct
(incentives / (revenue + incentives)), wip, land_investment, investment,
interest, contribution (gross_profit − interest), roi (gross_profit /
investment), contribution_roi, inventory_turns (cost / investment), land_owned,
land_controlled, years_supply_owned (land_owned / units), years_supply_total,
ranks {sales_pace, gross_profit, incentive_pct (lower is better), wip (lower is
better), roi}, composite_score (weighted average of percentile ranks with
`ranking_weights`), composite_rank, classification, recommended_action,
flags [], data_gaps []`. Missing inputs give `null`, never 0.

**Classification** (in this order):
1. required inputs missing for roi → `insufficient_data` / `obtain_data`
2. `community_status` in (pre_open, new) and roi < roi_hurdle → `temporarily_consuming` / `proceed_with_gates`
3. roi ≥ roi_hurdle and gm_pct ≥ min_gross_margin → `value_creating` / `proceed`
   (`proceed_accelerate` if sales_pace ≥ 1.25 × min_monthly_sales_pace and incentive_pct ≤ max_incentive_pct)
4. roi < exit_roi_floor or (gm_pct < min_gross_margin and years_supply_total > max_years_supply_total) → `exit_review` / `exit_review`
5. otherwise → `underperforming` / `slow` (or `renegotiate` if years_supply_total > max_years_supply_total)

Output also has `year, scenario, division, thresholds_status,
reconciliation_status, stamp, totals {units, revenue, gross_profit,
investment, roi}`. Validates against `schemas/community.schema.json` (file
`community-economics.json`).

### capital_map.py
```python
def capital_map(ds, year, scenario, cfg, allow_unreconciled=False) -> dict
```
`{"year","scenario","thresholds_status","reconciliation_status","stamp",
"rows": [{community, classification, recommended_action, investment, share_of_investment,
gross_profit, roi, gm_pct, sales_pace, years_supply_total}],  # sorted by roi desc, nulls last
"buckets": {classification: {"count","investment","share_of_investment","gross_profit"}},
"markdown": "<pipe table>", "csv": "<csv text>"}`.

### land.py
```python
def npv(rate_annual: float, cashflows: list[float], periods_per_year=12) -> float
def irr(cashflows: list[float], periods_per_year=12) -> float | None   # annualized: (1+r_m)^12 − 1; None if no sign change
def deal_cashflows(deal: dict, shocks: dict | None = None) -> dict      # monthly model, see below
def max_supportable_lot_price(deal: dict, cfg: dict) -> float | None    # avg lot price at which base IRR == irr_hurdle (bisection)
def land_gate(deal: dict, cfg: dict) -> dict
```
**Deal** (`schemas/land-deal.schema.json`, template `templates/land-deal-template.json`):
`community, division, lots, takedowns [{month, lots, price_per_lot}],
development_cost_per_lot, asp, incentive_pct, house_cost_per_home,
closing_cost_pct, warranty_pct, absorption_per_month, first_sale_month,
cycle_time_months, option_deposit, deposit_credited_to_takedowns (bool),
walk_away_rights (bool), discount_rate (null → cfg), notes`. Required:
`community, lots, takedowns, asp, house_cost_per_home, absorption_per_month,
first_sale_month, cycle_time_months`. The engine never fills a missing
required input; it returns `decision: "insufficient_data"` with `data_gaps`.

**Monthly model:** month 0 = today. Deposit paid month 0 (credited against
takedown payments in order when `deposit_credited_to_takedowns`). Land +
development paid at each takedown month. Sales start at `first_sale_month` at
`absorption_per_month` (fractional sales accumulate) and a lot can only be sold
once taken down. Each sale starts construction immediately; house cost is spread
evenly over `cycle_time_months`; closing happens at sale month +
`cycle_time_months` with revenue `asp × (1 − incentive_pct)` less
`closing_cost_pct` and `warranty_pct` of that revenue.
`gross_margin = (net revenue − land − development − house cost) / net revenue`
(before interest and overhead — say so in outputs).
Shocks: `asp_pct, volume_pct (absorption), unit_cost_pct (house cost),
incentive_pp, delay_months (first_sale_month)`. Downside/upside from
`cfg.scenarios`.

**Decision path** (record each rule evaluated in `decision_path`):
```
h = irr_hurdle; base_ok = base.irr >= h and base.gross_margin >= min_gross_margin
down_ok = downside.irr is not None and downside.irr >= downside_irr_floor
if base_ok and down_ok and base.irr >= h + accelerate_spread -> "proceed_accelerate"
elif base_ok and down_ok                                     -> "proceed"
elif base_ok                                                 -> "proceed_with_gates"
elif base.irr is not None and base.irr >= h - borderline_band -> "proceed_with_gates"
else:
    target = max_supportable_lot_price(deal, cfg); current = weighted avg takedown price
    if target is None or target <= 0 or target < current * (1 - max_renegotiation_discount):
        "exit" if walk_away_rights else "defer_renegotiate"   (note remaining committed exposure)
    else: "defer_renegotiate" (target lot price = target)
```
`gates` (for proceed_with_gates): concrete, numeric conditions, e.g. phase the
next takedown until N sales are achieved, or lot price ≤ target. Output
(`schemas/land-gate.schema.json`, file `land-gate-<community-slug>.json`):
`{deal, thresholds, thresholds_status, scenarios {base, downside, upside: {npv,
irr, gross_margin, profit, peak_capital, roi_on_peak_capital, payback_month,
total_closings, last_closing_month}}, max_supportable_lot_price,
current_avg_lot_price, decision, decision_path [], gates [], data_gaps [], notes []}`.

### scenarios.py
```python
def division_scenarios(ds, year, scenario, cfg, allow_unreconciled=False) -> dict
```
Base from `pl_summary` + division metrics. Downside/upside apply shocks: units
× (1+volume_pct), ASP × (1+asp_pct), unit cost × (1+unit_cost_pct), incentives
+pp of gross revenue. `selling` and `warranty` flex as % of revenue;
`general_conditions`, `g_and_a`, `interest`, `other_income` are fixed.
Output: `{"scenarios": {"base"|"downside"|"upside": {units, asp, revenue, cost_of_sales,
gross_profit, gm_pct, selling, warranty, general_conditions, g_and_a, interest,
other_income, pre_tax_income, pti_margin}}, "sensitivities": {"pti_per_1pct_asp",
"pti_per_1pct_volume", "pti_per_1pct_unit_cost", "pti_per_1pp_incentive"},
"assumptions": [...], "reconciliation_status", "stamp"}`.

### benchmarks.py
```python
def benchmark_compare(ds, peers: dict, year, scenario, cfg, allow_unreconciled=False) -> dict
```
Validates `peers` against `schemas/peer-benchmarks.schema.json`: every metric
value must carry `source_url`, `source_title`, `period_end`, `retrieved_on`,
`definition_note`. Division KPIs: `gm_pct, sga_pct ((g_and_a + selling) / revenue),
asp, pti_margin, closings`. Output: side-by-side rows plus
`comparability_notes` (fiscal calendar, consolidated vs division, impairment and
interest-in-COGS differences) and `"label": "DIRECTIONAL — not like-for-like"`.

### io.py (shared helpers)
```python
def output_root(cwd: str | None, cfg: dict) -> str         # <cwd>/<cfg.output_root>, created if missing
def write_json(obj, path) -> str
def slug(text) -> str
```

## 5. CLI — `engine/mi_review.py`

`python3 engine/mi_review.py <subcommand> [options]`; prints JSON to stdout,
writes `--out` when given. Subcommands mirror the MCP tools:
`extract, load, map, validate, reconcile, variance, reforecast, community,
capital-map, land-gate, scenarios, benchmarks, config`. Common options:
`--config PATH`, `--allow-unreconciled`, `--out PATH`. Exit codes: 0 ok, 2 bad
input, 3 reconciliation FAIL blocked the analysis, 4 missing optional
dependency.

## 6. MCP server — `engine/mcp_server.py`

Zero-dependency JSON-RPC 2.0 over stdio (newline-delimited). Implements
`initialize` (echo client's protocolVersion if one of `2025-06-18`,
`2025-03-26`, `2024-11-05`, else `2025-06-18`; capabilities `{"tools": {}}`;
serverInfo `{"name": "mi-finance-engine", "version": <plugin version>}`),
`notifications/initialized` (no reply), `ping`, `tools/list`, `tools/call`;
unknown methods → error -32601. Tool errors return `{"content": [{"type":
"text", "text": ...}], "isError": true}` — never crash the process. Logs go to
stderr only.

Relative paths resolve against `$CLAUDE_PROJECT_DIR`, else the server cwd.
Every tool writes its JSON result into the output folder
(`out_dir` argument, default `<project>/mi-review-output/`) and returns the
JSON text (if > 60,000 characters it returns a summary plus `output_path`).

| tool | arguments | writes |
|---|---|---|
| `get_config` | config_path? | — |
| `extract_tables` | path, out_dir? | `raw-tables.json` (+ registers source) |
| `load_canonical` | paths[], division?, out_dir? | `canonical.json` (+ registers sources) |
| `apply_mapping` | raw_tables_path, mapping_path, out_dir? | `canonical.json` |
| `validate_dataset` | dataset_path | — |
| `reconcile` | dataset_path, config_path?, out_dir? | `reconciliation.json` |
| `variance` | dataset_path, base_year, base_scenario, comp_year, comp_scenario, config_path?, allow_unreconciled?, out_dir? | `variance.json` |
| `reforecast` | dataset_path, year, through_month, method?, config_path?, allow_unreconciled?, out_dir? | `reforecast.json` |
| `community_economics` | dataset_path, year, scenario, config_path?, allow_unreconciled?, out_dir? | `community-economics.json` |
| `capital_map` | dataset_path, year, scenario, config_path?, allow_unreconciled?, out_dir? | `capital-map.json` |
| `land_gate` | deal_path, config_path?, out_dir? | `land-gate-<slug>.json` |
| `division_scenarios` | dataset_path, year, scenario, config_path?, allow_unreconciled?, out_dir? | `scenarios.json` |
| `benchmark_compare` | dataset_path, peers_path, year, scenario, config_path?, allow_unreconciled?, out_dir? | `peer-benchmarks-compare.json` |

When reconciliation blocks a tool, it returns `isError: true` with the
reconciliation failures listed and the instruction "Stop executive analysis;
report these exceptions."

Full tool names seen by Claude:
`mcp__plugin_mi-division-review_mi-finance-engine__<tool>`.

`.mcp.json`:
```json
{"mcpServers": {"mi-finance-engine": {"command": "python3",
  "args": ["${CLAUDE_PLUGIN_ROOT}/engine/mcp_server.py"]}}}
```

## 7. Hooks — `hooks/hooks.json`

```json
{"hooks": {
  "PreToolUse": [
    {"matcher": "^(Write|Edit|MultiEdit|NotebookEdit)$", "hooks": [{"type": "command",
      "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/scripts/guard_sources.py\"", "timeout": 10}]},
    {"matcher": "^mcp__.*([Dd]rive|[Dd]ropbox|[Bb]ox|[Ss]hare[Pp]oint|[Oo]ne[Dd]rive|Microsoft_365).*", "hooks": [{"type": "command",
      "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/scripts/guard_sources.py\"", "timeout": 10}]}],
  "PostToolUse": [
    {"matcher": "^(Write|Edit|MultiEdit)$", "hooks": [{"type": "command",
      "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/scripts/validate_output.py\"", "timeout": 30}]}]}}
```

**guard_sources.py** (PreToolUse; reads hook JSON on stdin; never crashes —
any internal error → exit 0):
- File tools: if the target path (resolved against `cwd`) is listed in
  `<cwd>/mi-review-output/.sources.json` → print PreToolUse JSON with
  `permissionDecision: "deny"` and a reason ("registered budget source; sources
  are read-only — write outputs to mi-review-output/ instead").
- Cloud-storage MCP tools: if the tool name contains a write verb (`create,
  update, upload, delete, trash, move, copy, share, rename, edit, write,
  insert, apply, comment, permission`) and `MI_REVIEW_ALLOW_SOURCE_WRITES` is
  not `1` and `<cwd>/mi-review-output/` exists → `permissionDecision: "ask"`
  with a reason explaining the read-only policy. Otherwise exit 0 silently.

**validate_output.py** (PostToolUse): acts only on files inside a
`mi-review-output` directory.
- JSON artifacts validated with `mi_engine.validate`: `canonical.json`→budget,
  `reconciliation.json`→reconciliation, `community-economics.json`→community,
  `land-gate*.json`→land-gate, `peer-benchmarks*.json` (inputs, not
  `-compare`)→peer-benchmarks, `mapping*.json`→mapping.
- Markdown reports (`executive-brief*.md, board-pack*.md, community-review*.md,
  land-gate*.md, reforecast*.md, capital-map*.md, challenge*.md`) must contain
  headings (case-insensitive) **Reconciliation status**, **Assumptions**, **Data
  gaps**; `land-gate*.md` and `capital-map*.md` must also mention
  **Thresholds** and, when the config is placeholder, the word "placeholder".
  If a `reconciliation.json` in the same folder or its parent has status
  `FAIL`, the report must contain "UNRECONCILED".
- **Number provenance:** collect figures from the markdown (`$12.3M`, `$571k`,
  `21.4%`, `1,234`) and check each against numbers in sibling engine JSON files
  (rounding-aware). Unmatched figures are returned as
  `hookSpecificOutput.additionalContext` (warning). With
  `MI_REVIEW_STRICT_PROVENANCE=1` they block (`decision: "block"`).
- Violations: print reasons to stderr and exit 2 (Claude sees them and fixes
  the file). Import the engine via the script's own path
  (`Path(__file__).resolve().parents[2] / "engine"`).

## 8. Output folder convention

`<project>/mi-review-output/<review-name>/` e.g. `mi-review-output/2027-budget/`.
Engine JSON and Claude-written markdown live side by side:
`raw-tables.json, mapping.json, canonical.json, reconciliation.json,
variance.json, community-economics.json, capital-map.json, scenarios.json,
land-gate-*.json, peer-benchmarks.json, peer-benchmarks-compare.json,
challenge.md, executive-brief.md, board-pack.md, data-request.md`.
`mi-review-output/.sources.json` is the read-only source registry.

## 9. Commands (namespaced `/mi-division-review:<name>`)

| command | argument-hint | purpose |
|---|---|---|
| `budget-review` | `<budget file or Drive link> [base=2026:forecast] [comp=2027:budget]` | full YoY review → executive brief |
| `monthly-reforecast` | `<actuals file> [through=YYYY-MM] [method=budget_remaining\|run_rate\|blend]` | YTD vs budget, FY reforecast, KPI dashboard |
| `board-pack` | `[review folder]` | CEO/CFO/regional pack from an existing review |
| `community-review` | `<file or review folder> [year:scenario]` | rank communities, classify capital |
| `land-gate` | `<community name or deal.json>` | invest/defer/renegotiate/exit recommendation |
| `challenge-plan` | `[review folder]` | independent skeptic challenge of the plan |
| `capital-map` | `<file or review folder> [year:scenario]` | community return-on-capital map |

Arguments: use `$ARGUMENTS` (placeholders are 0-based: `$0` is the first
argument). Every command: (1) resolves inputs, (2) runs the engine via MCP tools
(CLI fallback: `python3 "${CLAUDE_PLUGIN_ROOT}/engine/mi_review.py" ...`),
(3) stops on reconciliation FAIL and writes `reconciliation-exceptions.md`,
(4) delegates to agents, (5) writes markdown with the required sections.

## 10. Agents (`subagent_type: mi-division-review:<name>`)

| agent | role | tools |
|---|---|---|
| `financial-reconciler` | inspect raw tables, author mapping.json, run extraction + reconciliation, explain exceptions; never plugs differences | Read, Glob, Grep, Write, engine MCP tools |
| `community-analyst` | interpret community economics, explain drivers and outliers | Read, Glob, Grep, engine MCP tools |
| `land-capital-analyst` | land gates, lot supply, option exposure, cash/investment | Read, Glob, Grep, Write, engine MCP tools |
| `peer-benchmark-agent` | official IR/SEC/Census/HUD/Freddie Mac/NAHB/Fannie Mae sources only; writes peer-benchmarks.json with citations | Read, Write, WebSearch, WebFetch, engine MCP tools |
| `board-challenge-agent` | independent skeptic: ASP, volume, margin, land timing, overhead | Read, Glob, Grep, engine MCP tools (scenarios, variance) — no Write |

## 11. Skills

`homebuilder-finance` (domain glossary + KPI formulas), `variance-analysis`
(bridges, materiality, favorable/unfavorable language), `board-writing`
(executive brief and board deck structure, decision-first writing),
`data-quality` (extraction confidence, reconciliation tolerance, gap
classification, decision-grade vs audit-grade, data-request checklist).

## 12. Required report sections (every markdown report)

1. Bottom line / decision requested
2. **Reconciliation status** (PASS / PASS_WITH_WARNINGS / FAIL + grade)
3. Findings (each figure traceable to an engine JSON file)
4. Risks and challenges
5. **Data gaps** and clarifying questions
6. **Assumptions** and methodology (incl. **Thresholds**: placeholder/approved)
