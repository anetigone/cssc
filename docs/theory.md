# A Cost-Theoretic Framing of Proof Search Control

This document gives a formal theory for the claim that search control can make
an LLM-guided formal proof agent cheaper under a fixed budget. The main point is
not that search is always cheap. The claim is conditional: once proof attempts
are modeled as costly computations with verifier-observable outcomes, a
controller that allocates attempts by expected success gain per unit cost can
strictly dominate fixed sampling or direct escalation policies.

## 1. Problem Setting

Let a proof task be an instance \(x \in \mathcal{X}\). A proof agent interacts
with a formal verifier, such as Lean, through a finite sequence of computation
actions. An action may generate a candidate proof edit, retrieve relevant
lemmas, repair a failed candidate, call a stronger model, backtrack, prune a
branch, or stop.

We write the action set as

\[
\mathcal{A}
= \{\textsf{expand}, \textsf{retrieve}, \textsf{repair},
     \textsf{escalate}, \textsf{backtrack}, \textsf{prune}, \textsf{stop}\}.
\]

Each non-stop action \(a \in \mathcal{A}\) has a nonnegative cost

\[
c(a) > 0,
\]

which may combine token cost, wall-clock time, verifier calls, retrieval calls,
or monetary cost. The exact cost scale is application-dependent, but the theory
only requires that costs are additive and comparable.

The agent maintains a search state \(s_t\), which includes the current proof
prefix, verifier feedback, branch history, retrieved context, and remaining
budget. A policy \(\pi\) maps states to actions:

\[
\pi : \mathcal{S} \to \Delta(\mathcal{A}).
\]

The verifier induces an absorbing success event. Once a candidate proof is
accepted, the process stops and the task is solved. If the budget is exhausted
first, the task fails.

For a fixed task \(x\), policy \(\pi\), and budget \(B\), define

\[
C_\pi(x)
\]

as the random total cost spent before success or termination, and

\[
S_\pi(x) \in \{0,1\}
\]

as the event that the policy solves the task.

The primary evaluation objective is either:

\[
\max_\pi \Pr[S_\pi(x)=1 \mid C_\pi(x) \le B],
\]

or, for tasks that are eventually solved,

\[
\min_\pi \mathbb{E}[C_\pi(x) \mid S_\pi(x)=1].
\]

The architecture in the main README instantiates this setting with Lean
proof-completion tasks, a `ProofSystemAdapter`, a `BudgetManager`, a trace
store, and a controller that chooses among expansion, retrieval, repair,
escalation, pruning, and stopping.

## 2. Sequential Candidate Search

We first study a simple model that captures the cost of trying proof candidates
until one is accepted by the verifier.

Suppose a state \(s\) has candidate actions \(1,\ldots,n\). Trying candidate
\(i\) costs \(c_i > 0\). It succeeds with probability \(p_i \in [0,1]\). The
verifier checks success exactly, and the agent stops after the first accepted
candidate.

For an ordering \(\sigma\) of the candidates, the expected cost is

\[
\mathbb{E}[C_\sigma]
= \sum_{k=1}^{n}
  c_{\sigma(k)}
  \prod_{\ell < k} (1 - p_{\sigma(\ell)}).
\]

This expression is the expected amount spent before the first success, or
before all candidates have been exhausted.

### Theorem 1: Optimal Ordering by Cost per Success Probability

Assume candidate outcomes are independent conditional on the current state, and
the agent stops at the first success. Among all fixed orderings of candidates,
an ordering that sorts candidates by nondecreasing ratio

\[
\frac{c_i}{p_i}
\]

minimizes expected cost, with candidates satisfying \(p_i=0\) placed last.

Equivalently, candidates should be tried in nonincreasing order of

\[
\frac{p_i}{c_i},
\]

their expected success probability per unit cost.

#### Proof

It is enough to consider adjacent swaps. Suppose two adjacent candidates \(i\)
and \(j\) are reached after some common prefix whose failure probability is
\(q\). The expected contribution of trying \(i\) before \(j\) is

\[
q(c_i + (1-p_i)c_j).
\]

The expected contribution of trying \(j\) before \(i\) is

\[
q(c_j + (1-p_j)c_i).
\]

Trying \(i\) first is no worse exactly when

