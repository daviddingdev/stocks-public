# Method

How a name gets from "something looks odd here" to a decision, and what stops the process
from fooling itself along the way. This is the part of the project I would most want
someone to read: the code is downstream of these rules.

## 1. Source by mechanism, never by screen

The question is never "what looks cheap." It is **who is selling for a reason that has
nothing to do with value** — forced index deletion, a spin-off that lands in the wrong
shareholder's hands, a complexity discount nobody is paid to resolve, a post-deal share
overhang, a delisting-threat drawdown. A screen tells you what is cheap and so does
everyone else's screen; a mechanism tells you *why the price is wrong right now* and
roughly when the pressure ends.

Practically: a candidate has to arrive with a named mechanism and a rough clock. "Trades
at 8x" is not an entry.

## 2. Build the evidence pack before forming a view

Filings are pulled from the regulator's primary source and sliced with byte offsets, so
every later claim can point at a document rather than at a memory of one. A local model
writes navigation notes over the pack — *where* the segment disclosure, the contract terms
and the specific (non-boilerplate) risks live — and that is all it does.

The rule is absolute: **local-model output is additive**. It may rank, index, and point.
It may never remove something from what the primary analysis reads. A mis-scored item that
silently disappears from the decision context is an error you cannot detect afterwards.

That rule was bought with evidence. When a candidate model was benchmarked to replace the
incumbent reader, it fabricated five financial figures out of a document slice containing
none — confidently, in exactly the expected format. Every local model since is benchmarked
with a mechanical test: **every figure it emits must appear verbatim in the text it was
given**, checked by grep, not by judgement.

## 3. Model it independently, and write the bear case first

The valuation is built from the filings rather than from anyone's estimates, and the tool
is chosen to fit the asset — a DCF for a stable operator, a reverse DCF when the question
is "what is priced in," an rNPV sum-of-parts when the value is in discrete binary events.
Forcing one method onto every asset is how you get a number you cannot defend.

The bear case is written as an argument, not as a "risks" list. If it cannot be made
convincing, that is a finding about the analysis, not about the company.

## 4. Attack the result before believing it

Every teardown is handed to an independent adversarial pass whose instruction is to
**refute** — against the primary documents — and to mark each claim overturned, hardened,
or unchanged. It is not a review; it is an opponent with the same evidence.

Concrete things it has overturned, in its own words but with the names removed:

- A **cash figure the analysis computed itself** was wrong because the company's own
  filings define the measure differently — client float was sitting inside the cash line.
  Every valuation output moved, and the payoff ratio moved with them.
- A margin assumption was **eighteen months stale**; management had updated the figure on
  a recent call. Correcting it cut the modelled upside by two thirds and flipped the
  conclusion from "fairly priced" to "above fair value."
- A segment metric had been **silently redefined between filings** — from an inclusion
  list to an exclusion list, with no transition sentence — making a growth rate
  incomparable to the one printed beside it.
- A claimed moat was **not the moat**: the filings said the business was exempt from the
  licensing regime the analysis had credited it for; the licences were bought for a future
  product line.
- A headline regulatory "cap" was a **tailwind misread as a headwind** once measured
  against the prior year's actual volumes rather than the prior year's cap.

It also, more than once, overturned claims that favoured the *bear* case. That is the
signal that it is testing rather than agreeing with whoever wrote it.

## 5. Let code do the checking

Judgement is the model's job; verification is not.

- A claimed contradiction must carry a **verbatim quote that appears in the source** —
  code checks the string and drops the claim if it does not.
- Extraction and comparison are **separated**: the model extracts figures, plain code does
  every comparison and every arithmetic step.
- Numbers in written memos are **audited against the filings** by a separate pass before
  the memo is allowed to inform a decision.
- After every session that could have acted, the system **reconciles what it believes
  against what the account actually holds**, and raises an alarm on any difference it
  cannot explain.

## 6. Assume the pipeline is lying until it proves otherwise

The bugs that cost the most were not crashes. One example: the filing fetcher truncated
documents at a fixed character count, silently dropping the later sections — so an insider
analysis was drawn from a document that appeared complete and was not. It was caught by
the adversarial pass, not by the fetcher.

The standing conclusion: **silent, data-shaped, exit-0 failure is the failure mode to
design against.** Every stage that can produce a plausible wrong answer gets an assertion
about what it was supposed to *achieve*, not just an exit code.

_Last updated August 2026._
