# Code

Four components of the engine, copied verbatim from the private repo. Nothing here
touches an account, a position, or a broker — these are the parts that read documents and
do arithmetic.

| File | Lines | What it demonstrates |
|---|---:|---|
| [`valuation_toolkit.py`](valuation_toolkit.py) | 118 | **Fit-for-purpose valuation, never one hammer.** DCF, reverse DCF, risk-adjusted NPV sum-of-parts, and comps in one small module. The reverse DCF is the interesting one: instead of producing a price, it solves for the growth and margin the *current* price already implies, which turns "is this cheap?" into "what would have to be true?" — a question that can actually be checked against the filings. |
| [`dossier.py`](dossier.py) | 474 | **Extraction with a guardrail the model cannot talk its way past.** A local model pulls security and contract terms out of keyword-located passages; every extraction must carry a verbatim quote, and plain code then checks that the quote occurs in the source. Anything that fails the string check is dropped, so a fluent hallucination becomes a discarded record rather than a footnote in a report. |
| [`navindex.py`](navindex.py) | 70 | The smallest useful shape of "local model as navigation aid": it reads each filing and appends pointers to an index — where the revenue drivers, contract terms and specific (non-boilerplate) risks live — while the primary analysis still reads the raw text. Additive by construction. |
| [`relevance.py`](relevance.py) | 108 | News materiality scoring on the local GPU at zero API cost, with the scorer's memory bounded to the current universe, an escalation push for high-scoring items, and a compact brief for the expensive weekly session to read. The scores are a ranking layer — they never remove an item from what the primary analysis sees. |

_Read `dossier.py` if you only read one — the verbatim-quote check is the whole thesis of
the project in about ten lines._