\[
c_i + (1-p_i)c_j
\le
c_j + (1-p_j)c_i.
\]

Canceling terms gives

\[
p_j c_i \le p_i c_j,
\]

which is equivalent to

\[
\frac{c_i}{p_i} \le \frac{c_j}{p_j}
\]

when \(p_i,p_j>0\). Thus any inversion of this order can be swapped without
increasing expected cost. Repeatedly removing inversions yields a globally
optimal ordering. Candidates with \(p_i=0\) can never cause success and should
not precede any positive-probability candidate. \(\square\)

### Interpretation

The controller score used in the README,

\[
\textsf{score}(s,a)
=
\frac{\widehat{\textsf{success\_gain}}(s,a)}
      {\widehat{\textsf{cost}}(s,a)},
\]

is justified by Theorem 1 when the estimated success gain approximates
\(p_i\). This does not require the estimates to be perfect for the theorem to
be meaningful. The theorem supplies the target quantity that the empirical
system should try to estimate from model confidence, verifier progress,
retriever similarity, branch history, and diagnostic categories.

## 3. Two-Tier Model Control

The MVP should use a two-tier model policy. A cheap model proposes short Lean
tactics, small proof terms, and lightweight repairs. A strong model is reserved
for detailed proof completion or decomposition on branches that the controller
believes are worth the cost.

The simplest comparison is between direct escalation to a strong model and a
policy that first tries a cheap tactic-search action.

Let:

- \(C > 0\) be the cost of direct strong-model escalation;
- \(c > 0\) be the cost of a cheap search action;
- \(p \in [0,1]\) be the probability that the cheap action solves the task;
- the strong model is called only if the cheap action fails.

### Theorem 2: Condition for Search-Then-Escalate to Be Cheaper

The expected cost of the search-then-escalate policy is

\[
c + (1-p)C.
\]

This policy is strictly cheaper in expectation than direct escalation whenever

\[
c < pC.
\]

#### Proof

The cheap action is always paid, contributing \(c\). Escalation is paid only
when the cheap action fails, which occurs with probability \(1-p\), contributing
\((1-p)C\). Thus

\[
\mathbb{E}[C_{\textsf{cheap-first}}]
= c + (1-p)C.
\]

Direct escalation costs \(C\). Cheap-first is strictly cheaper exactly when

\[
c + (1-p)C < C,
\]

which simplifies to

\[
c < pC.
\]

\(\square\)

### Interpretation

This theorem formalizes a central design principle of the architecture:
cheap proof expansion, retrieval, or repair is worthwhile when its cost is
smaller than the expected escalation cost it avoids. In experiments, the trace
store can estimate \(p\), \(c\), and \(C\) for different error categories and
task levels.

This theorem only covers direct cheap success. In practice, cheap tactic search
may also be useful because it improves the state before a later strong-model
call. The next theorem captures that case.

Let \(s_0\) be the initial proof-search state. A direct strong-model attempt at
\(s_0\) costs \(C_H\) and succeeds with probability \(p_H(s_0)\). A cheap
search action costs \(c_L\), succeeds directly with probability \(p_L\), and if
it does not solve the task, moves the agent to a new state \(s_1\). A later
strong-model attempt at \(s_1\) succeeds with probability \(p_H(s_1)\).

For this comparison, measure expected cost per solved task. Direct strong
completion has expected cost per success

\[
\frac{C_H}{p_H(s_0)}.
\]

Cheap-search-then-strong has success probability

\[
p_L + (1-p_L)p_H(s_1),
\]

and expected cost

\[
c_L + (1-p_L)C_H.
\]

### Theorem 3: State-Improving Cheap Search Before Strong Completion

Cheap-search-then-strong has lower expected cost per solved task than direct
strong completion whenever

\[
\frac{c_L + (1-p_L)C_H}
     {p_L + (1-p_L)p_H(s_1)}
<
\frac{C_H}{p_H(s_0)}.
\]

Equivalently,

\[
c_L p_H(s_0)
<
C_H\left(
  p_L(1-p_H(s_0))
  + (1-p_L)(p_H(s_1)-p_H(s_0))
\right).
\]

#### Proof

