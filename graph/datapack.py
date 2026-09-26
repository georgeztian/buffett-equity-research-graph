"""Stage 0: deterministic SEC data pack, shared by every agent in a run.

Pulls the company's annual XBRL facts from SEC EDGAR once, so the three Stage 1 agents, the MOS agent, the
reviewer and the report agent all work from the same primary-source numbers instead of each fetching (and re-deriving) them.
No model is involved. Values are copied exactly as filed; every row names its XBRL tag (different tags are never
merged into one row); the few reference calculations show their formulas and inputs. If anything fails, the
pack says it is unavailable and the agents research everything as before, so this stage can never fail a run.
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from .config import DATA_PACK_YEARS, SEC_TIMEOUT_SECONDS, Paths, sec_contact

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT", "10-KT/A")

FLOW, STOCK = "flow", "stock"   # flow = amount for a fiscal year; stock = balance at fiscal year end
USD, PER_SHARE, SHARES = "USD", "USD/shares", "shares"

# section -> [(label, period kind, unit, candidate tags)]. Each tag that has data becomes its own row.
ITEMS: dict[str, list[tuple[str, str, str, tuple[str, ...]]]] = {
    "Income statement": [
        ("Revenue", FLOW, USD, ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                "RevenuesNetOfInterestExpense", "SalesRevenueNet")),
        ("Cost of revenue / COGS", FLOW, USD, ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")),
        ("Operating income", FLOW, USD, ("OperatingIncomeLoss",)),
        ("Pretax income", FLOW, USD, (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments")),
        ("Income tax expense", FLOW, USD, ("IncomeTaxExpenseBenefit",)),
        ("Net income", FLOW, USD, ("NetIncomeLoss",)),
        ("EPS, diluted", FLOW, PER_SHARE, ("EarningsPerShareDiluted",)),
        ("EPS, basic", FLOW, PER_SHARE, ("EarningsPerShareBasic",)),
        ("Weighted-average diluted shares", FLOW, SHARES, ("WeightedAverageNumberOfDilutedSharesOutstanding",)),
    ],
    "Cash flow statement": [
        ("Operating cash flow", FLOW, USD, ("NetCashProvidedByUsedInOperatingActivities",)),
        ("Depreciation, depletion & amortization", FLOW, USD, (
            "DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization", "Depreciation")),
        ("Share-based compensation", FLOW, USD, ("ShareBasedCompensation",)),
        ("Other non-cash income/expense", FLOW, USD, ("OtherNoncashIncomeExpense",)),
        ("Capital expenditures", FLOW, USD, ("PaymentsToAcquirePropertyPlantAndEquipment",
                                             "PaymentsToAcquireProductiveAssets")),
        ("Dividends paid", FLOW, USD, ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends")),
        ("Share repurchases", FLOW, USD, ("PaymentsForRepurchaseOfCommonStock",)),
    ],
    "Balance sheet (fiscal year end)": [
        ("Cash and equivalents", STOCK, USD, ("CashAndCashEquivalentsAtCarryingValue",
                                              "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")),
        ("Accounts receivable", STOCK, USD, ("AccountsReceivableNetCurrent",)),
        ("Inventory", STOCK, USD, ("InventoryNet",)),
        ("Current assets", STOCK, USD, ("AssetsCurrent",)),
        ("Net PP&E", STOCK, USD, ("PropertyPlantAndEquipmentNet",
                                  "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization")),
        ("Total assets", STOCK, USD, ("Assets",)),
        ("Accounts payable", STOCK, USD, ("AccountsPayableCurrent",)),
        ("Current liabilities", STOCK, USD, ("LiabilitiesCurrent",)),
        ("Total liabilities", STOCK, USD, ("Liabilities",)),
        ("Debt (tag scopes differ; see note)", STOCK, USD, (
            "DebtCurrent", "LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper",
            "LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations")),
        ("Shareholders' equity", STOCK, USD, ("StockholdersEquity",
                                              "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
    ],
}


# ---------------------------------------------------------------------------------------------------- fetching
def _get_json(url: str, ua: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=SEC_TIMEOUT_SECONDS) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def _norm_name(s: str) -> str:
    s = re.sub(r"\([^)]*\)|/[a-z]{2,3}/?", " ", s.lower())   # "(GOOGL)", SEC state suffixes like "/DE/"
    s = re.sub(r"['’]", "", s)                             # "Moody's" -> "moodys"
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    stop = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "the",
            "holdings", "group", "sa", "nv", "ag", "class", "a", "b", "c"}
    return " ".join(w for w in s.split() if w not in stop)


def resolve_cik(company: str, key: str, tickers: dict) -> tuple[int, str] | None:
    """(CIK, ticker) for the company: by the ticker in the folder key, else by an unambiguous exact name match."""
    rows = list(tickers.values())
    if key.upper() == key:   # company_key() returns the ticker in upper case, a name slug in lower case
        want = key.replace(".", "-").upper()
        hits = [r for r in rows if r["ticker"].upper() == want]
        if hits:
            return int(hits[0]["cik_str"]), hits[0]["ticker"]
    name = _norm_name(company)
    ciks = {int(r["cik_str"]): r["ticker"] for r in rows if name and _norm_name(r["title"]) == name}
    return next(iter(ciks.items())) if len(ciks) == 1 else None


# ---------------------------------------------------------------------------------------------------- extraction
def _days(f: dict) -> int | None:
    if "start" not in f:
        return None
    return (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days


def _annual(facts: list[dict], kind: str) -> list[dict]:
    out = []
    for f in facts:
        if f.get("form") not in ANNUAL_FORMS:
            continue
        d = _days(f)
        if (kind == FLOW and d is not None and 350 <= d <= 380) or (kind == STOCK and d is None):
            out.append(f)
    return out


def _unit_facts(tag_data: dict, unit: str) -> list[dict]:
    return tag_data.get("units", {}).get(unit, [])


def fiscal_year_ends(gaap: dict) -> list[str]:
    """Fiscal year-end dates, from annual net income (or revenue) durations in 10-K filings; newest last."""
    for tag in ("NetIncomeLoss", "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenuesNetOfInterestExpense", "OperatingIncomeLoss"):
        if tag in gaap:
            ends = sorted({f["end"] for f in _annual(_unit_facts(gaap[tag], USD), FLOW)})
            if ends:
                return ends[-DATA_PACK_YEARS:]
    return []


def extract(gaap: dict, tag: str, kind: str, unit: str, ends: list[str]) -> dict[str, dict]:
    """end date -> {val, accn, form, filed, original_val?}. The latest-filed value is used (a later 10-K's
    restated/recast comparative takes precedence); the originally filed value is kept when it differs."""
    if tag not in gaap:
        return {}
    by_end: dict[str, list[dict]] = {}
    for f in _annual(_unit_facts(gaap[tag], unit), kind):
        if f["end"] in ends:
            by_end.setdefault(f["end"], []).append(f)
    out = {}
    for end, fs in by_end.items():
        fs.sort(key=lambda f: (f.get("filed", ""), f.get("accn", "")))
        latest, first = fs[-1], fs[0]
        rec = {"val": latest["val"], "accn": latest.get("accn"), "form": latest.get("form"),
               "filed": latest.get("filed")}
        if first["val"] != latest["val"]:
            rec.update(original_val=first["val"], original_accn=first.get("accn"), original_filed=first.get("filed"))
        out[end] = rec
    return out


# ---------------------------------------------------------------------------------------------------- formatting
def _fmt(val: float | None, unit: str) -> str:
    if val is None:
        return "—"
    if unit == PER_SHARE:
        return f"{val:.2f}"
    s = f"{val / 1e6:,.3f}".rstrip("0").rstrip(".")   # USD millions / shares in millions, no rounding of M-level data
    return s


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _m(x: float | None) -> str:
    return "—" if x is None else f"{x / 1e6:,.1f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def _recent_filings(cik: int, subs: dict) -> list[str]:
    r = subs.get("filings", {}).get("recent", {})
    want = {"10-K": 1, "20-F": 1, "10-Q": 2, "DEF 14A": 1, "8-K": 5}
    got: dict[str, int] = {}
    lines = []
    for i, form in enumerate(r.get("form", [])):
        if form in want and got.get(form, 0) < want[form]:
            got[form] = got.get(form, 0) + 1
            accn, doc = r["accessionNumber"][i], r["primaryDocument"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc}"
            period = r["reportDate"][i] or "n/a"
            lines.append(f"- {form} filed {r['filingDate'][i]} (period {period}), accession {accn}: {url}")
    return lines


# ---------------------------------------------------------------------------------------------------- calculations
def _series_tag(series: dict[str, dict[str, dict]], tags: tuple[str, ...]) -> str | None:
    """The ONE tag used for a calculation input across all years: the candidate that covers the latest year,
    then the most years. Tags are never mixed across years (they can be different line items)."""
    have = [t for t in tags if series.get(t)]
    return max(have, key=lambda t: (max(series[t]), len(series[t]), -tags.index(t))) if have else None


def calculations(series: dict[str, dict[str, dict]], ends: list[str]) -> tuple[list[list[str]], list[str], dict]:
    """Reference CALCULATION rows (display rows, notes on which tags fed which years, machine-readable values)."""
    pretax_tags = ITEMS["Income statement"][3][3]
    capex_tags = ITEMS["Cash flow statement"][4][3]
    used: dict[str, dict[str, list[str]]] = {}   # input -> tag -> [years]
    calc: dict[str, dict[str, float | None]] = {k: {} for k in ("etr", "oiat", "roe", "wc", "dwc", "fcf")}

    def take(name: str, tags: tuple[str, ...], end: str) -> float | None:
        t = _series_tag(series, tags)
        rec = series[t].get(end) if t else None
        if rec is not None:
            used.setdefault(name, {}).setdefault(t, []).append(end[:4])
        return rec["val"] if rec is not None else None

    prev_wc = None
    for end in ends:
        op = take("Operating income", ("OperatingIncomeLoss",), end)
        tax = take("Income tax expense", ("IncomeTaxExpenseBenefit",), end)
        pre = take("Pretax income", pretax_tags, end)
        eq = take("Shareholders' equity", ("StockholdersEquity",), end)
        capex = take("Capital expenditures", capex_tags, end)
        cfo = take("Operating cash flow", ("NetCashProvidedByUsedInOperatingActivities",), end)
        ca = take("Current assets", ("AssetsCurrent",), end)
        cl = take("Current liabilities", ("LiabilitiesCurrent",), end)

        etr = tax / pre if tax is not None and pre else None
        oiat = op * (1 - etr) if op is not None and etr is not None else None
        roe = oiat / eq if oiat is not None and eq else None
        wc = ca - cl if ca is not None and cl is not None else None
        dwc = wc - prev_wc if wc is not None and prev_wc is not None else None
        fcf = cfo - capex if cfo is not None and capex is not None else None
        for k, v in zip(calc, (etr, oiat, roe, wc, dwc, fcf)):
            calc[k][end] = v
        prev_wc = wc

    rows = [
        ["Effective tax rate = Income tax expense ÷ Pretax income"] + [_pct(calc["etr"][e]) for e in ends],
        ["Operating income after taxes = Operating income × (1 − effective tax rate)"] + [_m(calc["oiat"][e]) for e in ends],
        ["ROE = Operating income after taxes ÷ Shareholders' equity (`StockholdersEquity`, year end)"] + [_pct(calc["roe"][e]) for e in ends],
        ["Working capital = Current assets − Current liabilities"] + [_m(calc["wc"][e]) for e in ends],
        ["Change in working capital = WC − prior-year WC"] + [_m(calc["dwc"][e]) for e in ends],
        ["Free cash flow = Operating cash flow − Capital expenditures"] + [_m(calc["fcf"][e]) for e in ends],
    ]
    notes = [f"- {name}: " + "; ".join(f"`{t}` ({', '.join(ys)})" for t, ys in tags.items())
             for name, tags in used.items()]
    return rows, notes, calc


def possible_splits(series: dict[str, dict[str, dict]], ends: list[str]) -> list[str]:
    """Flag adjacent years whose implied share count (net income ÷ diluted EPS) jumps by 1.5x or more, the
    signature of a stock split that the older values were never restated for."""
    ni, eps = series.get("NetIncomeLoss", {}), series.get("EarningsPerShareDiluted", {})
    implied = {e: ni[e]["val"] / eps[e]["val"] for e in ends
               if e in ni and e in eps and eps[e]["val"] and ni[e]["val"] and ni[e]["val"] / eps[e]["val"] > 0}
    out, have = [], [e for e in ends if e in implied]
    for a, b in zip(have, have[1:]):
        r = implied[b] / implied[a]
        if r >= 1.5 or r <= 1 / 1.5:
            out.append(f"implied diluted share count changes about {r:.2f}x between the fiscal years ending {a} "
                       f"and {b} (net income ÷ diluted EPS): probably a stock split, so per-share values before and "
                       "after that point are on different share bases.")
    return out


# ---------------------------------------------------------------------------------------------------- build
def _unavailable(paths: Paths, company: str, reason: str) -> str:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.data_pack.write_text(
        f"# SEC data pack: {company}\n\nUNAVAILABLE: {reason}\n\n"
        "No SEC XBRL data could be prepared for this run. Research all figures as usual from primary sources.\n",
        encoding="utf-8")
    return f"unavailable ({reason})"


def build(paths: Paths, company: str) -> str:
    """Write research/<KEY>/_data/financials.md (+ financials.json, raw/). Returns a one-line status note."""
    try:
        return _build(paths, company)
    except Exception as e:   # this stage must never fail the workflow
        if isinstance(e, urllib.error.HTTPError) and e.code == 403:
            reason = ("SEC refused the request (HTTP 403 Forbidden): check that SEC_USER_AGENT (env var or .env) "
                      "holds your real name and contact email, and that you are not on a VPN or cloud IP SEC blocks")
        else:
            reason = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            return _unavailable(paths, company, reason)
        except Exception:
            return f"unavailable ({type(e).__name__})"


def _build(paths: Paths, company: str) -> str:
    ua, problem = sec_contact()
    if not ua:   # SEC would answer 403, so do not send anonymous requests
        return _unavailable(paths, company, f"{problem}; set your name and email with "
                                            "`python -m graph --set-sec-contact`")
    tickers = _get_json(TICKERS_URL, ua)
    found = resolve_cik(company, paths.key, tickers)
    if not found:
        return _unavailable(paths, company, "company not found in the SEC ticker list (not an SEC registrant, "
                                            "or the name/ticker did not match exactly)")
    cik, ticker = found
    facts = _get_json(FACTS_URL.format(cik=cik), ua)
    subs = _get_json(SUBMISSIONS_URL.format(cik=cik), ua)
    raw = paths.data_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "companyfacts.json").write_text(json.dumps(facts), encoding="utf-8")   # unmodified, for audit
    (raw / "submissions.json").write_text(json.dumps(subs), encoding="utf-8")

    gaap = facts.get("facts", {}).get("us-gaap", {})
    ends = fiscal_year_ends(gaap)
    if not ends:
        return _unavailable(paths, company, f"no annual US-GAAP XBRL data for CIK {cik} (e.g. an IFRS/20-F filer)")

    series: dict[str, dict[str, dict]] = {}
    sections: list[str] = []
    missing: list[str] = []
    header = ["Item — XBRL tag"] + ends
    restated = False
    for section, items in ITEMS.items():
        rows = []
        for label, kind, unit, tags in items:
            present = False
            for tag in tags:
                vals = extract(gaap, tag, kind, unit, ends)
                if not vals:
                    continue
                present = True
                series[tag] = vals
                cells = []
                for e in ends:
                    rec = vals.get(e)
                    mark = "†" if rec and "original_val" in rec else ""
                    restated |= bool(mark)
                    cells.append(_fmt(rec["val"] if rec else None, unit) + mark)
                rows.append([f"{label} — `{tag}`"] + cells)
            if not present:
                missing.append(label)
        if rows:
            sections.append(f"### {section}\n\n" + _table(header, rows))

    calc_rows, calc_notes, calc = calculations(series, ends)
    split_warnings = possible_splits(series, ends)

    accns: dict[str, set[str]] = {}
    for vals in series.values():
        for e, rec in vals.items():
            if rec.get("accn"):
                accns.setdefault(e, set()).add(rec["accn"])
    src_lines = [f"- FY ending {e}: " + ", ".join(sorted(accns.get(e, ()))) for e in reversed(ends)]

    shares = [f for f in facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
              .get("units", {}).get(SHARES, [])]
    shares_line = "not tagged on the cover page (e.g. several share classes); take it from the latest filing"
    if shares:
        s = max(shares, key=lambda f: (f["end"], f.get("filed", "")))
        shares_line = f"{s['val']:,} shares as of {s['end']} ({s.get('form')} filed {s.get('filed')}, accession {s.get('accn')})"

    today = date.today().isoformat()
    md = [
        f"# SEC data pack: {facts.get('entityName', company)} ({ticker})",
        "",
        f"Generated by the workflow on {today} directly from SEC EDGAR (XBRL \"company facts\" and \"submissions\" "
        "APIs). Deterministic: no model produced or edited any value.",
        "",
        f"- CIK: {cik}; SIC: {subs.get('sicDescription') or 'n/a'}; exchanges: {', '.join(subs.get('exchanges') or []) or 'n/a'}; "
        f"fiscal year end (MMDD): {subs.get('fiscalYearEnd') or 'n/a'}; state of incorporation: {subs.get('stateOfIncorporation') or 'n/a'}",
        f"- Common shares outstanding (cover page, FACT): {shares_line}",
        "",
        "## How to read this pack",
        "",
        "- FACT tables: values exactly as tagged in the company's annual reports (10-K). Money and share counts are in "
        "millions (USD millions; shares in millions); EPS is in USD per share. Columns are fiscal year-end dates.",
        "- Each row names its XBRL tag. Different tags are different line items as filed; they are never merged, and a "
        "tag can stop or start being used from one year to the next.",
        "- Where a later 10-K restated or recast a year, the latest-filed value is shown and marked †; the originally "
        "filed value and both accession numbers are in `financials.json` next to this file.",
        "- — means the company did not tag that item for that year: take it from the filing itself if needed.",
        "- Per-share values and share counts are as filed and are NOT adjusted for stock splits made after the "
        "filing: years that no 10-K re-filed after a split stay on the old share basis. Adjust for splits before "
        "comparing them across years.",
        *(f"- WARNING: {w}" for w in split_warnings),
        "- Debt tags differ in scope (for example, `LongTermDebt` can include current maturities while "
        "`LongTermDebtNoncurrent` excludes them): compose total debt from the balance sheet, not by adding rows blindly.",
        "- Not covered: share price and market multiples (P/E, P/B), segment data, non-GAAP measures, quarterly/TTM "
        "figures, and anything not tagged in XBRL. Use the filings linked below and other sources as usual.",
        "",
        "## Recent filings (primary documents)",
        "",
        *(_recent_filings(cik, subs) or ["- none found"]),
        "",
        "## Annual financial data (FACT, from 10-K XBRL)",
        "",
        "\n\n".join(sections),
        "",
        f"Items with no XBRL data for this company: {', '.join(missing) if missing else 'none'}.",
        "",
        "## Reference calculations (CALCULATION)",
        "",
        "Computed by the workflow from the FACT values above, with the formula stated on each row (USD millions or "
        "percent). Each input uses one XBRL tag for every year (listed below). Operating income after taxes follows "
        "the definition in the valuation reference file, using each year's effective tax rate as reported; if your "
        "analysis uses a different tax rate, state which one and why. These are unadjusted: normalization (for "
        "example non-recurring items) and owner earnings remain the analysts' work.",
        "",
        _table(["Calculation"] + ends, calc_rows),
        "",
        "Inputs used (tag and fiscal years):",
        *calc_notes,
        "",
        "## Sources (10-K filings that supplied each fiscal year's values)",
        "",
        f"Accession numbers; filing index: https://www.sec.gov/Archives/edgar/data/{cik}/<accession without dashes>/",
        "",
        *src_lines,
        "",
    ]
    if restated:
        md.insert(md.index("## How to read this pack"), "Note: at least one value is marked † (restated or recast).\n")
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.data_pack.write_text("\n".join(md), encoding="utf-8")
    (paths.data_dir / "financials.json").write_text(json.dumps({
        "generated": datetime.now().isoformat(timespec="seconds"), "cik": cik, "ticker": ticker,
        "entity": facts.get("entityName"), "fiscal_year_ends": ends, "facts": series,
        "calculations": calc,
    }, indent=1), encoding="utf-8")
    return f"SEC XBRL data for {ticker} (CIK {cik}), {len(ends)} fiscal years {ends[0]}..{ends[-1]}"
