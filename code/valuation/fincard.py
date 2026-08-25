#!/usr/bin/env python3
"""
Financial card — codified digits-by-date for any ticker. PURE CODE, zero models.

WHY (David, 2026-08-12): every number a decision uses comes from code, dated and
sourced, never from a model's working memory. Both arithmetic incidents happened
in hand-derivation (TRIP's $824M net-cash error, LYFT's CFO-mislabeled-as-FCF).
Deep-numbers doctrine (same day): as many first-hand figures as possible on the
card, full period series retained, so any complicated figure is ONE CODED
EXPRESSION away (see query.py) — the PM writes code, never raw arithmetic.

fincard.json:
  figures   headline value per concept, duration-checked. TTM built from 4
            verified contiguous quarters — including quarters DERIVED by
            YTD-differencing (10-Q cash-flow statements report YTD; Q_n =
            YTD_n - YTD_{n-1}, flagged "derived") — else honestly-labeled FY.
            Every figure: value, unit, period|asof, tag, form, filed.
  series    per-concept history (last 12 quarters + 8 fiscal years) so trends
            and custom aggregates are computable without re-fetching EDGAR.
  derived   net cash, EV, FCF, margins, returns, leverage, dilution, yields —
            every value ships WITH its formula and inputs inline.
  valuation MECHANICAL ruler: reverse-DCF implied growth + DCF/share grid at
            fixed assumptions (10y, 10% discount, 2.5% terminal). Never a thesis.
  cross_checks  computed market cap vs Finnhub's (catches share-class/unit
            errors — multi-class issuers under-report dei shares); flagged >10%.
  flags     everything missing, mixed-period, or upper-bound — part of the number.

Used by BOTH books: agent dossiers (dossier.py -> names/<TK>/fincard.json) and
research-book evidence packs (evidence.py -> _evidence/fincard.json).
CLI: fincard.py TICKER [--cik N] [--out FILE]
"""
import datetime as dt
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from edgar_identity import UA  # SEC contact identity, config-driven

# concepts measured over a period; unit defaults to USD unless noted
FLOW = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    # CostDirectMaterial last: RRGB (Red Robin, a restaurant operator) breaks its
    # "Restaurant operating costs" block into Cost of sales / Labor / Other operating
    # rather than one COGS line, and tags Cost of sales with this concept —
    # $150,686,000 for the 28wk YTD to 2026-07-12, consistent with a ~22% food-cost
    # ratio and continuous with FY2021-FY2025 annual filings (2026-08-20).
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfServices",
             "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
             "CostDirectMaterial"],
    "gross_profit": ["GrossProfit"],
    "rnd": ["ResearchAndDevelopmentExpense"],
    "sga": ["SellingGeneralAndAdministrativeExpense",
            "GeneralAndAdministrativeExpense"],
    "op_income": ["OperatingIncomeLoss"],
    # NOT read into op_income directly (CostsAndExpenses is a subtotal, not equivalent
    # in every issuer's statement layout) — used only as the op_income_calc fallback
    # below, for issuers whose income statement goes straight from this subtotal to
    # Other Income/Expense with no OperatingIncomeLoss line at all (CXW post its 2021
    # REIT->C-corp conversion: Revenue -> Costs and Expenses, Total -> Other Income/
    # Expense -> pretax income, confirmed against the statement face, 2026-08-18).
    "costs_and_expenses": ["CostsAndExpenses"],
    # InterestAndDebtExpense last: it is the same "cost of borrowing" line under a
    # different name (ETD prints "Interest and other financing costs" and tags it that
    # way; InterestExpense died at 2024-06-30 and the card flagged a retired tag).
    # NEVER add InterestIncomeExpenseNonoperatingNet / InterestIncomeExpenseNet here —
    # those are NET of interest income and carry the OPPOSITE sign (NCLH -336.9M,
    # SYY -512M); they would invert interest_coverage.
    # InterestExpenseBorrowings: WELL (a REIT) switched to this tag at 2026-03-31
    # ($192.715M) from InterestExpenseDebt (last point 2024-09-30, $419.79M for 9mo) —
    # same "cost of borrowing" line, continuous magnitude across the switch (2026-08-18).
    "interest_expense": ["InterestExpense", "InterestExpenseDebt",
                         "InterestExpenseNonoperating", "InterestExpenseOperating",
                         "InterestAndDebtExpense", "InterestExpenseBorrowings"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "tax": ["IncomeTaxExpenseBenefit"],
    # ProfitLoss LAST and only as a fallback: it INCLUDES noncontrolling interests
    # where NetIncomeLoss is parent-only — same precedence rule already used for
    # equity below. Issuers switch tags mid-life (ETD's NetIncomeLoss stops at
    # 2025-03-31; its 10-Q "Net income" 28,129 for the FY26 nine months is tagged
    # ProfitLoss, and ETD has no NCI so the two are identical). rows_for picks the
    # freshest tag, so where an issuer files both the parent-only tag still wins.
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],                     # USD/shares
    "shares_diluted_wavg": ["WeightedAverageNumberOfDilutedSharesOutstanding"],  # shares
    "cfo": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    # PaymentsForProceedsFromProductiveAssets: RYAM's successor to PaymentsToAcquire-
    # PropertyPlantAndEquipment (died 2021-12-31) — same cash-capex line, continuous
    # magnitude (~$95-116M/yr both sides of the switch, 2026-08-18).
    # PaymentsToAcquireOtherPropertyPlantAndEquipment: EE (Excelerate Energy, an LNG
    # company) tags its Cash Flow Statement "Purchases of property and equipment" line
    # with this concept instead of the plain PaymentsToAcquirePropertyPlantAndEquipment
    # tag — $283.833M at 2026-06-30 (6mo YTD, 10-Q filed 2026-08-06), consistent with
    # PP&E net growing ~$225M over the same half (2026-08-20).
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquirePropertyAndEquipment",
              "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements",
              "PaymentsToDevelopRealEstateAssets", "PaymentsForProceedsFromProductiveAssets",
              "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
    # SEPARATE cash-flow line from "capex" above, not an alternate tag for it — capitalized
    # software development is its own investing-activities caption, landing on the balance
    # sheet as an intangible, never PP&E. Software-heavy issuers routinely tag BOTH lines
    # (fincard.py-033, PM 2026-08-21): TLS tags PP&E additions AND PaymentsToDevelopSoftware
    # separately in the same 10-Q, and the issuer's OWN "Free Cash Flow" press-release figure
    # only reconciles once both are subtracted from CFO — a card that kept PP&E-only capex
    # read 17.99% FCF margin against TLS's reported 13.9%. Combined additively with "capex"
    # in the fcf build below (see the capex_sw handling there), never treated as an alternate
    # tag NAME for the same line the way FLOW_MERGE gap-fills capex's own PP&E alternates.
    "capex_software": ["PaymentsToDevelopSoftware", "CapitalizedComputerSoftwareAdditions"],
    # CostDepreciationAmortizationAndDepletion (MTX) and CostOfGoodsAndServicesSold-
    # DepreciationAndAmortization (PEG, a regulated utility's "Cost, Depreciation and
    # Amortization" line) are the same D&A figure filed under a cost-statement caption
    # instead of a standalone D&A line; Depreciation LAST and only once amortization has
    # gone to zero (FTK fully wrote off goodwill/intangibles by 2021 — nothing left to
    # amortize, so the company now tags pure depreciation and that alone equals D&A for
    # this issuer). Measured 2026-08-18.
    "dna": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
            "CostDepreciationAmortizationAndDepletion",
            "CostOfGoodsAndServicesSoldDepreciationAndAmortization", "Depreciation"],
    # AllocatedShareBasedCompensationExpense is the income-statement total for the same
    # expense the cash-flow add-back reports; issuers that stop tagging the add-back keep
    # tagging this one (ARES 386.4M, DAN 19M, FLEX 51M all current at 2026-06 while
    # ShareBasedCompensation sat at 2011-2014 values and read as a retired tag).
    # AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriod-
    # RecognitionValue last: ALGT tags SBC only in its equity-roll-forward statement
    # ("Share-based compensation" line, $13.529M at 2026-06-30) once it stopped tagging
    # ShareBasedCompensation quarterly after 2019-Q3 (2026-08-18).
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense",
            "AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "acquisitions": ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
}
FLOW_UNITS = {"eps_diluted": "USD/shares", "shares_diluted_wavg": "shares"}
# STOCK measures reported per period (a weighted-average count) — never additive.
# TTM is the AVERAGE of its periods, and a quarter may NOT be derived by
# YTD-differencing: Q4 = FY_wavg - 9M_wavg is arithmetic nonsense (it printed ARI
# 410,541,977 diluted shares against 128.2M actual — BOOK.md fincard defect (c),
# 2026-08-13). Summing 4x'd every per-share denominator built off the card.
FLOW_AVG = {"shares_diluted_wavg"}
# concepts whose alternate tags are the SAME reported line under different tag
# names, so rows may be MERGED across tags to fill period gaps (the freshest tag
# still wins any period it reports). capex only: issuers split one "purchases of
# property and equipment" line across tags by form type — SONO tags its 10-Qs
# PaymentsToAcquirePropertyPlantAndEquipment and its 10-Ks PaymentsToAcquire-
# ProductiveAssets, so no TTM ever assembled and a FY2023 figure was served as
# current for 1001 days (2026-08-14). NEVER merge concepts whose alternates differ
# in DEFINITION (sga: SG&A vs G&A; dividends_paid: common-only vs total).
FLOW_MERGE = {"capex", "capex_software"}
# balance-sheet points in time
INSTANT = {
    # the RESTRICTED-inclusive tag stays LAST, and the two narrower balance-sheet tags
    # come first, because rows_for breaks ties on first-listed: QVCG (in Chapter 11) tags
    # its balance-sheet "Cash and cash equivalents" 1,019M as CashEquivalentsAtCarryingValue
    # and its restricted 493M separately, so the combined tag served 1,512M as "cash" into
    # net cash and EV — $493M of restricted money the company cannot spend (2026-08-18).
    # PFGC's balance sheet line is captioned and tagged plain "Cash" (92.4M at 2026-06-27)
    # while CashCashEquivalentsRestricted... died in 2019 and read as a retired tag.
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "Cash", "CashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    # AFS alternates come LAST on purpose: rows_for breaks ties on first-listed, and
    # AvailableForSaleSecuritiesDebtSecurities is the TOTAL AFS portfolio (current +
    # noncurrent + restricted). It is the right answer only where the issuer has no
    # cleaner tag — KLAC classifies its whole $3.21B portfolio as current and tags
    # nothing else, so st_investments sat at a 2018 value and net cash understated by
    # $3.2B (2026-08-15). Where a precise tag exists it must win: LYFT tags
    # ShortTermInvestments 657M and AFS-total 1,962M on the SAME date, and the extra
    # 1.3B is RESTRICTED investments backing insurance reserves — never net-cash money.
    # DebtSecuritiesAvailableForSaleExcludingAccruedInterest* are the post-ASU-2016-13
    # renames of the AvailableForSaleSecuritiesDebtSecurities* tags — the SAME line, and
    # issuers migrate without warning (XYZ's "Investments in short-term debt securities"
    # 310,845K at 2026-06-30 is tagged the new way while the old tag stopped in 2023-06,
    # so st_investments read STALE and net cash lost $311M — 2026-08-18). Precise
    # current/noncurrent tags stay ahead of the AFS TOTAL fallback for the KLAC/LYFT
    # reason above.
    "st_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                       "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                       "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
                       "AvailableForSaleSecuritiesDebtSecurities"],
    "lt_investments": ["LongTermInvestments", "MarketableSecuritiesNoncurrent",
                       "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
                       "DebtSecuritiesAvailableForSaleExcludingAccruedInterestNoncurrent"],
    # SYY captions the line "Accounts receivable, less allowances" (5,755M at 2026-03-28)
    # and tags it AccountsNotesAndLoansReceivableNetCurrent; ReceivablesNetCurrent last
    # filed in 2011 and read as a retired tag.
    # AccountsAndOtherReceivablesNetCurrent last: OLN and VHI both merged AR into a
    # combined "accounts and other receivables" line (OLN $988.6M at 2026-06-30, VHI
    # $434.2M at 2026-06-30) after their split AR/other tags went stale (2026-08-18).
    "receivables": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
                    "AccountsNotesAndLoansReceivableNetCurrent",
                    "AccountsAndOtherReceivablesNetCurrent"],
    # SYY tags its single "Inventories" line (5,291M at 2026-03-28) with the finished-goods
    # tag; InventoryNet stops in 2011. Listed after InventoryNet so an issuer filing both
    # the total and the component keeps the TOTAL.
    # InventoryRawMaterialsAndSupplies last: PEG (a regulated utility) has no finished-
    # goods inventory — its balance-sheet "Materials and supplies" line is the utility
    # equivalent (fuel/spare-parts stock), $868M at 2026-06-30 (2026-08-18).
    "inventory": ["InventoryNet", "InventoryFinishedGoodsNetOfReserves",
                 "InventoryRawMaterialsAndSupplies"],
    "current_assets": ["AssetsCurrent"],
    # PublicUtilitiesPropertyPlantAndEquipmentNet last: PEG's balance sheet has never
    # used the generic industrial PP&E tag — it captions the line "Property, plant and
    # equipment, net" but tags it with the utility-specific concept, $42.931B at
    # 2026-06-30 (2026-08-18).
    "ppe_net": ["PropertyPlantAndEquipmentNet",
                "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
                "PublicUtilitiesPropertyPlantAndEquipmentNet"],
    "goodwill": ["Goodwill"],
    "intangibles": ["FiniteLivedIntangibleAssetsNet", "IntangibleAssetsNetExcludingGoodwill"],
    "total_assets": ["Assets"],
    "current_liabilities": ["LiabilitiesCurrent"],
    # LongTermLineOfCredit last: a drawn revolver IS balance-sheet debt, and a name that
    # has only ever borrowed on its revolver tags nothing else — YELP drew $100M in
    # Q2-2026 while the card's freshest debt tag was a 2016 term loan, so net cash
    # overstated by $100M and the card called itself UNRELIABLE for the wrong reason
    # (2026-08-15). Listed after the totals so an issuer that tags both (ICLR: both
    # 2,111M on 2026-06-30) keeps the TOTAL, never the revolver component alone.
    # OtherLongTermDebt{Noncurrent,Current} last: they are "other" components by
    # definition, but an issuer with one debt caption may tag the WHOLE line that way —
    # XYZ's balance sheet prints "Current portion of long-term debt 0" and "Long-term debt
    # 5,720,569" at 2026-06-30 tagged exactly so, while LongTermDebtCurrent stopped in
    # 2022 and left the card shouting NET CASH UNRELIABLE over a $460M ghost (2026-08-18).
    # Totals stay first so an issuer filing both keeps the total, never the component.
    # FinanceLeaseLiability{Noncurrent,Current} last: a standard us-gaap tag for finance-
    # lease debt, previously absent from this map entirely — TLS (HELD) tags ONLY its
    # finance leases (2,113,000 current + 4,536,000 noncurrent at 2026-06-30) with no other
    # debt concept, so the gap was invisible on the card and then laundered into
    # "PROVEN ZERO" by _zero_proof, since the finance leases sit inside total_liabilities
    # and the balance sheet foots without them (fincard.py-022, 2026-08-20).
    # LongTermNotesPayable / ConvertibleDebtNoncurrent / NotesPayableCurrent /
    # NotesPayableToBankCurrent added 2026-08-25 (quality.py-043, Class B NET CASH
    # UNRELIABLE sweep) — all four verified against the issuer's own footing balance sheet
    # at its 2026-06-30 date before adopting (assets - equity = stated liabilities, exact,
    # in every case): SMID tags its only debt as LongTermNotesPayable 3,475,000 (noncurrent)
    # + NotesPayableCurrent 661,000 (current), reported every quarter with a consistent
    # paydown trend — the prior debt_lt/debt_current tags (FinanceLeaseLiability*) went
    # stale in 2021 when SMID retired its lease and switched to this note. LAB's only debt is
    # ConvertibleDebtNoncurrent 299,000, the same dollar figure the retired LongTermDebt tag
    # last carried at 2024-12-31 — a straight retag, not a new liability. CRTO tags
    # NotesPayableCurrent 5,688,000 (current only; no noncurrent debt reported since 2020,
    # left as debt_lt UNKNOWN rather than guessed zero). BDSX tags NotesPayableToBankCurrent
    # 0 at 2026-06-30, corroborated by its own maturity schedule showing the entire $50M term
    # loan due in year two — a verified zero, not an absent tag.
    # UnsecuredLongTermDebt / LinesOfCreditCurrent added 2026-08-25 (quality.py-043): CDNS
    # (Cadence Design Systems) ties UnsecuredLongTermDebt 2,481,170,000 exactly to
    # DebtInstrumentFaceAmount 2,500,000,000 - unamortized discount/issuance-costs 18,830,000
    # at 2026-03-31 (debt_lt was reading nothing at all — no prior alternate matched); its
    # newly-drawn revolver is tagged LinesOfCreditCurrent 425,000,000 at the same date
    # (zero the three prior quarters, then drawn — genuinely current, not a stale figure).
    # ICLR ties the same LinesOfCreditCurrent tag (1,279,762,000 at 2026-06-30) to the
    # issuer's own combined DebtInstrumentCarryingAmount to the dollar once summed with its
    # already-correct debt_lt and unamortized discount. NotesPayable last, LOWEST priority
    # (only wins when nothing else reports fresher): PRI's entire disclosed debt is
    # NotesPayable 595,716,000 at 2026-06-30 — the prior debt_lt pick was a stale/trivial
    # FinanceLeaseLiability figure understating real debt by ~$595M. NotesPayable is
    # normally a Note-level combined figure (see RESCUE_VETO caution elsewhere in this
    # file) so it is intentionally ordered last — it only replaces a tag that is itself
    # stale or absent, never a fresher, cleaner current/noncurrent split.
    "debt_lt": ["LongTermDebtNoncurrent", "LongTermDebt",
                "LongTermDebtAndCapitalLeaseObligations", "LongTermLineOfCredit",
                "OtherLongTermDebtNoncurrent", "FinanceLeaseLiabilityNoncurrent",
                "FinanceLeaseLiability", "LongTermNotesPayable", "ConvertibleDebtNoncurrent",
                "UnsecuredLongTermDebt", "NotesPayable"],
    "debt_current": ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings",
                     "LongTermDebtAndCapitalLeaseObligationsCurrent",
                     "OtherLongTermDebtCurrent", "FinanceLeaseLiabilityCurrent",
                     "NotesPayableCurrent", "NotesPayableToBankCurrent", "LinesOfCreditCurrent"],
    "operating_lease_liab": ["OperatingLeaseLiability"],
    "total_liabilities": ["Liabilities"],
    # PartnersCapital{,IncludingPortionAttributableToNoncontrollingInterest} last: an LP
    # (GEL — Genesis Energy LP) has no "stockholders'" equity at all, corporate-only
    # StockholdersEquity tags stopped in 2015, and the parent-only PartnersCapital tag
    # is ALSO stale (2015) while the NCI-inclusive one is current ($127.68M at
    # 2026-06-30) — same parent-vs-NCI precedence as the StockholdersEquity pair above,
    # just for the partnership form (2026-08-18).
    # MembersEquity last: a cooperative/LLC has no "stockholders'" or "partners'" equity
    # at all — GGROU (Golden Growers Cooperative) tags its balance sheet's "Total
    # members' equity" line this way, $15,865,000 at 2026-06-30, ties to the penny
    # against Assets - Liabilities (2026-08-20). Same precedence idea as PartnersCapital
    # above: a different legal form's equivalent concept, not a subset of equity.
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
               "PartnersCapital", "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
               "MembersEquity"],
}
# totals some issuers tag only ANNUALLY while tagging the current/noncurrent split
# every quarter — the total then looks like a retired tag (SONO OperatingLease-
# Liability: 10-K-only, "STALE 273d"; DXC/RIG debt sat behind the same pattern).
# Used only when every component is present at the SAME date AND that date beats
# the single-tag pick. Sum of components, never a partial sum.
INSTANT_SUM = {
    "operating_lease_liab": ["OperatingLeaseLiabilityCurrent",
                             "OperatingLeaseLiabilityNoncurrent"],
    # LW (Lamb Weston): the consolidated `Liabilities` tag stopped 2016-11-27 (STALE, 3472d,
    # quality.py-043 2026-08-25) while LiabilitiesCurrent and LiabilitiesNoncurrent both
    # report every quarter (1,415.5M + 4,139.7M = 5,555.2M at 2026-05-31, 10-K) — the two
    # components an issuer keeps tagging quarterly even after retiring the combined total,
    # same shape as operating_lease_liab above.
    "total_liabilities": ["LiabilitiesCurrent", "LiabilitiesNoncurrent"],
}
# PM-verified figures for lines an issuer reports ONLY in the printed statement.
# Applied in build() and never allowed to beat a real XBRL tag. Every entry needs a
# verbatim quote and the document it came from, because this dict is the one place
# on the card where a number is not machine-derived — which is exactly where a
# fabricated figure would hide. Re-key after each new filing; a manual TTM goes
# stale silently where an XBRL one would not.
MANUAL = {
    "LYFT": {
        # LYFT tags no cash-capex concept (companyfacts has none of the FLOW["capex"]
        # tags; CapitalizedComputerSoftwareAdditions = 0). The line is printed in
        # every cash-flow statement and the company uses it for its own FCF measure:
        # "We define free cash flow as net cash provided by (used in) operating
        # activities less purchases of property and equipment and scooter fleet."
        "capex": {
            "value": 82_754_000,
            "period": "TTM 2025-07-01..2026-06-30 (FY25 52,822 + H1'26 50,718 - H1'25 20,786)",
            "period_end": "2026-06-30",
            "formula": "FY2025 52,822,000 + H1-2026 50,718,000 - H1-2025 20,786,000",
            "quote": "Purchases of property and equipment and scooter fleet ( 50,718 ) ( 20,786 )",
            "doc": "10-Q filed 2026-08-07 (H1 legs) + 10-K filed 2026-02-11 ('( 52,822 )') — cash-flow statements",
            "entered": "2026-08-14",
        },
    },
    "INSG": {
        # INSG (Inseego) carries two distinct, non-overlapping long-term debt lines at
        # 2026-06-30: a drawn revolver (LongTermLineOfCredit) and secured notes
        # (SecuredLongTermDebt). Our tag map can only pick ONE freshest tag per concept —
        # summing them here (rather than adding a second alternate that would silently
        # replace, not add to, the first) avoids understating debt_lt by whichever leg loses
        # the pick (quality.py-043, 2026-08-25).
        "debt_lt": {
            "value": 60_291_000,
            "period": "instant 2026-06-30",
            "period_end": "2026-06-30",
            "formula": "Working Capital Facility 10,000,000 + 2029 Senior Secured Notes, net 50,291,000",
            "quote": "Working Capital Facility 10,000 0 ... 2029 Senior Secured Notes, net 50,291 41,611 "
                     "(balance sheet, $ in Thousands)",
            "doc": "10-Q filed 2026-08-06 (period 2026-06-30) — condensed consolidated balance sheet",
            "entered": "2026-08-25",
        },
    },
    "MDGL": {
        # MDGL tags Cost of sales with the standard us-gaap:CostOfGoodsAndServicesSold
        # concept (already in FLOW["cogs"]) but ONLY under
        # StatementBusinessSegmentsAxis=mdgl:ReportableSegmentMember — MDGL has exactly
        # one reportable segment, so the segment figure equals the consolidated figure,
        # but companyfacts drops every dimensional fact regardless (same mechanism as
        # the ARI/LYFT extension-tag drops, just a dimension instead of a namespace),
        # which is why the companyfacts pull reads this concept as retired at
        # 2025-03-31 ($6,233K) while the face of the income statement carries it
        # current every quarter. Confirmed by reading ix:nonFraction tags directly off
        # the filed 10-Q/10-K (companyfacts never serves this fact at all).
        "cogs": {
            "value": 109_424_000,
            "period": "TTM 2025-07-01..2026-06-30 (FY2025 56,148 + H1'26 66,854 - H1'25 13,578)",
            "period_end": "2026-06-30",
            "formula": "FY2025 56,148,000 + H1-2026 66,854,000 - H1-2025 13,578,000",
            "quote": "Cost of sales 40,007 9,065 66,854 13,578 (10-Q, Q2/H1 2026 vs 2025); "
                     "Cost of sales 56,148 6,233 (10-K, FY2025 vs FY2024)",
            "doc": "10-Q filed 2026-07-30 (H1'26/H1'25 legs) + 10-K filed 2026-02-19 (FY2025 total) "
                   "— income statements",
            "entered": "2026-08-19",
        },
    },
}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        data = json.loads(r.read())
    time.sleep(0.15)
    return data


