# Code

Thirteen components of the engine, copied verbatim from the private repo and re-copied by
an automated daily job — so what you are reading is what ran last night, not a snapshot
someone remembered to update. Nothing here touches an account, a position or a broker.

The layout mirrors the private one.

## `agent/` — the autonomous book's staff

The design conceit is an org chart: a portfolio manager that runs once a day, and staff
that prepare its desk overnight for free. Everything below the PM is code or a local
model — zero API cost — so the expensive session spends its context deciding rather than
gathering.

| File | Lines | What it demonstrates |
|---|---:|---|
| [`vp.py`](agent/vp.py) | 415 | **An 11-stage nightly sweep** run in dependency order, whose single deliverable is the brief the PM opens in the morning. It replaced three commands stapled together in a crontab line whose only output was three log files nobody read — the lesson being that a pipeline needs one artifact a human actually opens, or it is not a pipeline. |
| [`bench.py`](agent/bench.py) | 584 | **A durable work queue for overnight local-model reading.** Leased tasks, stateless workers, resume-after-crash, a fixed time budget, and a guardrail that drops any claimed finding whose supporting quote does not literally occur in the source text. Takes a GPU slot per read rather than per run, so a five-hour job never holds the machine's only card. |
| [`roster.py`](agent/roster.py) | 361 | **The org chart as data, not prose.** Each role carries the files it reads and writes, so the system can `stat` them and tell the PM what actually ran and how fresh its output is. Prose in a prompt goes stale silently; a row that can be checked against the filesystem cannot. |
| [`triggers.py`](agent/triggers.py) | 341 | The coded escalation engine — runs every few minutes in market hours, fires on state change, and formats one line a human can read on a phone. No model in the loop: the things worth waking someone for are all expressible as arithmetic. |
| [`scout.py`](agent/scout.py) | 297 | The funnel that turns raw intel into a ranked shortlist before the expensive session sees anything. |
| [`dossier.py`](agent/dossier.py) | 474 | **Extraction with a guardrail the model cannot talk past.** A local model pulls security and contract terms from keyword-located passages; every extraction must carry a verbatim quote, and plain code then checks that the quote occurs in the source. A fluent hallucination becomes a discarded record instead of a footnote in a report. |
| [`relevance.py`](agent/relevance.py) | 108 | News materiality scoring on the local GPU at zero API cost, bounded to the current universe, with escalation for high-scoring items. The scores rank; they never filter what the primary analysis sees. |

## `research/` — reading primary documents

| File | Lines | What it demonstrates |
|---|---:|---|
| [`navindex.py`](research/navindex.py) | 69 | The smallest useful shape of "local model as navigation aid": it appends pointers to an index — where the revenue drivers, contract terms and specific risks live — while the primary analysis still reads the raw text. |
| [`predigest.py`](research/predigest.py) | 64 | Local pre-digests of filings, generated so an expensive session can start from a map instead of 300,000 characters of prose. |

## `valuation/`, `scanners/`, and one small file that matters

| File | Lines | What it demonstrates |
|---|---:|---|
| [`valuation/toolkit.py`](valuation/toolkit.py) | 118 | DCF, **reverse DCF**, risk-adjusted NPV sum-of-parts, comps. The reverse DCF is the interesting one: rather than producing a price, it solves for the growth and margin the current price already implies — turning "is this cheap?" into "what would have to be true?", a question you can actually check against the filings. |
| [`scanners/insider_cluster.py`](scanners/insider_cluster.py) | 211 | Parses ownership forms to find the multi-buyer fingerprint — several insiders buying in the same window — rather than any single purchase, which is noise. |
| [`scanners/enrich_board.py`](scanners/enrich_board.py) | 103 | Enriches a candidate board with the size and liquidity facts that decide whether a name is even investable. |
| [`edgar_identity.py`](edgar_identity.py) | 76 | Small, but it is the shape of a lesson: ten modules had hardcoded the same regulator-required contact string. It now resolves from config with a **hard failure if absent** — no silent generic fallback, because an unidentified request gets you blocked and a fetch pipeline that quietly degrades to "blocked" is the exact silent-failure mode this project keeps designing out. |

_Read `dossier.py` for the guardrail, `bench.py` for the queue, `roster.py` for the idea
that documentation which cannot be checked is documentation that will lie to you._