The direct policy pays \(C_H\) and succeeds with probability \(p_H(s_0)\), so
its expected cost per success is \(C_H/p_H(s_0)\). The two-tier policy always
pays \(c_L\). It pays \(C_H\) only if the cheap action fails, which happens with
probability \(1-p_L\). Its total success probability is the probability of
cheap success plus the probability of cheap failure followed by strong success:

\[
p_L + (1-p_L)p_H(s_1).
\]

Thus its expected cost per success is

\[
\frac{c_L + (1-p_L)C_H}
     {p_L + (1-p_L)p_H(s_1)}.
\]

Requiring this to be smaller than \(C_H/p_H(s_0)\) and multiplying by positive
denominators gives

\[
p_H(s_0)(c_L + (1-p_L)C_H)
<
C_H(p_L + (1-p_L)p_H(s_1)).
\]

Rearranging yields

\[
c_L p_H(s_0)
<
C_H\left(
  p_L(1-p_H(s_0))
  + (1-p_L)(p_H(s_1)-p_H(s_0))
\right).
\]

\(\square\)

### Interpretation

The right-hand side has two benefits. The term

\[
p_L(1-p_H(s_0))
\]

is the direct benefit of cheap success: the cheap model may solve tasks that
would otherwise require a strong call. The term

\[
(1-p_L)(p_H(s_1)-p_H(s_0))
\]

is the state-improvement benefit: even when cheap search does not solve the
task, it may create verifier feedback, a longer verified prefix, retrieved
lemmas, or a decomposition that raises the conditional success probability of
the strong model.

This theorem justifies treating strong-model calls as high-cost macro-actions
such as `escalate_detailed_proof` or `escalate_decompose`, not as another
ordinary sampler. The controller should escalate when cheap search has either
saturated or produced enough evidence that \(p_H(s)\) is high relative to the
strong-model cost.

## 4. Repair Versus Regeneration

Verifier feedback is valuable because it changes the conditional probability
that a repair action will succeed.

Suppose a failed candidate produces diagnostic category \(e\), such as parser
error, unknown identifier, type mismatch, unsolved goals, or timeout.

Let:

- \(c_r(e)\) be the cost of attempting repair after diagnostic \(e\);
- \(p_r(e)\) be the conditional probability that repair succeeds;
- \(c_g\) be the cost of generating a fresh candidate;
- \(p_g\) be the probability that a fresh candidate succeeds.

### Theorem 4: When Repair Dominates Regeneration

After observing diagnostic \(e\), repair has lower expected cost per success
than fresh regeneration whenever

\[
\frac{c_r(e)}{p_r(e)}
<
\frac{c_g}{p_g}.
\]

Equivalently,

\[
\frac{p_r(e)}{c_r(e)}
>
\frac{p_g}{c_g}.
\]

#### Proof

This is an immediate application of Theorem 1 to two available actions:
repair and regeneration. The action with smaller cost per success probability
should be attempted first. \(\square\)

### Interpretation

Different Lean diagnostics should induce different control decisions. A parser
error may have high repairability, so repair can dominate regeneration. A
repeated unknown identifier may make retrieval more attractive. A persistent
unsolved goal may lower \(p_r(e)\), making decomposition or escalation better.

Thus the feedback parser is not merely an engineering convenience. It supplies
state information needed to estimate the value of computation.

## 5. Residual Value and Verifier Progress

Absolute value estimates from language models can be poorly calibrated.
Verifier feedback enables a more stable residual-value formulation.