def resolve_cik(tk, override=None):
    if override:
        return str(override).zfill(10)
    cache = ENGINE / "agent" / "data" / "cik_map.json"
    try:
        m = json.loads(cache.read_text())
    except Exception:
        raw = _get("https://www.sec.gov/files/company_tickers.json")
        m = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in raw.values()}
    # The EXACT ticker is always tried, whatever its length. The `>= 2` guard below exists
    # for the DERIVED fallbacks (a class-share guess like tk[:-1] on a 2-letter ticker is a
    # 1-character shot in the dark), and applying it to the exact ticker too meant no
    # one-letter ticker could ever resolve. `L` is Loews Corp, CIK 60086, an S&P 500 name
    # the Bench surfaced with evidence — it failed to card every night, silently, as
    # "no CIK".
    if m.get(tk):
        return m[tk]
    for cand in (tk[:-1] + "A", tk[:-1] + "K", tk[:-1] + "B", tk[:-1]):
        if len(cand) >= 2 and m.get(cand):
            return m[cand]
    raise SystemExit(f"{tk}: no CIK (pass --cik N)")


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def _pick_flow(entries, derive=True):
    """Raw XBRL duration entries -> (quarters, annuals), deduped by end date,
    latest-filed restatement wins. Quarters include values DERIVED from YTD
    differences (10-Q cash-flow statements report YTD: Q_n = YTD_n - YTD_{n-1});
    derive=False for stock measures (FLOW_AVG) where that subtraction is meaningless."""
    direct_q, ytd, fy = {}, {}, {}
    for e in entries:
        s, en = e.get("start"), e.get("end")
        if not s or not en or e.get("val") is None:
            continue
        d = _days(s, en)
        row = {"value": e["val"], "start": s, "end": en,
               "form": e.get("form"), "filed": e.get("filed")}
        if 75 <= d <= 100:
            if en not in direct_q or (e.get("filed") or "") > (direct_q[en].get("filed") or ""):
                direct_q[en] = row
        elif 350 <= d <= 380:
            if en not in fy or (e.get("filed") or "") > (fy[en].get("filed") or ""):
                fy[en] = row
        elif 160 <= d <= 290:  # 6- or 9-month YTD
            key = (s, en)
            if key not in ytd or (e.get("filed") or "") > (ytd[key].get("filed") or ""):
                ytd[key] = row
    # derive quarters from YTD chains sharing a fiscal-year start. The FY row joins
    # its chain too: Q4 = FY - 9-month YTD (without it, TTM never assembles for
    # issuers whose 10-Q cash-flow statements are YTD-only, e.g. LYFT).
    chains = {}
    for (s, en), row in ytd.items():
        chains.setdefault(s, []).append(row)
    for s, rows in (chains.items() if derive else ()):
        seq = sorted(rows + [r for r in direct_q.values() if r["start"] == s]
                     + [r for r in fy.values() if r["start"] == s],
                     key=lambda r: r["end"])
        for i in range(1, len(seq)):
            gap = _days(seq[i - 1]["end"], seq[i]["end"])
            if 75 <= gap <= 100 and seq[i]["end"] not in direct_q:
                direct_q[seq[i]["end"]] = {
                    "value": seq[i]["value"] - seq[i - 1]["value"],
                    "start": (dt.date.fromisoformat(seq[i - 1]["end"]) + dt.timedelta(days=1)).isoformat(),
                    "end": seq[i]["end"], "form": seq[i].get("form"),
                    "filed": seq[i].get("filed"), "derived": "ytd-diff"}
    return sorted(direct_q.values(), key=lambda r: r["end"], reverse=True), \
        sorted(fy.values(), key=lambda r: r["end"], reverse=True), \
        sorted(ytd.values(), key=lambda r: r["end"], reverse=True)


