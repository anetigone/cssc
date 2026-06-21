"""Agent roles that propose Lean proof-hole completions."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Sequence

from ..proof_system.base import DiagnosticCategory, ParsedFeedback, ProofTask
from ..search.action import ActionCandidate, ActionGenerationRequest, ActionGenerator
from .chat_driver import ChatDriver
from .context import SummarizationResult
from ..search.memory import ProofMemory, memory_to_prompt
from .openai import (
    ChatConfig,
    ChatTransport,
    ModelAdapterError,
    UrllibChatTransport,
    chat_completions_url,
    choice_content,
)
from .tools import Tool


logger = logging.getLogger(__name__)

_FORBIDDEN_PROOF_COMMAND_RE = re.compile(
    r"^\s*(?:import\b|#(?:check|print|eval|reduce)\b)", re.IGNORECASE
)
_DIAGNOSTIC_START_RE = re.compile(
    r"^.*?:\d+:\d+:\s+(?:error(?:\([^)]*\))?|warning|information|hint):",
    re.IGNORECASE | re.MULTILINE,
)


class ChatActionGenerator(ActionGenerator):
    """Generate proof edits through a chat-completion endpoint."""

    def __init__(
        self,
        config: ChatConfig,
        *,
        transport: ChatTransport | None = None,
        tools: Sequence[Tool] | None = None,
        max_tool_rounds: int = 5,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibChatTransport()
        self.driver = ChatDriver(
            config=config,
            transport=self.transport,
            tools=tools or (),
            max_tool_rounds=max_tool_rounds,
        )

    def generate(self, request: ActionGenerationRequest) -> Sequence[ActionCandidate]:
        url = chat_completions_url(self.config.base_url)
        logger.debug(
            "Requesting chat completions: model=%s url=%s task_id=%s max_candidates=%d",
            self.config.model,
            url,
            request.task.task_id,
            request.max_candidates,
        )
        allow_tools = _should_allow_tools(request)
        messages: list[dict[str, Any]] = list(
            _build_messages(
                request,
                has_tools=allow_tools and bool(self.driver.tools),
            )
        )
        logger.info(
            "Proof tool access: task_id=%s attempt_index=%d enabled=%s",
            request.task.task_id,
            request.attempt_index,
            allow_tools and bool(self.driver.tools),
        )
        response = self.driver.complete(
            messages,
            final_n=request.max_candidates,
            allow_tools=allow_tools,
        )
        choices = response.get("choices")
        if not isinstance(choices, list):
            logger.error("Model response missing choices list: model=%s", self.config.model)
            raise ModelAdapterError("Model response is missing a choices list.")

        candidates: list[ActionCandidate] = []
        for index, choice in enumerate(choices[: request.max_candidates]):
            if not isinstance(choice, Mapping):
                continue
            proof_text, removed_commands = _clean_proof_text(choice_content(choice))
            if not proof_text:
                continue
            if removed_commands:
                logger.warning(
                    "Removed exploration commands from proof candidate: task_id=%s choice_index=%d commands=%d",
                    request.task.task_id,
                    index,
                    removed_commands,
                )
            candidates.append(
                ActionCandidate(
                    proof_text=proof_text,
                    action="openai_chat",
                    metadata={
                        "model": self.config.model,
                        "choice_index": index,
                        "finish_reason": choice.get("finish_reason"),
                        "removed_exploration_commands": removed_commands,
                    },
                )
            )
        logger.info(
            "Generated model candidates: model=%s task_id=%s candidates=%d",
            self.config.model,
            request.task.task_id,
            len(candidates),
        )
        if not candidates:
            logger.warning(
                "Model response produced no proof candidates: model=%s task_id=%s response=%s",
                self.config.model,
                request.task.task_id,
                json.dumps(response, ensure_ascii=False, default=str),
            )
        return tuple(candidates)


def _build_messages(
    request: ActionGenerationRequest, *, has_tools: bool = False
) -> list[dict[str, str]]:
    tool_guidance = (
        " Use check_lean_snippet for #check queries or scratch compilation before answering."
        if has_tools
        else ""
    )
    phase = request.metadata.get("proof_phase", "propose")
    if phase == "retry":
        phase_guidance = (
            " A previous attempt failed Lean checking. Reconsider the failing subargument or "
            "proof strategy, keeping verified parts when useful, and make the smallest change "
            "that resolves the reported Lean errors."
        )
    else:
        phase_guidance = (
            " First plan the mathematical construction and its Lean API usage, then return "
            "one coherent proof body."
        )
    return [
        {
            "role": "system",
            "content": (
                "You iteratively complete Lean 4 proof holes. Return only the full Lean proof "
                "body that replaces the marker; do not wrap it in markdown. The final answer "
                "must not contain import, #check, #print, #eval, or #reduce commands."
                + phase_guidance
                + tool_guidance
            ),
        },
        {"role": "user", "content": _build_user_prompt(request)},
    ]


def _build_user_prompt(request: ActionGenerationRequest) -> str:
    task = request.task
    feedback = request.previous_feedback
    parts = [
        f"Task id: {task.task_id}",
        f"Replace exactly this marker: {task.hole_marker}",
    ]
    problem = task.metadata.get("natural_language_problem")
    if isinstance(problem, str) and problem.strip():
        parts.extend(["Natural-language problem statement:", problem.strip()])
    informal_proof = task.metadata.get("natural_language_proof")
    if isinstance(informal_proof, str) and informal_proof.strip():
        parts.extend(
            [
                "Candidate natural-language proof to preserve when it is mathematically sound:",
                informal_proof.strip(),
            ]
        )
    proof_phase = request.metadata.get("proof_phase")
    if isinstance(proof_phase, str):
        parts.append(f"Proof loop phase: {proof_phase}")
    encoded_state = request.metadata.get("encoded_state")
    if encoded_state is not None and hasattr(encoded_state, "to_prompt_context"):
        parts.extend(["Controller state:", str(encoded_state.to_prompt_context())])
    previous_attempt = request.metadata.get("previous_attempt")
    summarized_context = request.metadata.get("summarized_context")
    has_summarized_context = isinstance(summarized_context, SummarizationResult) and summarized_context.was_summarized
    summary = summarized_context if has_summarized_context else None
    has_previous_attempt = isinstance(previous_attempt, Mapping)
    if has_previous_attempt:
        previous_proof = previous_attempt.get("proof_text")
        raw_output = previous_attempt.get("raw_output")
        if isinstance(previous_proof, str) and previous_proof.strip():
            parts.extend(
                ["Previous proof body to revise:", "```lean", previous_proof, "```"]
            )
        if summary is not None:
            if summary.concise_error:
                parts.extend(
                    [
                        "Summary of Lean compiler errors from that proof:",
                        summary.concise_error,
                    ]
                )
            if summary.strategy_hint:
                parts.extend(
                    [
                        "Suggested repair direction:",
                        summary.strategy_hint,
                    ]
                )
            if summary.relevant_history:
                parts.append("Key history from prior attempts:")
                for line in summary.relevant_history:
                    parts.append(f"- {line}")
        elif isinstance(raw_output, str) and raw_output.strip():
            compact_output = _compact_checker_output(raw_output)
            parts.extend(
                [
                    "Relevant Lean compiler errors from that proof:",
                    "```text",
                    compact_output,
                    "```",
                ]
            )
    retrieved = request.metadata.get("retrieved_results") or ()
    if isinstance(retrieved, Sequence) and retrieved:
        # ``None`` means "no summary produced": keep every retrieved snippet.
        # An empty tuple means the summarizer explicitly kept nothing: drop all.
        retained = summary.retained_retrieved if summary is not None else None
        selected = _filter_retrieved(retrieved, retained)
        if selected:
            parts.append("Retrieved Lean snippets:")
            for name, snippet in selected:
                parts.extend([f"- {name}", "```lean", snippet, "```"])
    parts.extend(["Lean source template:", "```lean", task.source_template, "```"])
    proof_memory = request.metadata.get("proof_memory")
    if isinstance(proof_memory, ProofMemory):
        memory_block = memory_to_prompt(proof_memory)
        if memory_block:
            # The self-managed compact memory is the loop's primary carried
            # context; when it carries open goals and prior failures it
            # supersedes the raw feedback list.
            parts.extend(["Compact proof memory:", memory_block])
    if feedback and not _has_memory_context(proof_memory):
        parts.extend(["Previous checker feedback:"])
        # If the previous attempt's full output is already shown above, the
        # most recent feedback entry repeats the same diagnosis; keep the
        # older history instead.
        feedback_slice = feedback[:-1] if has_previous_attempt else feedback[-3:]
        for item in feedback_slice:
            parts.append(_feedback_line(item))
    return "\n".join(parts)


def _has_memory_context(proof_memory: Any) -> bool:
    """True when the compact memory already carries retry context.

    The first iteration ships an empty memory, so we still want the raw
    feedback list then. Once the loop has folded attempts into memory, the
    memory block is the authoritative compact view.
    """
    if not isinstance(proof_memory, ProofMemory):
        return False
    return bool(
        proof_memory.failed_approaches
        or proof_memory.open_goals
        or proof_memory.established_facts
        or proof_memory.lean_api_lessons
    )


def _feedback_line(feedback: ParsedFeedback) -> str:
    return f"- {feedback.category.value}: {feedback.message}"


def _filter_retrieved(
    retrieved: Sequence[Any], retained: tuple[str, ...] | None
) -> list[tuple[str, str]]:
    """Pick ``(name, snippet)`` pairs to show.

    When the context summarizer named specific snippets to keep (``retained``),
    honor that allowlist so the prompt only carries what it judged useful.
    Otherwise fall back to the first few retrieved items.
    """
    keep = (
        None
        if retained is None
        else {
            name.strip()
            for name in retained
            if isinstance(name, str) and name.strip()
        }
    )
    pairs: list[tuple[str, str]] = []
    for item in retrieved[:5]:
        name = getattr(item, "name", None)
        snippet = getattr(item, "snippet", None)
        if not (isinstance(name, str) and isinstance(snippet, str)):
            continue
        if keep is not None and name not in keep:
            continue
        pairs.append((name, snippet))
    return pairs


def _should_allow_tools(request: ActionGenerationRequest) -> bool:
    if not request.previous_feedback:
        return True
    return request.previous_feedback[-1].category in {
        DiagnosticCategory.UNKNOWN_IDENTIFIER,
        DiagnosticCategory.INVALID_REFERENCE,
    }


def _clean_proof_text(content: str) -> tuple[str, int]:
    stripped = content.strip()
    fence = re.fullmatch(r"```(?:lean)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    lines = stripped.splitlines()
    kept = [line for line in lines if not _FORBIDDEN_PROOF_COMMAND_RE.match(line)]
    removed = len(lines) - len(kept)
    return "\n".join(kept).strip(), removed


def _compact_checker_output(raw_output: str, *, max_chars: int = 6_000) -> str:
    """Keep fatal diagnostic blocks and discard noisy info/#check output."""
    matches = list(_DIAGNOSTIC_START_RE.finditer(raw_output))
    if not matches:
        return _truncate_text(raw_output.strip(), max_chars)

    blocks: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw_output)
        blocks.append(raw_output[match.start() : end].strip())

    errors = [
        block
        for block in blocks
        if re.search(r":\s+error(?:\([^)]*\))?:", block.splitlines()[0], re.IGNORECASE)
    ]
    selected = errors or [
        block
        for block in blocks
        if re.search(r":\s+warning:", block.splitlines()[0], re.IGNORECASE)
    ]
    if not selected:
        selected = blocks
    return _truncate_text("\n\n".join(selected[:6]), max_chars)


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[diagnostics truncated]"
