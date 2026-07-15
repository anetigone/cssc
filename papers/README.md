# Paper Draft

This directory contains the working manuscript for an AAAI submission on
budget-aware control for LLM-guided Lean proof search.

## Positioning

The paper treats the two execution modes asymmetrically:

- `minimal` is the strong iterative-refinement baseline;
- `structured` is the state representation and execution substrate for the
  proposed budget-aware controller.

The central claim is not that structure is useful by itself. The claim to test
is whether verifier-grounded allocation improves the solve--cost frontier after
charging the full structured-state overhead. Hard-budget candidate selection,
within-set ordering, action-space expansion, cost-aware selection, and model
routing are treated as distinct mechanisms rather than one bundled gain.

## Files

- `main.tex`: format-neutral LaTeX entry point.
- `sections/`: the manuscript body.
- `references.bib`: a small, verified starter bibliography.

The draft intentionally uses `\todo{...}` for missing empirical values,
dataset counts, model names, and implementation details that must be frozen
before submission. No result is stated as measured until it has a value and a
traceable experiment behind it.

## Immediate writing/experiment TODOs

1. Freeze the exact heuristic/learned selector, shadow prices, cost-history
   snapshot, and unknown-cost behavior used by the confirmatory arm.
2. Implement and verify the controlled action masks and the fixed-versus-
   adaptive routing control under an identical cheap/strong portfolio.
3. Freeze the second public benchmark, models, paired seeds, nested budget
   vectors, non-inferiority margin, and leakage controls.
4. Establish ledger parity across minimal and structured arms, then run the
   matched-budget system comparisons and structured mechanism contrasts.
5. Fill the result tables with absolute accepted counts, paired intervals,
   unconditional cost, measurement coverage, and infrastructure counts.
6. Replace provisional citation metadata where needed and move to the official
   AAAI style only after the content and page budget stabilize.
