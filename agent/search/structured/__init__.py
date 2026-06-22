"""Structured execution mode: frontier / AND-OR search driver.

Phase 6 ships the structured *executor* that drives the state primitives
introduced in Phases 3-5 (:class:`ProofWorkspace`, :class:`ProofBranch`,
:class:`SearchAction`, :class:`FailureHypothesis`). It owns a mutable
:class:`Frontier` scheduler, a deterministic :class:`StructuredReducer`, and a
:class:`SolutionTracker`; the :class:`StructuredController` ties them into the
shared budget / metrics / trace pipeline used by the minimal loop.

The minimal loop never imports this package: the only entry point is the lazy
import in :func:`agent.search.factory.build_controller`, which fires only when
``--execution-mode structured`` is selected.
"""

from __future__ import annotations