def _contig(qs):
    return all(abs(_days(qs[i + 1]["end"], qs[i]["start"])) <= 6 for i in range(len(qs) - 1))


def _ttm(quarters, annuals, ytd=None, mode="sum"):
    if mode == "avg" and quarters:
        # stock measure (a weighted-average count): average the most recent contiguous
        # run of directly-reported quarters — never a sum, and never fewer than the
        # freshest annual can offer. 10-Ks print no Q4 column for these, so a clean
        # 4-quarter run rarely exists; 2 or 3 current quarters beat a year-old FY average.
        run = [quarters[0]]
        for q in quarters[1:4]:
            if abs(_days(q["end"], run[-1]["start"])) <= 6:
                run.append(q)
            else:
                break
        if not annuals or run[0]["end"] > annuals[0]["end"]:
            return (sum(x["value"] for x in run) / len(run),
                    f"avg of {len(run)} direct quarter(s) {run[-1]['start']}..{run[0]['end']}",
                    run[0]["end"], run[-1]["start"])
    q_result = None
    if len(quarters) >= 4:
        qs = quarters[:4]
        if _contig(qs) and 350 <= _days(qs[3]["start"], qs[0]["end"]) <= 380:
            how = "incl. ytd-diff derived" if any(q.get("derived") for q in qs) else "4 direct 10-Q quarters"
            q_result = (sum(x["value"] for x in qs), f"TTM {qs[3]['start']}..{qs[0]['end']} ({how})",
                        qs[0]["end"], qs[3]["start"])
    a_result = None
    if annuals:
        a = annuals[0]
        a_result = (a["value"], f"FY {a['start']}..{a['end']} (no verified TTM — annual used)",
                    a["end"], a["start"])
    y_result = None
    if ytd:
        # newly-registered issuer: only one YTD period on file, no prior quarters/FY to
        # build a TTM or even a clean quarter from — carry the YTD figure honestly labeled
        # rather than drop it (MBGL: single 10-Q on file, caught by fincheck 2026-08-13)
        y = ytd[0]
        y_result = (y["value"], f"YTD {y['start']}..{y['end']} (single period on file — no TTM/FY yet)",
                    y["end"], y["start"])
    # None of these three shapes is automatically the FRESHEST answer — an issuer that
    # switches disclosure cadence leaves an old-but-structurally-valid candidate sitting
    # next to fresher data in a shape the code used to rank below it. LEU's sbc: 4
    # contiguous quarters exist but stop in 2017, 8 years behind the FY2025 annual.
    # ALGT's sbc: the only FY-duration filing of this tag is from 2020 (this tag is a
    # 10-Q-only equity-rollforward line — no fresher annual will EVER exist), while
    # 2026-06-30 YTD data sat right there, ranked last by the old fixed priority order.
    # Listed in this order so a tie on end-date still prefers the higher-quality shape
    # (a verified 4-quarter TTM over a single annual over a partial-year YTD).
    candidates = [c for c in (q_result, a_result, y_result) if c is not None]
    if not candidates:
        return None, None, None, None
    return max(candidates, key=lambda c: c[2])


def _prior_ttm(quarters, annuals, mode="sum"):
    if len(quarters) >= 8 and _contig(quarters[4:8]):
        tot = sum(x["value"] for x in quarters[4:8])
        if mode == "avg":
            return tot / 4.0, f"avg of 4 quarters to {quarters[4]['end']}"
        return tot, f"TTM to {quarters[4]['end']}"
    if len(annuals) >= 2:
        return annuals[1]["value"], f"FY to {annuals[1]['end']}"
    return None, None


def _price(tk):
    try:
        key = json.loads((ENGINE / "config" / "keys.json").read_text()).get("finnhub", "")
        with urllib.request.urlopen(
                f"https://finnhub.io/api/v1/quote?symbol={tk}&token={key}", timeout=15) as r:
            return json.loads(r.read()).get("c") or None
    except Exception:
        return None


def _cik_tickers(cik):
    """Every ticker SEC has ever registered under this CIK (submissions API), primary
    (common) first. [] on any failure — never blocks the card. Used to catch the FTAIM
    class of error (quality.py-043, 2026-08-25): FTAI Aviation Ltd. registers FIVE tickers
    at one CIK — FTAI (common) plus FTAIM/FTAIN/FTAIO/FTAIP (four preferred series) — and
    dei:EntityCommonStockSharesOutstanding is COMMON-only. Multiplying the parent's common
    share count by a preferred ticker's quote (FTAIM $27.42 x 102.7M common shares) is a
    type error, not a data-quality gap: it computed $2.82B against Finnhub's real $21.54B
    common-stock market cap, a 7.6x miss the "multi-class shares" flag text could not
    explain because there is no multi-class dei split to rescue here at all."""
    try:
        return _get(f"https://data.sec.gov/submissions/CIK{cik}.json").get("tickers") or []
    except Exception:
        return []


def _finnhub_mktcap(tk):
    """SECOND-SOURCE CROSS-CHECK ONLY, never a figure source (first-hand doctrine)."""
    try:
        key = json.loads((ENGINE / "config" / "keys.json").read_text()).get("finnhub", "")
        with urllib.request.urlopen(
                f"https://finnhub.io/api/v1/stock/profile2?symbol={tk}&token={key}", timeout=15) as r:
            v = json.loads(r.read()).get("marketCapitalization")
        return v * 1e6 if v else None  # Finnhub reports $M
    except Exception:
        return None


# --------------------------------------------------------------------- flag triage
# 2026-08-18. The unknowns register carried 161 NEEDS-KEY rows and 104 of them were STALE
# flags with a MEDIAN of 1,734 days behind. Read by class rather than by count: a
# BALANCE-SHEET line that goes stale is a real defect — the issuer retagged it, or it went
# to zero and we print "stale" instead of 0 (the ARI debt_lt and HUT $7.6B class). But a
# FLOW that stops being reported usually means THE COMPANY STOPPED DOING IT. `acquisitions`
# alone raised 21 flags, on a concept no derived value reads, and every one of them meant
# "has not bought anything recently".
#
# Flagging both the same way is worse than not flagging: 28 rows of guaranteed noise train
# the reader to skim a list whose entire value is that it is short. The episodic flows below
# stay excluded from derived values (that part was never wrong) and are recorded as
# `not_reported` rather than raising a flag.
#
# Deliberately NARROW. A concept belongs here only if a healthy company can legitimately
# report nothing for it, period after period. Everything else keeps flagging loudly.
EPISODIC_FLOWS = {
    "acquisitions",     # no deal this year is the normal case, not a data defect
    "buybacks",         # a company can simply not repurchase
    "dividends_paid",   # non-payers report nothing, forever
}
# concepts that exist ONLY as an internal fallback input for a derived value (never
# read directly, never shown as a headline figure) — a stale one must still be
# excluded from ttm_vals (an op_income_calc built on a 5-year-old CostsAndExpenses
# would be as wrong as using a stale op_income directly), but raising its own flag is
# pure noise for the ~85% of tickers that never touch it: costs_and_expenses matters
# only to CXW's op_income_calc fallback, and adding it to FLOW put a "STALE" flag on
# BKH/CIFR/FCELB/FIS/KLAC/LBRDP that had never used the concept before (2026-08-18).
# capex_software (fincard.py-033, 2026-08-21) is the same shape: it feeds the capex/fcf
# build only for issuers currently tagging it, but companyfacts keeps a dead data point
# for issuers that tagged it once and stopped (CDNS last 2021-01-02, GRPN 2012-06-30,
# FIS 2014-06-30, HGV 2017-12-31) — without this, adding the concept put a fresh "STALE"
# flag on all four the day it shipped, none of which had ever used it before either.
# The individual figure still carries its own STALE marker (build()'s quarantine loop
# sets F["capex_software"]["STALE"] before this set is even consulted) — only the
# card-level flags list, the one a human actually scans, stays quiet for issuers where
# it was never material.
AUX_ONLY_FLOWS = {"costs_and_expenses", "capex_software"}

# Concepts any US-GAAP filer must report. Their absence is a TAXONOMY problem (usually an
# IFRS filer) rather than a tag-mapping problem — see the check at the end of build().
# Balance-sheet and income-statement DETAIL that no derived value on any card consumes.
# Determined empirically from the `formula` string of every derived value across all 85
# cards, then checked by hand — the empirical pass alone is NOT enough: ebitda_approx writes
# "D&A" rather than "dna" and would have been wrongly listed here.
#
# Concepts that ARE consumed and therefore keep flagging loudly: interest_expense
# (interest_coverage), gross_profit (gross_margin_pct), dna (ebitda_approx), sbc
# (sbc_pct_revenue), total_assets and total_liabilities (the footing identity), and
# everything feeding net_cash / EV / FCF / BVPS.
#
# A stale `goodwill` tag changes no number this book acts on. Raising it to the same flag
# list as a stale `debt_lt` is how a flag list becomes wallpaper — 50 of 99 open rows on
# 2026-08-18 were exactly this. Recorded on the figure, left off the card's flags.
DISPLAY_ONLY = {
    "goodwill", "intangibles", "receivables", "inventory", "ppe_net", "operating_lease_liab",
    "lt_investments", "rnd", "sga", "acquisitions", "eps_diluted", "shares_diluted_wavg",
}

UNIVERSAL = ("revenue", "net_income", "cfo", "cash", "equity", "total_assets")



# ------------------------------------------------------- rescue from the filing itself
# companyfacts serves only the standard us-gaap dictionary and silently drops every number
# a company files under a label it invented. Measured 2026-08-18: ARI files 370 facts under
# `ari:`, ARES 732 under `ares:`, FC 70, LBRDP 77 — none of which reach us. ARI's card
# printed $1.24B of net cash against a true $868M because its $371,428,000 of debt lives in
# `ari:DebtRelatedToRealEstateOwnedHeldForInvestment`. The COO re-derived that by hand from
# the printed statement on 2026-08-16; it was machine-readable the whole time.
#
# The error is one-directional — an omitted liability always flatters — so this failure
# manufactures apparent net-cash bargains, which is exactly what this book screens for.
#
# So when a balance-sheet concept is missing or stale, go read the filing. Adoption is
# allowed ONLY when the period matches exactly and the answer is unambiguous; anything else
# keeps its flag and now NAMES the candidates instead of leaving the PM to find them.
# Keywords are matched at the START of the tag name, never anywhere inside it, and TOTALS
# are not rescuable at all.
#
# The first version of this table matched substrings and shipped five wrong numbers in one
# rebuild: `total_liabilities` keyed on "liabilities", so it adopted
# `vhi:EmployeeRelatedLiabilitiesNoncurrent` ($4.3M) and `peg:CustomerCollateralLiabilities`
# ($157M) AS TOTAL LIABILITIES — PEG's real total is ~$41.5B against $58.8B of assets, so
# that one was wrong by 264x. It passed the uniqueness test because a filing contains
# exactly one such consolidated fact at that date. Unique is not the same as right.
#
# A TOTAL can never be recovered by name-matching a component, so totals are gone from this
# table permanently. `goodwill` must not swallow `GoodwillAndIntangibleAssets`, which is a
# combined line and a different number.
RESCUE = {
    "debt_lt": ["debt", "longtermdebt", "borrowings", "notespayable", "loanspayable",
                "seniornotes"],
    "debt_current": ["debtcurrent", "shorttermborrowings", "currentportion",
                     "notespayablecurrent"],
    "st_investments": ["shortterminvestments", "marketablesecuritiescurrent"],
    "lt_investments": ["longterminvestments", "marketablesecuritiesnoncurrent"],
    "goodwill": ["goodwill"],
    "inventory": ["inventory", "inventories"],
}
# tag names that must never be adopted for a concept even if they match its prefix
RESCUE_VETO = {
    "goodwill": ("andintangible", "andother"),
    "debt_lt": ("issuancecost", "discount", "premium"),
}


def _plausible(name, value, F):
    """The accounting identity as a guard rail. A rescued figure has no human reviewing it,
    so it must survive arithmetic that a wrong number cannot.

    This is the check that would have caught the total_liabilities disaster: $157M against
    $58.8B of assets and $17.3B of equity fails on sight."""
    if value is None or value <= 0:
        return False, "non-positive"
    ta = (F.get("total_assets") or {}).get("value")
    tl = (F.get("total_liabilities") or {}).get("value")
    if name in ("debt_lt", "debt_current"):
        if tl and value > tl * 1.02:
            return False, f"{value:,.0f} exceeds total liabilities {tl:,.0f}"
        if ta and value > ta:
            return False, f"{value:,.0f} exceeds total assets {ta:,.0f}"
    if name in ("goodwill", "inventory", "st_investments", "lt_investments"):
        if ta and value > ta:
            return False, f"{value:,.0f} exceeds total assets {ta:,.0f}"
    return True, ""


