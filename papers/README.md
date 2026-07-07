# Paper Draft

This directory contains the working manuscript for an AAAI submission on
budget-aware control for LLM-guided Lean proof search.

## Positioning

The paper treats the two execution modes asymmetrically:

- `minimal` is the strong iterative-refinement baseline;
- `structured` is the state representation and execution substrate for the
  proposed budget-aware controller.

The central claim is not that structure is useful by itself. The claim to test
is that a controller can solve more tasks under the same resource budget when
it makes verifier-grounded decisions over explicit obligations, branches,
failure evidence, and action costs.

## Files

- `main.tex`: format-neutral LaTeX entry point.
- `sections/`: the manuscript body.
- `references.bib`: a small, verified starter bibliography.

The draft intentionally uses `\todo{...}` for missing empirical values,
dataset counts, model names, and implementation details that must be frozen
before submission. No result is stated as measured until it has a value and a
traceable experiment behind it.

## Immediate writing/experiment TODOs

1. Freeze the controller policy and decide whether its score is heuristic,
   learned from held-out traces, or reported in both variants.
2. Freeze task suites and prevent theorem/proof leakage.
3. Select cheap and strong model tiers and record provider-independent token
   accounting.
4. Run equal-budget comparisons across checker-call, token, wall-clock, and
   monetary budgets.
5. Fill the result tables and replace the provisional related-work entries
   with venue-final metadata where available.
6. Move the manuscript into the official AAAI style only after the content and
   page budget stabilize.