Let \(s\) be a parent state and \(s'\) be a child state produced by action
\(a\). Let

\[
\phi(s) \in \mathbb{R}^d
\]

be a verifier-derived progress feature vector. Possible components include:

- accepted proof-prefix length;
- diagnostic category rank;
- number of unsolved goals;
- approximate goal size;
- number of repeated failures on the branch;
- whether a candidate moved from syntax failure to semantic proof obligation.

For a weight vector \(w \in \mathbb{R}^d\), define residual progress:

\[
\Delta_w(s,a,s')
=
w^\top(\phi(s') - \phi(s)).
\]

The controller can score an action by

\[
\textsf{score}(s,a)
=
\frac{\mathbb{E}[\Delta_w(s,a,S') \mid s,a]}
      {c(a)}.
\]

This is a proof-search analogue of value of computation: spend a computation
step when its expected verifier-observable improvement per unit cost is high.

### Proposition 5: Residual Scoring Recovers Cost per Success

If the progress feature is the binary success indicator

\[
\phi(s') = \mathbf{1}\{s' \text{ is accepted by the verifier}\},
\]

and the parent state is not yet accepted, then residual scoring becomes

\[
\frac{\mathbb{E}[\Delta(s,a,S')]}{c(a)}
=
\frac{\Pr[S' \text{ accepted} \mid s,a]}{c(a)}.
\]

Thus residual progress per cost reduces exactly to success probability per
cost in the one-step candidate model.

#### Proof

Because the parent is not accepted, \(\phi(s)=0\). The child feature is \(1\)
exactly on success and \(0\) otherwise. Therefore

\[
\mathbb{E}[\Delta(s,a,S')]
=
\mathbb{E}[\phi(S')]
=
\Pr[S' \text{ accepted} \mid s,a].
\]

Dividing by \(c(a)\) gives the claim. \(\square\)

### Interpretation

The residual-value design in the README is a generalization of the optimal
candidate ordering theorem. Instead of using only binary success, the controller
uses verifier-derived partial progress as a denser signal.

## 6. Budget-Conditioned Control

Let \(b \in [0,1]\) denote the fraction of remaining budget. A budget-aware
controller may use a priority rule of the form

\[
\textsf{priority}(s)
=
\frac{V(s)^{\gamma(b)}}{\widehat{c}_{\textsf{expand}}(s)}.
\]

Here \(V(s)\) estimates branch value, and \(\gamma(b)\) changes with the
remaining budget. A larger \(\gamma(b)\) concentrates search on high-value
branches, while a smaller \(\gamma(b)\) keeps exploration broader.

This framework does not by itself prove that a particular schedule
\(\gamma(b)\) is optimal. Instead, it states the policy class to be evaluated.
The formal results above justify the local decision rule: at any state, prefer
actions and branches with higher expected progress or success per unit cost.

## 7. What the Theory Does and Does Not Prove

The theory proves conditional optimality statements under an explicit model:

- if candidate success probabilities and costs are known, candidates should be
  ordered by \(p_i/c_i\);
- cheap search before escalation is cheaper exactly when \(c < pC\);
- cheap tactic search before strong detailed proof is cheaper when direct cheap
  success plus state improvement raises expected strong-model value enough to
  offset cheap-search cost;
- repair is preferable to regeneration when its diagnostic-conditioned success
  per cost is higher;
- residual verifier progress per cost generalizes success probability per cost.

The theory does not prove:

- that all search is cheaper than direct generation;
- that the LLM's probability estimates are calibrated;
- that every verifier progress signal corresponds to true nearness to a proof;
- that a heuristic budget schedule is globally optimal for all proof tasks.

These are empirical questions. The role of the trace store is to estimate the
quantities that the theory identifies as decision-relevant:

\[
c(a), \quad p(a \mid s), \quad p_r(e), \quad
\mathbb{E}[\Delta(s,a,S')], \quad
p_H(s), \quad C_{\textsf{strong}}.
\]

## 8. Empirical Predictions

The theory suggests the following measurable predictions for the MVP:

1. Cheap-first search should reduce expected cost on task categories where
   \(c < pC\).
2. Cheap tactic search should make strong-model calls more effective when it
   increases \(p_H(s)\) through verified prefixes, diagnostics, retrieved
   lemmas, or decomposition hints.
3. Diagnostic-aware repair should outperform blind regeneration for error
   categories with high \(p_r(e)/c_r(e)\).
4. Retrieval should be useful mainly for diagnostics or states where it
   increases downstream success probability enough to offset retrieval cost.
5. Budget-aware policies should spend strong-model calls on branches with high
   estimated \(p_H(s)/C_H\), rather than after a fixed number of failures.
6. The advantage of cost-sensitive control should be largest when strong-model
   calls are expensive and cheap verifier-guided feedback is informative.

These predictions connect the formal theory to the proposed Lean experiments:
the system can record every action, cost, diagnostic category, progress signal,
branch value, escalation point, model tier, and final outcome, then test whether
the observed policy gains match the conditions above.