# dei:EntityCommonStockSharesOutstanding is a MANDATORY cover-page tag every 10-Q/10-K
# carries, so a stale companyfacts value looks like a data gap but almost never is one:
# companyfacts serves only the NON-dimensional default-context fact, and a multi-class
# issuer tags shares outstanding PER CLASS with a dimension (ClassOfStockAxis or
# similar), which companyfacts drops entirely — the same drop-dimensional-facts
# behavior documented above for balance-sheet concepts, just on a dei tag instead of a
# us-gaap one. Measured 2026-08-18: VICR's last non-dimensional fact was 2015-04-24
# (26,978,949 shares) while its 2026-07-29 10-Q carries two dimensional facts totaling
# 46,106,732 — computed market cap corrected from a 0.63 Finnhub ratio to 1.07. Same
# shape confirmed for GEL (2014-02-24 -> current, ratio 0.73 -> 1.00), LEU (2022-03-01
# -> current, 0.65 -> 0.95), ATROB (2023-03-06 -> current, 0.75 -> 1.00), TBLA
# (2024-10-31 -> current, transitioned to dimensional reporting since).
def _shares_out_rescue(cik, tk):
    """Read the latest 10-Q/10-K's own XBRL instance for every
    EntityCommonStockSharesOutstanding fact (any dimension) at the freshest instant
    found. Returns (value, asof, n_classes) or None. Never raises.

    Summing every class is right for a genuine dual/multi-class COMMON structure
    (VICR/GEL/LEU/ATROB: each class carries identical economic rights, so total
    shares x one class's price IS the market cap) but wrong for an Up-C-style
    holding company where "classes" are structurally different instruments — ARES
    tags five dei figures (223.96M / 3.49M / 1,000 / 102.83M / 30M) and only the
    223.96M is the publicly-traded Class A; the other four are non-economic voting
    shares and AOG/LP-style units that do not price 1:1 with Class A. Summing all
    five overcorrected ARES's market-cap check from a 0.46 Finnhub ratio to 1.60 —
    WORSE than the stale figure it replaced (2026-08-18). Fetch Finnhub's own
    market-cap figure (second-source cross-check only, per doctrine) and pick
    whichever of {sum of all classes, largest class alone} lands closer — never
    assume the sum is right just because there is more than one class."""
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        import xbrlfacts as X
        acc, form, fdate, _doc = X.latest_filing(cik)
        if not acc:
            return None
        fs = X.facts(cik, acc)
    except Exception:
        return None
    hits = [f for f in fs if f.get("tag") == "EntityCommonStockSharesOutstanding"
            and f.get("instant") and f.get("number")]
    if not hits:
        return None
    newest = max(h["instant"] for h in hits)
    at_newest = [h for h in hits if h["instant"] == newest]
    n = len(at_newest)
    total = sum(h["number"] for h in at_newest)
    if n == 1:
        return total, newest, n
    largest = max(h["number"] for h in at_newest)
    px, fh_mc = _price(tk), _finnhub_mktcap(tk)
    if px and fh_mc:
        err_sum = abs(px * total - fh_mc) / fh_mc
        err_largest = abs(px * largest - fh_mc) / fh_mc
        if err_largest < err_sum:
            return largest, newest, 1
    return total, newest, n


def _rescue_instant(card, F, cik, wanted, asof):
    """Try the issuer's own filing for concepts companyfacts could not serve.
    Returns the number of concepts rescued. Never raises — a failed rescue leaves the
    existing flag exactly as it was."""
    if not wanted or not asof:
        return 0
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        import xbrlfacts as X
        acc, form, fdate, _doc = X.latest_filing(cik)
        if not acc:
            return 0
        fs = X.facts(cik, acc)
    except Exception as e:
        card["flags"].append(f"filing-rescue unavailable ({type(e).__name__}) — "
                             f"extension-tagged figures may be missing")
        return 0
    ext = X.namespaces(fs).get("extensions") or {}
    n = 0
    for name in wanted:
        kws = RESCUE.get(name)
        if not kws:
            continue
        uniq, cands = X.resolve_instant(fs, kws, asof)
        # prefix-anchored, and vetoed names dropped: "contains the word" is not a match
        def _ok_tag(f):
            t = f["tag"].lower()
            if any(v in t for v in RESCUE_VETO.get(name, ())):
                return False
            return any(t.startswith(k) for k in kws)
        cands = [c for c in cands if _ok_tag(c)]
        vals = {c["number"] for c in cands}
        uniq = cands[0] if (len(vals) == 1 and cands) else None
        if uniq is not None:
            ok, why = _plausible(name, uniq["number"], F)
            if not ok:
                card["flags"].append(
                    f"{name}: filing candidate REJECTED by the accounting identity — "
                    f"{uniq['ns']}:{uniq['tag']} = {uniq['number']:,.0f} ({why}). "
                    f"Left unresolved rather than adopted.")
                continue
        if uniq is not None:
            F[name] = {"value": uniq["number"], "unit": "USD", "asof": asof,
                       "tag": f"{uniq['ns']}:{uniq['tag']}", "form": form, "filed": fdate,
                       "source": "filing-extension",
                       "note": (f"NOT in companyfacts — read from the issuer's own filing "
                                f"({form} filed {fdate}, accession {acc}). companyfacts "
                                f"serves only us-gaap and drops the `{uniq['ns']}:` namespace. "
                                f"Period-matched to the {asof} balance sheet and consolidated "
                                f"(no segment dimensions).")}
            # the flag this rescue answers is no longer true
            card["flags"] = [f for f in card["flags"] if not f.startswith(f"{name}: ")]
            card.setdefault("rescued", []).append(
                {"concept": name, "tag": F[name]["tag"], "value": uniq["number"],
                 "asof": asof, "from": f"{form} {fdate}"})
            n += 1
        elif cands:
            vals = sorted({c["number"] for c in cands})
            card["flags"].append(
                f"{name}: AMBIGUOUS in the filing — {len(cands)} consolidated facts dated "
                f"{asof} with {len(vals)} distinct values "
                f"({', '.join(f'{v:,.0f}' for v in vals[:4])}); tags: "
                f"{', '.join(sorted({c['ns'] + ':' + c['tag'] for c in cands})[:3])}. "
                f"A person must pick one — do NOT guess.")
    if n:
        card.setdefault("_doc_rescue", f"{n} concept(s) recovered from the issuer's own "
                                       f"filing; extension namespaces present: {ext}")
    return n



# ------------------------------------------------------------------ the zero proof
# "Stale" and "zero" look identical from outside: a company that paid off its debt stops
# reporting the tag, exactly like a company that retagged it. We were printing STALE for
# both, and excluding the concept from every derived value — which understates leverage in
# the second case and is simply noise in the first.
#
# The balance sheet itself settles it, and this is the footing test the COO did by hand for
# ARI generalised: if ASSETS - EQUITY already equals STATED LIABILITIES, then every
# liability is accounted for by the lines we CAN see, so the one we cannot see is zero.
# If it does NOT balance, the gap IS the liability we are missing, and the flag must stay
# and say how big it is.
#
# Deliberately restricted to LIABILITY concepts. For an asset line the identity proves
# nothing — a missing goodwill just means it sits inside "other assets", and asserting
# zero there would understate the asset side. Understating our own net cash is the safe
# direction; understating debt is the direction that has already cost us twice.
ZERO_PROVABLE = ("debt_lt", "debt_current")

# EXHAUSTIVENESS — the second half of the zero-proof standard (pm-023/fincard.py-024,
# 2026-08-20). Completeness (the footing identity above) is NECESSARY but not sufficient:
# TLS's finance leases sat INSIDE total_liabilities, so the balance sheet foots cleanly
# without them and the identity alone still blesses a wrong zero. This regex asks a second,
# independent question of the SAME companyfacts blob already in memory (no extra HTTP call):
# does the issuer report ANY debt-like concept, non-dimensional, at the balance-sheet date,
# that our own tag maps never looked for? A hit means "you may not print PROVEN," never
# "here is the debt figure" — DebtInstrumentCarryingAmount/SeniorNotes/UnsecuredDebt/
# SecuredDebt are Note-level disclosure concepts that prove debt exists without giving the
# balance-sheet current/noncurrent split (WELL: debt_lt already correct, only debt_current
# open). Measured against the PM's 56-card probe (2026-08-20_pm_zeroproof_scan.py): 30 clean,
# 25 MISS, 1 mapped-only.
DEBT_LIKE_PATTERN = re.compile(
    r"(LongTermDebt|DebtCurrent|DebtNoncurrent|NotesPayable|LoansPayable|"
    r"FinanceLeaseLiability|CapitalLeaseObligation|ConvertibleNotesPayable|"
    r"ConvertibleDebt|LineOfCredit|SecuredDebt|UnsecuredDebt|SeniorNotes|"
    r"DebtInstrumentCarryingAmount|OtherBorrowings|ShortTermBorrowings|"
    r"BorrowingsUnder|BankOverdrafts|SubordinatedDebt|MortgageLoans)")
# concepts that match the pattern above by substring but are not a balance-sheet carrying
# liability. "IssuanceCosts" added here 2026-08-20: DebtIssuanceCostsLineOfCreditArrange-
# mentsNet is a CONTRA-ASSET (RRGB, confirmed false positive in the PM's own probe), not a
# liability — TARS carries the same concept alongside a real one, so this must be excluded
# rather than the whole pattern narrowed. Deliberately does NOT skip "Term" — that would
# also skip every LongTermDebt* tag, which is the concept this check exists to see.
DEBT_LIKE_SKIP = re.compile(
    r"(FairValue|InterestRate|Maturity|Percentage|Weighted|Number|"
    r"RightOfUseAsset|Payments|Proceeds|Repayments|Expense|Amortization|"
    r"Gain|Loss|Extinguish|Covenant|Remaining|Undiscounted|Description|"
    r"IssuanceCosts)")


_DEBT_TAG_MAP_CONCEPTS = set(INSTANT["debt_lt"]) | set(INSTANT["debt_current"])


def _debt_like_hits(gaap, asof, exclude=_DEBT_TAG_MAP_CONCEPTS):
    """Any non-dimensional, instant (not duration), nonzero USD fact at the
    balance-sheet date under a concept name that looks like a liability our own debt
    tag maps do not resolve. Returns a list of (concept, value) pairs, largest first.

    `exclude` defaults to every tag already IN our own debt_lt/debt_current alternates
    (e.g. LongTermDebt) — a concept already used to resolve the OTHER debt field on this
    same card is not an unmapped hit. Without this, MBGL's debt_lt (LongTermDebt,
    correctly resolved) was reported as an 'unmapped debt-like concept' when checking
    whether debt_current could be zero-proved, blocking a correct zero on a HELD name
    (caught in the fincard.py-024 blast-radius review, 2026-08-20)."""
    hits = []
    for concept, blob in gaap.items():
        if concept in exclude:
            continue
        if not DEBT_LIKE_PATTERN.search(concept) or DEBT_LIKE_SKIP.search(concept):
            continue
        for r in blob.get("units", {}).get("USD", []):
            if r.get("end") == asof and r.get("val") and not r.get("start"):
                hits.append((concept, r["val"]))
                break
    hits.sort(key=lambda h: -abs(h[1]))
    return hits


# noncontrolling-interest / mezzanine equity carried OUTSIDE parent-only StockholdersEquity
# but INSIDE the balance-sheet identity (assets = liabilities + NCI + temporary equity +
# parent equity). Fetched ONLY for the footing check below — never allowed to touch F["equity"],
# which must stay parent-only (same reason ProfitLoss stays a net_income fallback, never first:
# ROE/BVPS/price-to-book are shareholder-facing and NCI is not the shareholders' equity).
#
# Measured 2026-08-18 across 15 "BALANCE SHEET DOES NOT FOOT" flags: the gap matched this
# figure to the dollar for L (917.0M), DAN (63.0M), WBD (1,157.0M), GETY (48.244M),
# MTX (31.9M), HGV (156.0M), MAC (80.275M), QVCG (74.0M), SXC (27.9M), HUT (311.410M),
# LB (476.275M), ARES (4,634.2M, 0.1% residual), HY (20.0M, NCI + redeemable NCI both
# needed), FCELB (68.939M, NCI + temporary equity both needed) — every REIT/insurer/holdco
# with joint-venture or OP-unit noncontrolling interests, or a redeemable-preferred mezzanine
# line, was flagging a false "does not foot" because the identity was tested against
# shareholders' equity alone instead of total equity.
MEZZANINE_TAGS = ("MinorityInterest", "TemporaryEquityCarryingAmountAttributableToParent",
                  "RedeemableNoncontrollingInterestEquityCarryingAmount",
                  "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests")
# added 2026-08-20 (quality queue CMTL/ATNI footing breaks): the "Including..." variant is
# used when a filer reports temporary equity WITHOUT splitting out the NCI portion the way
# TemporaryEquityCarryingAmountAttributableToParent implies a split exists. Matched to the
# dollar at each issuer's own balance-sheet date: CMTL 210,783,000 (closes a 32.9% gap
# exactly), ATNI 97,393,000 on top of its already-summed MinorityInterest 120,524,000
# (120,524,000 + 97,393,000 = 217,917,000, the exact reported 17.0% gap).


def _mezzanine_equity(gaap, asof, parent_eq):
    """Sum of NCI/temporary-equity concepts reported AT the balance-sheet date. Read from
    the ALREADY-FETCHED companyfacts blob, not a fresh companyconcept call: the per-tag
    companyconcept endpoint served an empty units.USD for WELL's MinorityInterest (939.184M
    at 2026-03-31) while the very same figure sat right there in bulk companyfacts —
    an SEC API inconsistency, not a data gap (2026-08-18). Returns None (not 0) when
    nothing is found, so the caller can tell 'no mezzanine equity' from 'not present'."""
    total, found, got_minority = 0.0, False, False
    seen_vals = set()
    for tag in MEZZANINE_TAGS:
        rows = [r for r in gaap.get(tag, {}).get("units", {}).get("USD", [])
                if r.get("end") == asof and r.get("val") is not None]
        if rows:
            val = max(rows, key=lambda r: r.get("filed") or "")["val"]
            found = True
            if tag == "MinorityInterest":
                got_minority = True
            # Some issuers tag the SAME reported line under two MEZZANINE_TAGS concepts —
            # U (Unity Software): RedeemableNoncontrollingInterestEquityCarryingAmount and
            # TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests
            # both read 266,727,000 at 2026-06-30, the SAME redeemable-NCI line disclosed
            # twice for taxonomy reasons, not two components. Summing both overshot the
            # footing identity by exactly that 266.7M (quality.py-043, 2026-08-25). Dedupe
            # by exact value match at this date rather than by tag identity — CMTL/ATNI's
            # genuinely additive pair (120,524,000 + 97,393,000, distinct values) is untouched.
            if val in seen_vals:
                continue
            seen_vals.add(val)
            total += val
    if not got_minority:
        # ARES: 732 facts live under its own `ares:` extension namespace (companyfacts
        # drops it) and NCI never gets its own us-gaap MinorityInterest tag — only the
        # COMBINED total. Where that happens, back the NCI portion out of the combined
        # tag instead of losing it: NCI = (parent + NCI) - parent.
        rows = [r for r in gaap.get("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", {})
                .get("units", {}).get("USD", [])
                if r.get("end") == asof and r.get("val") is not None]
        if not rows:
            # Partnership form has no "stockholders'" equity at all — KRP (Kimbell Royalty
            # Partners LP) reports zero rows under the StockholdersEquity tag above, so the
            # NCI backout silently did nothing; PartnersCapitalIncludingPortionAttributable-
            # ToNoncontrollingInterest is the LP-form equivalent combined tag (quality.py-043,
            # 2026-08-25: KRP 669,545,000 combined - 575,851,000 parent-only PartnersCapital
            # = 93,694,000 NCI, closing the remaining footing gap to the dollar).
            rows = [r for r in gaap.get("PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest", {})
                    .get("units", {}).get("USD", [])
                    if r.get("end") == asof and r.get("val") is not None]
        if rows:
            incl = max(rows, key=lambda r: r.get("filed") or "")["val"]
            total += incl - parent_eq
            found = True
    return total if found else None


def _foot_check(card, F, gaap, tol=0.01, flag=True):
    """Does the balance sheet foot? Runs on EVERY card, always.

    This logic used to live only inside _zero_proof, which only runs when some concept is
    missing or stale — so a card with every concept PRESENT but one of them WRONG was never
    checked at all. That is how GEL shipped on 2026-08-18 carrying equity of $127,680,000
    against $5.41B of assets and $4.96B of liabilities (the identity implies ~$452.8M; the
    card had grabbed a component of an MLP's partners' capital rather than the total) with
    ZERO flags on it. sweepcheck caught it from outside the card; the card itself was silent,
    and a silent card is what the PM underwrites from.

    Returns (foots, implied, gap, err, equity_used, mezz_used) — equity_used may include
    mezzanine; mezz_used is the dollar amount of NCI/temporary equity added on top of
    parent-only StockholdersEquity to make it foot (0.0 if none was needed). Callers that
    want to show their work — e.g. the zero-proof note, so "equity -8,126,000" doesn't read
    as an unexplained plug — recover parent-only equity as `equity_used - mezz_used`.
    `flag=False` lets a caller reuse the arithmetic without double-flagging."""
    ta = (F.get("total_assets") or {}).get("value")
    eq = (F.get("equity") or {}).get("value")
    tl_fig = F.get("total_liabilities") or {}
    tl = tl_fig.get("value")
    asof = (F.get("total_assets") or {}).get("asof")
    if not (ta and eq and tl) or ta - eq <= 0:
        return None, None, None, None, eq, 0.0
    if tl_fig.get("STALE"):
        # total_liabilities is itself a retired/stale tag (FLS: last filed 2014-12-31,
        # 4199d behind) — testing the identity against a number the card already
        # quarantined produces a bogus gap, not a real footing defect. That STALE flag
        # already tells the reader "excluded from derived values"; a second, contradictory
        # "does not foot" flag built on the same excluded number is noise, not signal.
        return None, None, None, None, eq, 0.0
    implied = ta - eq
    gap = implied - tl
    err = abs(gap) / implied
    if err > tol:
        mezz = _mezzanine_equity(gaap, asof, eq)
        if mezz:
            implied2 = ta - (eq + mezz)
            gap2 = implied2 - tl
            err2 = abs(gap2) / implied2 if implied2 else err
            if err2 <= tol:
                card.setdefault("_doc_footing",
                    f"balance sheet foots once {mezz:,.0f} of noncontrolling/temporary "
                    f"equity (outside parent-only StockholdersEquity) is added back — "
                    f"see MEZZANINE_TAGS in fincard.py")
                implied, gap, err = implied2, gap2, err2
                eq = eq + mezz  # for the zero-proof note below: total equity, not parent-only
                return True, implied, gap, err, eq, mezz
            else:
                flag and card["flags"].append(
                    f"BALANCE SHEET DOES NOT FOOT: assets - equity = {implied:,.0f} but "
                    f"stated liabilities = {tl:,.0f}, a gap of {gap:,.0f} ({err * 100:.1f}%) "
                    f"— even after adding {mezz:,.0f} of noncontrolling/temporary equity "
                    f"(still {gap2:,.0f} / {err2 * 100:.1f}% short). That gap is liabilities "
                    f"we cannot see — net cash and EV are understated by roughly that much. "
                    f"Do not treat this card's leverage as known.")
                return False, implied, gap, err, eq, mezz
        else:
            # The statement does not foot. Do not assert anything — quantify what is missing,
            # which is far more useful than "stale" and is the ARI/HUT signature.
            flag and card["flags"].append(
                f"BALANCE SHEET DOES NOT FOOT: assets - equity = {implied:,.0f} but stated "
                f"liabilities = {tl:,.0f}, a gap of {gap:,.0f} ({err * 100:.1f}%). That gap is "
                f"liabilities we cannot see — net cash and EV are understated by roughly that "
                f"much. Do not treat this card's leverage as known.")
            return False, implied, gap, err, eq, 0.0
    return True, implied, gap, err, eq, 0.0


def _zero_proof(card, F, names, gaap, tol=0.01):
    """Turn 'stale, unknown' into 'zero, proven' where BOTH halves of the standard hold
    (pm-023/fincard.py-024, 2026-08-20): (1) COMPLETENESS — the balance sheet foots
    without the missing concept (_foot_check's arithmetic); (2) EXHAUSTIVENESS — no
    debt-like concept anywhere in the issuer's own companyfacts carries a nonzero value
    at the balance-sheet date that our tag maps did not already look for
    (_debt_like_hits). Condition (1) alone is not enough: TLS's finance leases sat
    INSIDE total_liabilities, so the identity closed cleanly while still blessing a
    zero that was wrong by 6,649,000. Failing either condition leaves the concept
    UNKNOWN and flagged — never a silent zero, never the word PROVEN.

    The note SHOWS the equity figure's components rather than printing one number labeled
    "equity" — a card whose mezzanine-adjusted equity coincidentally reads like assets minus
    liabilities is not a plug, but printing only the combined figure makes it look like one.
    fincard.py-013 (2026-08-19) diagnosed KPLT's "-8,126,000" as circular/back-solved because
    the note didn't disclose it was parent equity -36,035,000 + 27,909,000 of independently-
    reported temporary equity — both figures are real XBRL facts, neither derived from
    assets or liabilities. Spelling out the components here is the fix: the arithmetic was
    always sound, the note just didn't show its work."""
    foots, implied, gap, err, eq, mezz = _foot_check(card, F, gaap, tol, flag=False)
    if not foots:
        return []
    ta = (F.get("total_assets") or {}).get("value")
    tl = (F.get("total_liabilities") or {}).get("value")
    asof = (F.get("total_assets") or {}).get("asof")
    wanted = [n for n in names if n in ZERO_PROVABLE]
    if not wanted:
        return []
    hits = _debt_like_hits(gaap, asof)
    if hits:
        hit_str = "; ".join(f"{c}={v:,.0f}" for c, v in hits[:4])
        issuer = card.get("ticker") or "the issuer"
        for n in wanted:
            card["flags"].append(
                f"{n}: UNKNOWN, not zero — the balance sheet foots without it, but {issuer} "
                f"reports {hit_str} at {asof}, a debt-like concept our tag map does not "
                f"resolve into debt_lt/debt_current. A hit proves debt exists; it does not "
                f"say which line it belongs on, so the concept is left UNKNOWN rather than "
                f"guessed. Leverage not known — do not treat net_cash/EV as reliable "
                f"(fincard.py-024).")
        return []
    if mezz:
        parent_eq = eq - mezz
        eq_note = (f"parent equity {parent_eq:,.0f} + {mezz:,.0f} of noncontrolling/"
                   f"temporary equity (both independently reported, not derived from "
                   f"assets or liabilities) = {eq:,.0f} total equity")
    else:
        eq_note = f"equity {eq:,.0f}"
    proven = []
    for n in wanted:
        F[n] = {"value": 0.0, "unit": "USD", "asof": asof, "tag": None,
                "source": "zero-proved",
                "note": (f"ZERO, CHECKED (both conditions of the fincard.py-024 standard): "
                         f"(1) COMPLETENESS — total assets {ta:,.0f} - ({eq_note}) = "
                         f"{implied:,.0f}, matching stated total liabilities {tl:,.0f} to "
                         f"within {err * 100:.2f}%. (2) EXHAUSTIVENESS — no debt-like "
                         f"XBRL concept outside our own tag map reports a nonzero value at "
                         f"{asof}. Both checked, not inferred; treated as zero.")}
        card["flags"] = [f for f in card["flags"] if not f.startswith(f"{n}: ")]
        card.setdefault("zero_proved", []).append(
            {"concept": n, "identity_error_pct": round(err * 100, 3), "asof": asof})
        proven.append(n)
    return proven


def build(tk, cik_override=None):
    tk = tk.upper()
    cik = resolve_cik(tk, cik_override)
    cf = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    gaap = cf.get("facts", {}).get("us-gaap", {})
    dei = cf.get("facts", {}).get("dei", {})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    card = {"ticker": tk, "cik": cik, "entity": cf.get("entityName"), "built": now,
            "_doc": "PURE CODE from SEC XBRL + live quote. figures carry tag/period/filed; "
                    "derived carry formulas; series carry history for custom coded expressions "
                    "(query.py). Finnhub appears ONLY in cross_checks. Flags are part of the "
                    "number. Valuation grid is MECHANICAL — a ruler, never a thesis.",
            "figures": {}, "series": {}, "derived": {}, "valuation": {},
            "cross_checks": {}, "flags": []}
    F, S = card["figures"], card["series"]

    def rows_for(tagmap, name):
        """Pick the tag with the most RECENT data, not the first with any rows —
        issuers retire tags (TRIP's PaymentsToAcquirePropertyPlantAndEquipment ends
        2021; first-match served five-year-stale capex into FCF. Caught by fincheck
        2026-08-12)."""
        want_unit = FLOW_UNITS.get(name, "USD")
        best, found = (None, [], ""), []
        for tag in tagmap[name]:
            units = gaap.get(tag, {}).get("units", {})
            rows = units.get(want_unit) or (units.get("shares") if want_unit == "shares" else None) or []
            if not rows:
                continue
            found.append((tag, rows))
            newest = max((r.get("end") or "" for r in rows), default="")
            if newest > best[2]:
                best = (tag, rows, newest)
        if name in FLOW_MERGE and best[0] and len(found) > 1:
            # gap-fill only: the freshest tag owns every period it reports; an
            # alternate tag may ONLY contribute periods that tag never filed
            seen = {(r.get("start"), r.get("end")) for r in best[1]}
            merged = list(best[1])
            for tag, rows in found:
                if tag == best[0]:
                    continue
                for r in rows:
                    if (r.get("start"), r.get("end")) not in seen:
                        seen.add((r.get("start"), r.get("end")))
                        merged.append(r)
            return best[0], merged
        return best[0], best[1]

    ttm_vals = {}
    for name in FLOW:
        tag, rows = rows_for(FLOW, name)
        if not rows:
            if name in ("revenue", "net_income", "cfo", "capex"):
                card["flags"].append(f"{name}: no XBRL tag found")
            continue
        avg = name in FLOW_AVG
        quarters, annuals, ytd = _pick_flow(rows, derive=not avg)
        val, period, endd, startd = _ttm(quarters, annuals, ytd, mode="avg" if avg else "sum")
        if val is None:
            continue
        F[name] = {"value": val, "unit": FLOW_UNITS.get(name, "USD"), "period": period,
                   "period_end": endd, "period_start": startd, "tag": tag,
                   "latest_quarter_end": quarters[0]["end"] if quarters else None}
        ttm_vals[name] = val
        pv, pp = _prior_ttm(quarters, annuals, mode="avg" if avg else "sum")
        if pv is not None:
            F[name]["prior_period_value"], F[name]["prior_period"] = pv, pp
        S[name] = {"quarters": [{"end": q["end"], "value": q["value"],
                                 **({"derived": q["derived"]} if q.get("derived") else {})}
                                for q in quarters[:12]],
                   "annual": [{"end": a["end"], "value": a["value"]} for a in annuals[:8]]}

    # SOURCED OVERRIDES: some issuers report a line only in the printed statement.
    # LYFT's "Purchases of property and equipment and scooter fleet" is the standing
    # case — no us-gaap tag, and the CapitalizedComputerSoftwareAdditions proxy is 0,
    # so FCF silently fell back to CFO and printed 4.86x EV/FCF against a true 5.22x
    # (2026-08-14). A number that exists in a filing but not in XBRL must still be
    # allowed into the card, and must NEVER be mistakable for an XBRL figure:
    #   * applied ONLY where the XBRL pass produced nothing (a filing tag always wins)
    #   * every entry carries a verbatim quote + the document it came from
    #   * source is stamped MANUAL and a flag is raised on every card that uses one
    for name, ov in (MANUAL.get(tk.upper()) or {}).items():
        if name not in FLOW:
            continue
        existing = F.get(name)
        # XBRL wins whenever it is AT LEAST AS FRESH as the manual figure — MANUAL exists
        # to beat a stale/absent XBRL pass, never to override a current tag. MDGL's cogs is
        # PRESENT in F (companyfacts served a genuine 2024-12-31 CostOfGoodsAndServicesSold
        # point) but that point is 546+ days stale because the fresher quarters are filed
        # only under a segment dimension companyfacts drops — "name in F" alone would have
        # skipped this override forever, so freshness (not mere presence) is the test.
        if existing and (existing.get("period_end") or "") >= (ov.get("period_end") or ""):
            continue
        F[name] = {"value": ov["value"], "unit": FLOW_UNITS.get(name, "USD"),
                   "period": ov["period"], "period_end": ov["period_end"],
                   "tag": "MANUAL (not read by the XBRL pass)", "source": "MANUAL — PM-verified",
                   "quote": ov["quote"], "doc": ov["doc"], "entered": ov["entered"],
                   "formula": ov.get("formula")}
        ttm_vals[name] = ov["value"]
        card["flags"].append(
            f"{name}: MANUAL figure — not read by the XBRL pass, keyed from the printed "
            f"statement ({ov['doc']}, entered {ov['entered']}). Quote on the figure. Derived "
            f"values built on it inherit this: verify the quote before quoting the derivation.")

    # quarantine stale flows: a concept whose data ends >270 days before the freshest
    # concept was likely reported under a retired tag — keep the figure (flagged) but
    # NEVER feed it into derived values (stale capex inside FCF is silent poison)
    ends = {n: (F[n].get("period_end") or F[n].get("latest_quarter_end"))
            for n in ttm_vals if n in F}
    newest_end = max((e for e in ends.values() if e), default="")
    for n, e in ends.items():
        if e and newest_end and _days(e, newest_end) > 270:
            F[n]["STALE"] = f"data ends {e}, {_days(e, newest_end)}d behind freshest concept — tag likely retired"
            if n in EPISODIC_FLOWS or n in DISPLAY_ONLY:
                # Not a defect: the company stopped doing the thing. Recorded, not flagged.
                F[n]["not_reported"] = (f"last reported {e}; nothing since. For {n} that normally "
                                        f"means the company did none — not that the tag moved.")
                F[n].pop("STALE", None)
            elif n in AUX_ONLY_FLOWS:
                pass  # excluded from ttm_vals below, but no standalone flag — see AUX_ONLY_FLOWS
            else:
                card["flags"].append(f"{n}: STALE ({F[n]['STALE']}) — excluded from derived values")
            del ttm_vals[n]

    def component_sum(name):
        """INSTANT_SUM fallback: rebuild an annual-only total from the current/
        noncurrent split the issuer does tag quarterly. All components or nothing."""
        comps = INSTANT_SUM.get(name) or []
        per = {}
        for ctag in comps:
            for r in gaap.get(ctag, {}).get("units", {}).get("USD", []):
                if r.get("end") is None or r.get("val") is None:
                    continue
                at = per.setdefault(r["end"], {})
                if ctag not in at or (r.get("filed") or "") > (at[ctag].get("filed") or ""):
                    at[ctag] = r
        pts = [{"end": e, "val": sum(x["val"] for x in at.values()),
                "form": next(iter(at.values())).get("form"),
                "filed": next(iter(at.values())).get("filed")}
               for e, at in sorted(per.items()) if len(at) == len(comps)]
        return ("+".join(comps), pts) if pts else (None, [])

    for name in INSTANT:
        tag, rows = rows_for(INSTANT, name)
        vals = sorted([r for r in rows if r.get("end") and r.get("val") is not None],
                      key=lambda r: (r["end"], r.get("filed") or ""))
        ctag, cpts = component_sum(name)
        if cpts and (not vals or cpts[-1]["end"] > vals[-1]["end"]):
            tag, vals = ctag, cpts
        if not vals:
            if name in ("cash", "equity", "total_assets"):
                card["flags"].append(f"{name}: no XBRL tag found")
            continue
        v = vals[-1]
        F[name] = {"value": v["val"], "unit": "USD", "asof": v["end"], "tag": tag,
                   "form": v.get("form"), "filed": v.get("filed"),
                   **({"note": "sum of the components above — issuer tags the total "
                               "only annually"} if tag == ctag else {})}
        seen = {}
        for r in vals:
            seen[r["end"]] = r["val"]
        S[name] = {"points": [{"asof": k, "value": seen[k]} for k in sorted(seen)[-12:]]}

    # SOURCED OVERRIDES, INSTANT twin of the FLOW block above (added 2026-08-25,
    # quality.py-043): some issuers carry TWO simultaneous, genuinely additive debt
    # instruments where our tag map can only pick ONE (rows_for keeps the freshest single
    # tag; it does not sum). INSG (Inseego) reports LongTermLineOfCredit 10,000,000 AND
    # SecuredLongTermDebt 50,291,000 at the same 2026-06-30 date — two distinct, non-
    # overlapping instruments (confirmed against the balance sheet: noncurrent liabilities
    # 66,618,000 = 50,291,000 + 10,000,000 + OperatingLeaseLiabilityNoncurrent 2,381,000 +
    # OtherLiabilitiesNoncurrent 3,754,000 = 66,426,000, within 192,000/0.3%). Adding
    # SecuredLongTermDebt as a plain alternate would make rows_for pick ONE of the two
    # (whichever is freshest/first-listed) and silently drop the other — same
    # understatement risk as the bug this file exists to avoid. MANUAL is the existing,
    # narrower escape hatch (PM-verified, quoted, flagged) for exactly this shape; applied
    # to INSTANT concepts here for the first time, same rules as the FLOW version.
    for name, ov in (MANUAL.get(tk.upper()) or {}).items():
        if name not in INSTANT:
            continue
        existing = F.get(name)
        # Strict `>`, not `>=` (unlike the FLOW version above): the FLOW case exists for
        # STALENESS (an equally-fresh XBRL tag is trusted over a frozen manual figure
        # because both would compute the SAME thing). This INSTANT case exists for
        # ADDITIVITY — INSG's single-tag pick (LongTermLineOfCredit alone) is EQUALLY
        # fresh as the MANUAL sum and still wrong, because freshness never told rows_for
        # to add a second simultaneous instrument in the first place. A tie must go to
        # MANUAL here; only a STRICTLY newer filing (a real reason to re-key) defers to it.
        if existing and (existing.get("asof") or "") > (ov.get("period_end") or ""):
            continue
        F[name] = {"value": ov["value"], "unit": "USD", "asof": ov["period_end"],
                   "tag": "MANUAL (not read by the XBRL pass)", "source": "MANUAL — PM-verified",
                   "quote": ov["quote"], "doc": ov["doc"], "entered": ov["entered"],
                   "formula": ov.get("formula")}
        card["flags"].append(
            f"{name}: MANUAL figure — not read by the XBRL pass, keyed from the printed "
            f"statement ({ov['doc']}, entered {ov['entered']}). Quote on the figure. Derived "
            f"values built on it inherit this: verify the quote before quoting the derivation.")

    # A debt_lt/debt_current resolved ONLY via a finance-lease tag (fincard.py-022) is a
    # real reported figure, but not necessarily the issuer's WHOLE long-term debt: AES
    # resolves 714,000,000 this way while reporting no other debt-like concept anywhere in
    # companyfacts, for a $54B-asset utility holding company whose conventional bonds are
    # plausibly sitting in an extension namespace this scan cannot see (same shape as
    # ARI's `ari:` debt tag). Apply the SAME two-condition standard used for zero-proof
    # (fincard.py-024): flag unless the balance sheet foots AND no other debt-like concept
    # exists — TLS clears both (its total_liabilities reconciles with the two finance-
    # lease figures already on the card) and stays silent; AES clears neither (no
    # total_liabilities tag to foot against at all) and is flagged for review.
    #
    # FIXED 2026-08-25 (quality.py-043 blast-radius review): the condition below was
    # INVERTED — `if _debt_like_hits(...): continue` SKIPPED the flag exactly when a hit
    # existed, i.e. exactly when there WAS other debt out there our tag map cannot resolve.
    # That is the one case this check exists to catch. Live and silent on HUT: debt_lt read
    # FinanceLeaseLiability=0 at 2026-06-30 while the issuer's own DebtInstrumentCarryingAmount
    # showed 7,735,104,000 at the same date — a 7.7B debt understatement with ZERO flags on
    # the card, the exact "PROVEN ZERO on a real debt" shape fincard.py-022 was written to
    # stop, reintroduced by this one inverted condition. Also silent on PRI (NotesPayable
    # 595,716,000 unresolved against a FinanceLeaseLiability debt_lt of ~1,001,000). "Clears
    # both -> silent" from the doctrine above means BOTH conditions must hold to stay quiet;
    # either failing must flag, so a hit alone is now sufficient to flag, not to suppress.
    for _dn in ("debt_lt", "debt_current"):
        _fig = F.get(_dn)
        if not _fig or not (_fig.get("tag") or "").startswith("FinanceLeaseLiability"):
            continue
        _hits = _debt_like_hits(gaap, _fig["asof"])
        _foots = _foot_check(card, F, gaap, flag=False)[0]
        if not _hits and _foots is True:
            continue
        if _hits:
            hit_str = "; ".join(f"{c}={v:,.0f}" for c, v in _hits[:4])
            card["flags"].append(
                f"{_dn}: sourced ONLY from a finance-lease tag ({_fig['tag']} = "
                f"{_fig['value']:,.0f} at {_fig['asof']}) — but the issuer ALSO reports "
                f"{hit_str} at the same date, a debt-like concept our tag map does not "
                f"resolve into debt_lt/debt_current. Treat {_fig['value']:,.0f} as a LOWER "
                f"BOUND, not the whole debt line (fincard.py-022/quality.py-043).")
        else:
            card["flags"].append(
                f"{_dn}: sourced ONLY from a finance-lease tag ({_fig['tag']} = "
                f"{_fig['value']:,.0f} at {_fig['asof']}) — no other debt-like XBRL concept "
                f"found, and the balance sheet cannot be footed to confirm nothing else is "
                f"missing. If this issuer carries conventional debt under an extension "
                f"namespace (see ARI), it is not reflected here.")

    # same quarantine as FLOW above, applied to balance-sheet points in time: a debt/asset
    # tag that stops updating while cash/equity/total_assets keep filing quarterly is a
    # retired tag serving a stale carrying value, not "unchanged" (ARI debt_lt served a
    # 2021-09-30 LongTermDebt figure as current — caught by fincheck 2026-08-13)
    inst_ends = {n: F[n].get("asof") for n in INSTANT if n in F}
    # THE BALANCE-SHEET DATE, and it is not simply the newest instant on the card.
    # `shares_out` comes off the 10-K COVER PAGE and is dated the filing date (KLAC:
    # 2026-08-03) while the balance sheet it accompanies is 2026-06-30. Taking the max
    # across all instants therefore invents a date on which NO balance-sheet line exists,
    # which (a) inflated every staleness measurement by the cover-page lag and (b) made the
    # filing rescue match on a date nothing is filed under — it found 0 candidates on 58 of
    # the 60 concepts it tried, which is why the first run rescued almost nothing.
    #
    # The balance-sheet date is the newest instant among lines that ARE the balance sheet.
    _CORE_BS = ("cash", "total_assets", "equity", "total_liabilities", "current_assets")
    inst_newest = max((F[n]["asof"] for n in _CORE_BS if n in F and F[n].get("asof")),
                      default="") or max((e for e in inst_ends.values() if e), default="")
    stale_instant = set()
    for n, e in inst_ends.items():
        if e and inst_newest and _days(e, inst_newest) > 270:
            F[n]["STALE"] = f"data ends {e}, {_days(e, inst_newest)}d behind freshest concept — tag likely retired"
            if n in DISPLAY_ONLY:
                F[n]["not_used"] = ("stale, and no derived value reads it — recorded rather "
                                    "than flagged (see DISPLAY_ONLY in fincard.py)")
            else:
                card["flags"].append(f"{n}: STALE ({F[n]['STALE']}) — excluded from derived values")
            stale_instant.add(n)

    # Before accepting "missing" or "stale" as the answer, read the filing. One HTTP fetch,
    # and only when something is actually unresolved.
    _wanted = [n for n in RESCUE
               if n in stale_instant or n not in F]
    if _wanted and inst_newest:
        try:
            _n = _rescue_instant(card, F, cik, _wanted, inst_newest)
            for n in list(stale_instant):
                if F.get(n, {}).get("source") == "filing-extension":
                    stale_instant.discard(n)   # rescued: it is a live figure again
        except Exception as e:
            card["flags"].append(f"filing-rescue failed ({type(e).__name__}: {str(e)[:60]})")
        # Whatever the filing could not supply, the balance sheet may still be able to
        # PROVE is zero. Run last, on what is still unresolved.
        try:
            _left = [n for n in _wanted if F.get(n, {}).get("source") != "filing-extension"]
            for n in _zero_proof(card, F, _left, gaap):
                stale_instant.discard(n)
        except Exception as e:
            card["flags"].append(f"zero-proof failed ({type(e).__name__}: {str(e)[:60]})")

    sh_rows = (dei.get("EntityCommonStockSharesOutstanding", {}).get("units", {}) or {}).get("shares", [])
    if sh_rows:
        sh_rows.sort(key=lambda r: (r.get("end") or "", r.get("filed") or ""))
        s = sh_rows[-1]
        F["shares_out"] = {"value": s["val"], "unit": "shares", "asof": s.get("end"),
                           "tag": "dei:EntityCommonStockSharesOutstanding", "form": s.get("form"),
                           "note": "dei may cover ONE class only on multi-class issuers — see cross_checks"}
        seen_sh = {}  # dedupe: several filings restate the same as-of date
        for r in sh_rows:
            if r.get("end"):
                seen_sh[r["end"]] = r["val"]
        S["shares_out"] = {"points": [{"asof": k, "value": seen_sh[k]} for k in sorted(seen_sh)[-16:]]}

    # multi-class rescue: trigger whenever companyfacts served nothing, or something
    # more than 270 days old — see _shares_out_rescue's docstring for why that happens.
    _cur_end = F.get("shares_out", {}).get("asof")
    if not _cur_end or _days(_cur_end, now[:10]) > 270:
        _resc = _shares_out_rescue(cik, tk)
        if _resc:
            _val, _asof, _n = _resc
            if not _cur_end or _asof > _cur_end:
                F["shares_out"] = {
                    "value": _val, "unit": "shares", "asof": _asof,
                    "tag": f"dei:EntityCommonStockSharesOutstanding ({_n} class(es), filing-summed)",
                    "source": "filing-extension",
                    "note": (f"companyfacts served no current non-dimensional shares fact "
                             f"(multi-class issuer, or filer switched to per-class tagging) — "
                             f"summed {_n} class(es) from the issuer's own latest 10-Q/10-K "
                             f"instead of using a stale companyfacts figure.")}
                card.setdefault("rescued", []).append(
                    {"concept": "shares_out", "value": _val, "asof": _asof, "classes": _n})

    # preferred liquidation preference — COMMON book value must exclude it (BOOK.md
    # fincard defect (b), 2026-08-13: ARI printed BVPS 9.79 against 8.47 true, because
    # $169.26M of preferred sits inside StockholdersEquity). Kept outside INSTANT so a
    # redeemed preferred can't raise a STALE flag; only applied when it is at least as
    # current as the equity figure it is subtracted from.
    pref_rows = [r for r in (gaap.get("PreferredStockLiquidationPreferenceValue", {})
                             .get("units", {}) or {}).get("USD", [])
                 if r.get("end") and r.get("val") is not None]
    if pref_rows:
        p = max(pref_rows, key=lambda r: (r["end"], r.get("filed") or ""))
        F["preferred_liq_pref"] = {"value": p["val"], "unit": "USD", "asof": p["end"],
                                   "tag": "PreferredStockLiquidationPreferenceValue",
                                   "form": p.get("form"), "filed": p.get("filed"),
                                   "note": "aggregate; subtracted from equity for COMMON book value"}

    # ---------------- derived, formulas attached ----------------
    D = card["derived"]

    def gv(name):
        if name in stale_instant:
            return None
        return (F.get(name) or {}).get("value")

    def put(key, value, formula, note=""):
        if value is not None:
            D[key] = {"value": round(value, 4) if isinstance(value, float) else value,
                      "formula": formula, **({"note": note} if note else {})}

    cash, sti = gv("cash") or 0, gv("st_investments") or 0
    dlt, dcur = gv("debt_lt") or 0, gv("debt_current") or 0
    # a QUARANTINED debt figure must poison net cash LOUDLY — DXC lesson 2026-08-13:
    # $2.37B of LTD sat stale-excluded while net_cash printed as cash alone, and a
    # screen ranked DXC top on the phantom "net cash"
    stale_debt = ""
    for dk in ("debt_lt", "debt_current"):
        fdk = F.get(dk) or {}
        if fdk.get("STALE") and fdk.get("value"):
            stale_debt += (f" {dk} tag STALE (last known {fdk['value']:,.0f} at "
                           f"{fdk.get('asof')}) excluded —")
    if gv("cash") is not None:
        put("total_debt", dlt + dcur, f"debt_lt {dlt:,.0f} + debt_current {dcur:,.0f}",
            "excl. operating leases (see operating_lease_liab figure); missing tags count as 0"
            + stale_debt)
        put("net_cash", cash + sti - dlt - dcur,
            f"cash {cash:,.0f} + st_investments {sti:,.0f} - total_debt {dlt + dcur:,.0f}",
            f"as of {F['cash'].get('asof')}"
            + (stale_debt + " NET CASH UNRELIABLE until the current debt tag is found"
               if stale_debt else ""))
        if stale_debt:
            card["flags"].append("NET CASH UNRELIABLE:" + stale_debt.rstrip("—") +
                                 " find the issuer's current debt tag (EV/multiples inherit this)")
    if gv("current_assets") is not None and gv("current_liabilities") is not None:
        put("working_capital", gv("current_assets") - gv("current_liabilities"),
            f"current_assets {gv('current_assets'):,.0f} - current_liabilities {gv('current_liabilities'):,.0f}")
        if gv("current_liabilities"):
            put("current_ratio", gv("current_assets") / gv("current_liabilities"),
                f"current_assets / current_liabilities")

    rev, ni, opi = ttm_vals.get("revenue"), ttm_vals.get("net_income"), ttm_vals.get("op_income")
    if opi is None and rev is not None and ttm_vals.get("costs_and_expenses") is not None:
        opi = rev - ttm_vals["costs_and_expenses"]
        put("op_income_calc", opi,
            f"revenue {rev:,.0f} - costs_and_expenses {ttm_vals['costs_and_expenses']:,.0f}",
            "issuer's income statement has no OperatingIncomeLoss subtotal — this is "
            "revenue minus the statement's own 'Total costs and expenses' line, the "
            "subtotal that precedes Other Income/Expense on the face of the statement")
    cfo, capex, capex_sw = ttm_vals.get("cfo"), ttm_vals.get("capex"), ttm_vals.get("capex_software")
    capex_note = ""
    # capex_sw must cover a comparable SPAN to capex before being summed — "single period
    # on file" is fincard's own label for LESS than a verified TTM/FY (as little as one
    # half-year), and summing that partial window against the other concept's full TTM/FY
    # silently mixes a 6-month figure into a 12-month total. LTCH (blast-radius review,
    # 2026-08-22): capex YTD 2026-01-01..2026-06-30 (6mo, no TTM built yet) + capex_
    # software TTM 2025-07-01..2026-06-30 (12mo) — same period_end, HALF the actual span,
    # and the mismatch alone produced a 67,233% "addition" that had nothing to do with the
    # double-count risk fincard.py-033 was written to catch. TTM-vs-FY-fallback pairs (both
    # genuine ~12mo windows, just anchored on different fiscal-year-ends) are left alone —
    # that offset is the same order as tolerances already accepted elsewhere in this file.
    capex_sw_comparable = (
        "single period on file" not in (F.get("capex", {}).get("period") or "")
        and "single period on file" not in (F.get("capex_software", {}).get("period") or ""))
    if capex is not None and capex_sw and capex_sw_comparable:
        # ADDITIVE, not first-match (fincard.py-033, PM 2026-08-21): capex_software is a
        # separately-reported line, not an alternate tag for "capex" — see the FLOW comment
        # above. Anti-double-count: the two concepts are, by GAAP definition, mutually
        # exclusive cash-flow captions (PP&E purchases vs. capitalized software development,
        # which lands on the balance sheet as an intangible, never PP&E), so summing them
        # sums two different reported dollars rather than counting one dollar twice. TLS
        # proof (2026-08-21 repro): CFO 8,833 - PP&E capex 246 - software capex 1,970 (H1) =
        # 6,617, matching the issuer's OWN "Free Cash Flow" press-release figure to the
        # dollar; the PP&E-only card read 17.99% FCF margin against TLS's reported 13.9%.
        # Still flagged past a materiality threshold rather than trusted blindly — if some
        # future issuer's PP&E tag turns out to already subsume software spend, a >20% jump
        # is the signal that surfaces it for review instead of silently doubling the figure.
        combined = capex + capex_sw
        pct_added = (capex_sw / capex) if capex else float("inf")
        capex_note = (f" capex is PP&E {capex:,.0f} + capitalized software {capex_sw:,.0f} "
                      f"(fincard.py-033) = {combined:,.0f}.")
        if pct_added > 0.20:
            card["flags"].append(
                f"capex_software adds {pct_added * 100:,.0f}% on top of PP&E-only capex "
                f"({capex_sw:,.0f} added to {capex:,.0f}) — material; verify neither line "
                f"double-counts the other on this issuer's cash-flow statement (fincard.py-033).")
        capex = combined
    elif capex is not None and capex_sw and not capex_sw_comparable:
        card["flags"].append(
            f"capex_software resolved ({capex_sw:,.0f}, {F.get('capex_software', {}).get('period')}) "
            f"but is not yet a full TTM/FY — NOT combined into capex/FCF to avoid summing a "
            f"partial period against a full one; revisit once a full period is on file "
            f"(fincard.py-033).")
    elif cfo is not None and capex is None:
        # PROXY CHAIN (2026-08-13, LYFT forensic): some issuers tag NO cash-capex
        # line at all (LYFT's "purchases of property, equipment and scooter fleet"
        # is untagged in XBRL). Fall back to capitalized-software additions as an
        # explicit, labeled proxy before surrendering to the upper-bound flag.
        if capex_sw:   # a 0-proxy is no proxy — keep the honest upper-bound flag instead
            capex = capex_sw
            capex_note = (f" capex is a PROXY: capitalized software additions "
                          f"({F.get('capex_software', {}).get('period')}) — issuer tags no "
                          f"cash-capex line; true capex may differ, verify the cash-flow "
                          f"statement.")
    if cfo is not None:
        if capex is None:
            capex_note = (" CAPEX INVISIBLE TO XBRL API — this FCF is an UPPER BOUND (=CFO). "
                          "Known cause: the companyfacts API OMITS issuer-extension tags entirely "
                          "(LYFT tags its capex line 'lyft:', 2026-08-13 forensic) — the number "
                          "exists only in the printed statement; QUOTE it from there.")
        put("fcf", cfo - (capex or 0), f"CFO {cfo:,.0f} - capex {capex or 0:,.0f}",
            "FCF is NOT operating cash flow — capex subtracted (2026-08-12 LYFT lesson). "
            + (F.get("cfo", {}).get("period") or "") + capex_note)
        if "PROXY" in capex_note:
            card["flags"].append("capex is a software-additions PROXY (no cash-capex tag) — "
                                 "fcf approximate; " + capex_note.strip())
    if opi is not None and ttm_vals.get("dna") is not None:
        put("ebitda_approx", opi + ttm_vals["dna"],
            f"op_income {opi:,.0f} + D&A {ttm_vals['dna']:,.0f}", "approximation")
    gp = ttm_vals.get("gross_profit")
    if gp is None and rev is not None and ttm_vals.get("cogs") is not None:
        gp = rev - ttm_vals["cogs"]
        put("gross_profit_calc", gp, f"revenue {rev:,.0f} - cogs {ttm_vals['cogs']:,.0f}")
    if rev:
        for label, num in (("gross_margin_pct", gp), ("op_margin_pct", opi),
                           ("net_margin_pct", ni), ("fcf_margin_pct", (D.get("fcf") or {}).get("value")),
                           ("sbc_pct_revenue", ttm_vals.get("sbc"))):
            if num is not None:
                put(label, num / rev * 100, f"{num:,.0f} / revenue {rev:,.0f}")
    fr = F.get("revenue")
    if fr and fr.get("prior_period_value"):
        put("revenue_growth_pct", (fr["value"] / fr["prior_period_value"] - 1) * 100,
            f"{fr['value']:,.0f} vs {fr['prior_period_value']:,.0f} ({fr['prior_period']})")
    if ni is not None and gv("equity"):
        put("roe_pct", ni / gv("equity") * 100, f"net_income {ni:,.0f} / equity {gv('equity'):,.0f}",
            "period-end equity, not average")
    if opi is not None and gv("equity") is not None:
        ic = gv("equity") + (D.get("total_debt", {}).get("value") or 0) - cash - sti
        if ic > 0:
            put("roic_approx_pct", opi * 0.79 / ic * 100,
                f"op_income {opi:,.0f} x (1-21% tax) / (equity+debt-cash {ic:,.0f})", "rough approximation")
    ie = ttm_vals.get("interest_expense")
    if opi is not None and ie:
        put("interest_coverage", opi / ie, f"op_income {opi:,.0f} / interest_expense {ie:,.0f}")
    eb = (D.get("ebitda_approx") or {}).get("value")
    td = (D.get("total_debt") or {}).get("value")
    if eb and eb > 0 and td is not None:
        put("debt_over_ebitda", td / eb, f"total_debt {td:,.0f} / approx EBITDA {eb:,.0f}")
    shp = S.get("shares_out", {}).get("points", [])
    if len(shp) >= 2 and F.get("shares_out", {}).get("asof"):
        base = shp[0]  # oldest available dei point; span stated in the formula
        span = _days(base["asof"], F["shares_out"]["asof"])
        if span >= 175 and base["value"]:
            put("share_count_change_pct",
                (F["shares_out"]["value"] / base["value"] - 1) * 100,
                f"{F['shares_out']['value']:,.0f} ({F['shares_out']['asof']}) vs "
                f"{base['value']:,.0f} ({base['asof']}) over {span} days",
                "positive = dilution, negative = net buybacks; span varies with dei history")

    _cik_tks = _cik_tickers(cik)
    _non_primary = bool(_cik_tks) and len(_cik_tks) > 1 and tk.upper() != _cik_tks[0].upper()
    px, sh = _price(tk), gv("shares_out")
    if px:
        card["price"] = {"value": px, "asof": now, "source": "finnhub quote"}
    if _non_primary:
        card["flags"].append(
            f"NON-PRIMARY SECURITY: CIK {cik} registers {len(_cik_tks)} tickers "
            f"({', '.join(_cik_tks)}) — {tk} is not {_cik_tks[0]}, the common stock "
            f"dei:EntityCommonStockSharesOutstanding covers. market_cap/EV/multiples are "
            f"NOT computed for {tk}: a non-common quote (preferred/alternate class) priced "
            f"against the common share count is not a real number (quality.py-043, "
            f"FTAIM forensic, 2026-08-25). Price is still recorded above for reference.")
    if px and sh and not _non_primary:
        mc = px * sh
        put("market_cap", mc, f"price {px} x shares_out {sh:,.0f} (asof {F['shares_out'].get('asof')})")
        fh_mc = _finnhub_mktcap(tk)
        if fh_mc:
            ratio = mc / fh_mc
            fh_implied_price = fh_mc / sh
            card["cross_checks"]["market_cap_vs_finnhub"] = {
                "computed": round(mc), "finnhub": round(fh_mc), "ratio": round(ratio, 3),
                "fh_implied_price": round(fh_implied_price, 2), "ok": 0.9 <= ratio <= 1.1}
            if not (0.9 <= ratio <= 1.1):
                # fh_implied_price (Finnhub's cached market cap / OUR OWN shares_out) lets
                # the reader tell the two candidate causes apart at a glance: close to the
                # live quote price -> our share count is the suspect (multi-class/stale
                # dei); far from it but a PLAUSIBLE past price -> Finnhub's /stock/profile2
                # market cap is just cached from an older quote (profile2 updates on a
                # slower cadence than /quote, the endpoint `px` itself comes from) and our
                # own price x shares figure is likely the correct one. Diagnosed 2026-08-20
                # against MRNA (fh_implied_price $63.0 vs live $174.38 after a large rally
                # — a stale-cache mismatch, not a share-count one).
                #
                # SPLIT DETECTION added 2026-08-25 (quality.py-043, SMXT forensic): SMXT
                # executed a 1-for-12 reverse split (Nevada cert. of change 2026-08-04,
                # effective ~2026-08-11) with no post-split 10-Q on file yet (NT 10-Q filed
                # 2026-08-17) — shares_out is a real, correctly-read dei fact that simply
                # PREDATES the split. Ratio read exactly 12.0, and the "multi-class shares"
                # message was actively wrong for this shape (no per-class dei fact exists
                # to rescue). Name the pattern when the ratio matches a common split factor.
                _split = ""
                for _n in (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30, 40, 50):
                    if abs(ratio - _n) / _n < 0.05 or abs(ratio * _n - 1) < 0.05:
                        _split = (f" Ratio is within 5% of {_n}x — consistent with an "
                                  f"UN-REFLECTED STOCK SPLIT (shares_out asof "
                                  f"{F['shares_out'].get('asof')} likely predates a since-"
                                  f"filed split/reverse-split; check EDGAR for a recent 8-K "
                                  f"item 5.03/3.03) rather than multi-class shares.")
                        break
                card["flags"].append(
                    f"MARKET CAP MISMATCH: computed {mc / 1e9:.2f}B vs Finnhub {fh_mc / 1e9:.2f}B "
                    f"(Finnhub-implied price ${fh_implied_price:,.2f} vs live quote ${px:,.2f}) — "
                    "likely multi-class shares (dei counts one class) or stale share count if the "
                    "implied price is close to the live quote; likely Finnhub's cached "
                    "/stock/profile2 market cap lagging a price move if it is not." + _split +
                    " EV/multiples below inherit this error — resolve before using")
        nc = (D.get("net_cash") or {}).get("value")
        if nc is not None:
            ev = mc - nc
            put("enterprise_value", ev, f"market_cap {mc:,.0f} - net_cash {nc:,.0f}")
            fcf = (D.get("fcf") or {}).get("value")
            if fcf and fcf > 0:
                # fincard.py-045 (quality.py-043, 2026-08-25): EV is a POINT-IN-TIME stock;
                # FCF from a single YTD period on file (no TTM/FY built yet — a newly-
                # registered issuer, e.g. MBGL HELD) is a PARTIAL-YEAR flow. Dividing the
                # two unannotated overstated MBGL's multiple ~2x (43.2x printed vs ~21.6x
                # true, on its 177,000,000 six-month FCF) with no flag anywhere on the card.
                # Annualize by the period's own day-count — named in the formula, never a
                # bare number — rather than either silently printing the wrong multiple or
                # refusing it outright.
                _cfo_f = F.get("cfo") or {}
                _cfo_days = (_days(_cfo_f["period_start"], _cfo_f["period_end"])
                             if _cfo_f.get("period_start") and _cfo_f.get("period_end") else None)
                if "single period on file" in (_cfo_f.get("period") or "") and _cfo_days:
                    _ann_fcf = fcf * 365.0 / _cfo_days
                    put("ev_over_fcf", ev / _ann_fcf,
                        f"EV {ev:,.0f} / FCF(annualized) {_ann_fcf:,.0f} (FCF {fcf:,.0f} over "
                        f"{_cfo_days}d x 365/{_cfo_days} — {_cfo_f.get('period')})",
                        "FCF is a partial-period (YTD, no TTM/FY on file yet) figure, "
                        "annualized by day-count so a point-in-time EV is not divided by a "
                        "partial-year flow (fincard.py-045). Re-derive once a full TTM/FY "
                        "is on file — this annualization assumes a flat run-rate.")
                    put("fcf_yield_pct", _ann_fcf / mc * 100,
                        f"FCF(annualized) {_ann_fcf:,.0f} / market_cap {mc:,.0f}",
                        "annualized from a partial-period FCF — see ev_over_fcf note")
                    card["flags"].append(
                        f"ev_over_fcf/fcf_yield_pct ANNUALIZED from a {_cfo_days}d partial-"
                        f"period FCF ({fcf:,.0f} -> {_ann_fcf:,.0f}/yr) — no TTM/FY on file "
                        f"yet; treat as approximate, assumes a flat run-rate (fincard.py-045).")
                else:
                    put("ev_over_fcf", ev / fcf, f"EV {ev:,.0f} / FCF {fcf:,.0f}")
                    put("fcf_yield_pct", fcf / mc * 100, f"FCF {fcf:,.0f} / market_cap {mc:,.0f}")
            if eb and eb > 0:
                put("ev_over_ebitda", ev / eb, f"EV {ev:,.0f} / approx EBITDA {eb:,.0f}")
            if rev:
                put("ev_over_revenue", ev / rev, f"EV {ev:,.0f} / revenue {rev:,.0f}")
        if ni and ni > 0:
            put("pe", mc / ni, f"market_cap {mc:,.0f} / net_income {ni:,.0f}")
        if gv("equity") and gv("equity") > 0:
            pf = F.get("preferred_liq_pref") or {}
            pref = pf.get("value") or 0
            if not (pf.get("asof") and (F.get("equity") or {}).get("asof")
                    and pf["asof"] >= F["equity"]["asof"] and 0 < pref < gv("equity")):
                pref = 0    # stale, absent, or nonsensical preference — don't subtract
            ce = gv("equity") - pref
            pref_txt = (f"(equity {gv('equity'):,.0f} - preferred liquidation preference "
                        f"{pref:,.0f})" if pref else f"equity {gv('equity'):,.0f}")
            note = ("common equity only — preferred liquidation preference removed"
                    if pref else "no preferred liquidation preference tagged as of the equity date")
            put("price_over_book", mc / ce, f"market_cap {mc:,.0f} / {pref_txt}", note)
            put("book_value_per_share", ce / sh, f"{pref_txt} / shares {sh:,.0f}", note)
        bb, dv = ttm_vals.get("buybacks"), ttm_vals.get("dividends_paid")
        if bb:
            put("buyback_yield_pct", bb / mc * 100, f"buybacks {bb:,.0f} / market_cap {mc:,.0f}")
        if dv:
            put("dividend_yield_pct", dv / mc * 100, f"dividends_paid {dv:,.0f} / market_cap {mc:,.0f}")

        fcf = (D.get("fcf") or {}).get("value")
        nc0 = (D.get("net_cash") or {}).get("value") or 0
        if fcf and fcf > 0:
            sys.path.insert(0, str(ENGINE / "valuation"))
            import toolkit
            V = card["valuation"]
            V["_assumptions"] = ("MECHANICAL: 10y horizon, 10% discount, 2.5% terminal growth, "
                                 "base FCF = TTM. A ruler for sanity, never a thesis.")
            try:
                r = toolkit.reverse_dcf(px, sh, fcf, 0.10, 0.025, 10, net_cash=nc0)
                V["market_implied_fcf_growth_pct"] = round(r["implied_growth"] * 100, 2)
            except Exception as e:
                V["reverse_dcf_error"] = str(e)[:80]
            grid = {}
            for g in (-0.05, 0.0, 0.05, 0.10, 0.15):
                try:
                    out = toolkit.dcf([fcf * (1 + g) ** y for y in range(1, 11)],
                                      0.10, 0.025, net_cash=nc0, shares=sh)
                    ps = out.get("per_share") or (out.get("equity_value", 0) / sh if sh else None)
                    grid[f"{g * 100:+.0f}%"] = round(ps, 2) if ps else None
                except Exception:
                    grid[f"{g * 100:+.0f}%"] = None
            V["dcf_value_per_share_at_growth"] = grid
    elif not _non_primary:
        card["flags"].append("no live price and/or shares_out — market-derived values skipped")
    # else: _non_primary already carries its own NON-PRIMARY SECURITY flag above — a second,
    # generic "no live price" flag here would misdescribe a card that has both.

    # One flag, not six. A filer with no us-gaap facts for the universal concepts is not
    # mis-mapped — it reports under a different taxonomy. Six identical "no XBRL tag found"
    # rows describe that badly and bury the actual cause, which is a DATA-SOURCE gap.
    _missing = [c for c in UNIVERSAL if c not in F]
    if len(_missing) >= 3:
        _ns = [k for k in (cf.get("facts") or {})
               if k not in ("us-gaap", "dei", "srt", "invest", "ecd", "ffd")]
        # An extension namespace is NOT a taxonomy. The first version of this check called
        # any non-standard namespace one, so TBCV — a SPAC with a `spac:` extension — was
        # reported as "reports under spac, not us-gaap", which is meaningless. Only
        # `ifrs-full` is an actual alternate accounting taxonomy.
        _tax = [n for n in _ns if n.startswith("ifrs")]
        card["flags"] = [f for f in card["flags"] if not f.endswith("no XBRL tag found")]
        if _tax:
            # OUT OF SCOPE, decided 2026-08-18 after measuring rather than assuming. An IFRS
            # tag map was considered and rejected: the only two IFRS names carded were SPOT,
            # a foreign private issuer that files 6-K/20-F and therefore has NO QUARTERLY
            # structured data at all — so a tag map would still not produce a usable card,
            # because every derived value in this book is TTM — and MDXH, whose last 10-Q was
            # 2025-08-28, a year stale. Neither is held, in the universe, or on the bench.
            # Building a second dictionary to maintain forever for two dead names is cost
            # without a reader.
            card["flags"].append(
                f"OUT OF SCOPE — IFRS filer ({', '.join(_tax)}). This book's number pipeline "
                f"is us-gaap + quarterly (10-Q/10-K) by design; foreign private issuers report "
                f"on 20-F/6-K, so there is no quarterly series to build TTM from even with a "
                f"tag map. Do not underwrite from this card. Reconsider only if a name we "
                f"actually want to own turns out to be an FPI.")
        elif _ns:
            card["flags"].append(
                f"CORE CONCEPTS MISSING: {len(_missing)} of the six every us-gaap filer must "
                f"report ({', '.join(_missing)}) are absent. The issuer tags heavily under its "
                f"own `{_ns[0]}:` namespace ({sum(1 for _ in _ns)} extension ns), which "
                f"companyfacts drops — but that is an EXTENSION, not a different accounting "
                f"taxonomy. Nothing derived here can be trusted.")
        else:
            card["flags"].append(
                f"NO USABLE FACTS: {len(_missing)} core us-gaap concepts missing "
                f"({', '.join(_missing)}) and no alternate taxonomy present. Nothing derived on "
                f"this card can be trusted.")

    # Always, on every card — not only when something was missing.
    _foot_check(card, F, gaap)

    # DISPLAY_ONLY asserts these concepts feed nothing. If that stops being true the silence
    # it buys becomes a hidden defect, so the claim verifies itself here rather than relying
    # on anyone remembering the doctrine.
    # Plain-string word boundary rather than a regex: `re` is deliberately not imported in
    # this module and adding an import to a file two other sessions co-edit is a wider blast
    # radius than this check is worth.
    _f = " ".join(str((v or {}).get("formula") or "") for v in card["derived"].values()).lower()
    for _sep in "()/-+,x":
        _f = _f.replace(_sep, " ")
    _f = f" {_f} "
    for _c in DISPLAY_ONLY:
        if f" {_c} " in _f:
            card["flags"].append(
                f"DOCTRINE BREACH: `{_c}` is DISPLAY_ONLY in fincard.py (staleness recorded, "
                f"not flagged) but a derived value on this card now consumes it. Remove it "
                f"from DISPLAY_ONLY — a concept that feeds a number must flag.")

    flow_end = (F.get("cfo") or F.get("revenue") or {}).get("latest_quarter_end") \
        or (F.get("cfo") or F.get("revenue") or {}).get("period", "")[-10:]
    bal = (F.get("cash") or {}).get("asof")
    if flow_end and bal and flow_end < bal:
        card["flags"].append(f"MIXED PERIODS: flow figures end {flow_end} but balance sheet is "
                             f"{bal} — multiples mix eras; note it when quoting them")
    return card


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit("usage: fincard.py TICKER [--cik N] [--out FILE]")
    cik = a[a.index("--cik") + 1] if "--cik" in a else None
    card = build(a[0], cik)
    js = json.dumps(card, indent=1)
    if "--out" in a:
        Path(a[a.index("--out") + 1]).write_text(js)
        print(f"fincard: {a[0].upper()} -> {a[a.index('--out') + 1]} "
              f"({len(card['figures'])} figures, {len(card['derived'])} derived, "
              f"{len(card['flags'])} flags)")
    else:
        print(js)
