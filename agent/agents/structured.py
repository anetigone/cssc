"""Chat agent that emits typed structured search proposals."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Sequence

from agent.proof_system.workspace import (
    DEFAULT_ALLOWED_MUTATIONS,
    SearchAction,
    SearchActionKind,
)
from agent.search.action import ActionGenerationError, ActionGenerationRequest
from agent.search.structured.proposal import (
    PAYLOAD_KIND_CAPABILITY_TEST,
    PAYLOAD_KIND_CHANGE_REPRESENTATION,
    PAYLOAD_KIND_DECOMPOSE,
    PAYLOAD_KIND_IMPLEMENT,
    PAYLOAD_KIND_PROPOSE_ARGUMENT,
    PAYLOAD_KIND_REFINE_ARGUMENT,
    AlignmentSpec,
    ArgumentStepSpec,
    CapabilityTestPayload,
    ChangeRepresentationPayload,
    DecomposeChildSpec,
    DecomposePayload,
    ImplementPayload,
    ProposeArgumentPayload,
    RefineArgumentPayload,
    StructuredActionGenerator,
    StructuredActionProposal,
    structured_action_proposal_from_dict,
)

from .chat_driver import ChatDriver
from .openai import (
    ChatConfig,
    ChatTransport,
    ModelAdapterError,
    UrllibChatTransport,
    chat_completions_url,
    choice_content,
    output_budget_was_exhausted,
)
from .proof import (
    _build_user_prompt,
    _looks_like_dsml_tool_call,
    _response_preview,
    _should_allow_tools,
)
from .tools import Tool
from .tools.loop import (
    AGENT_PROVIDER_REQUESTS_KEY,
    AGENT_TOKEN_USAGE_KEY,
    AGENT_TOOL_CALLS_KEY,
)


logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_STRUCTURED_TOOL_BUDGET_FINAL_INSTRUCTION = (
    "The Lean tool budget is exhausted. Do not call tools again. Return only "
    "the final JSON object with a `proposals` array, following the structured "
    "action schemas from the system message. Never return a bare Lean proof "
    "body or markdown."
)


class ChatStructuredActionGenerator(StructuredActionGenerator):
    """Generate typed structured proposals through a chat-completion endpoint."""

    _is_structured_generator = True

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

    def generate(
        self, request: ActionGenerationRequest
    ) -> Sequence[StructuredActionProposal]:
        url = chat_completions_url(self.config.base_url)
        logger.debug(
            "Requesting structured chat proposals: model=%s url=%s task_id=%s",
            self.config.model,
            url,
            request.task.task_id,
        )
        allow_tools = _should_allow_tools(request)
        response = self.driver.complete(
            _build_structured_messages(
                request,
                has_tools=allow_tools and bool(self.driver.tools),
            ),
            final_n=1,
            allow_tools=allow_tools,
            tool_budget_final_instruction=(
                _STRUCTURED_TOOL_BUDGET_FINAL_INSTRUCTION
            ),
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelAdapterError("Model response is missing a choices list.")

        token_usage = response.get(AGENT_TOKEN_USAGE_KEY)
        usage_metadata = dict(token_usage) if isinstance(token_usage, Mapping) else {}
        provider_requests = response.get(AGENT_PROVIDER_REQUESTS_KEY)
        provider_metadata = list(provider_requests) if isinstance(provider_requests, (list, tuple)) else []
        tool_calls = response.get(AGENT_TOOL_CALLS_KEY)
        tool_metadata = list(tool_calls) if isinstance(tool_calls, (list, tuple)) else []
        proposals: list[StructuredActionProposal] = []
        errors: list[str] = []
        branch_id = str(request.metadata.get("branch_id", ""))
        for choice_index, choice in enumerate(choices[:1]):
            if not isinstance(choice, Mapping):
                continue
            content = choice_content(choice)
            try:
                decoded = _decode_json(content)
                for proposal_index, item in enumerate(_proposal_items(decoded)):
                    proposal = _proposal_from_model_item(item, branch_id=branch_id)
                    ok, validation_errors = proposal.validate()
                    if not ok:
                        errors.extend(
                            f"proposal[{proposal_index}]: {error}"
                            for error in validation_errors
                        )
                        continue
                    metadata = dict(proposal.metadata)
                    metadata.update(
                        {
                            "model": self.config.model,
                            "choice_index": choice_index,
                            "finish_reason": choice.get("finish_reason"),
                            "token_usage": usage_metadata,
                            "provider_requests": provider_metadata,
                            "tool_calls": tool_metadata,
                        }
                    )
                    proposals.append(
                        StructuredActionProposal(
                            action=proposal.action,
                            payload=proposal.payload,
                            score=proposal.score,
                            metadata=metadata,
                        )
                    )
            except ValueError as exc:
                errors.append(str(exc))

        if not proposals:
            response_content = (
                choice_content(choices[0])
                if choices and isinstance(choices[0], Mapping)
                else ""
            )
            if output_budget_was_exhausted(choices, usage_metadata):
                reason = "model_output_truncated"
            elif tool_metadata and _looks_like_dsml_tool_call(response_content):
                reason = "tool_call_after_budget"
            else:
                reason = "invalid_structured_output"
            logger.warning(
                "Structured model response produced no valid proposals: model=%s "
                "task_id=%s reason=%s errors=%s token_usage=%s",
                self.config.model,
                request.task.task_id,
                reason,
                errors,
                usage_metadata,
            )
            raise ActionGenerationError(
                reason,
                (
                    "Model exhausted its output budget before producing a "
                    "structured proposal."
                    if reason == "model_output_truncated"
                    else "Model requested another tool call after the structured "
                    "tool budget was exhausted."
                    if reason == "tool_call_after_budget"
                    else "Model produced no valid structured proposals."
                ),
                metadata={
                    "model": self.config.model,
                    "errors": tuple(errors),
                    "response_preview": _response_preview(response_content),
                    "token_usage": usage_metadata,
                    "provider_requests": provider_metadata,
                    "tool_calls": tool_metadata,
                },
            )
        logger.info(
            "Generated structured proposals: model=%s task_id=%s proposals=%d kinds=%s",
            self.config.model,
            request.task.task_id,
            len(proposals),
            [proposal.action.kind.value for proposal in proposals],
        )
        return tuple(proposals)


def _build_structured_messages(
    request: ActionGenerationRequest, *, has_tools: bool = False
) -> list[dict[str, str]]:
    tool_guidance = (
        " You may use check_lean_snippet for #check queries or scratch compilation before returning JSON."
        if has_tools
        else ""
    )
    selected = request.metadata.get("selected_test_action")
    selected_guidance = ""
    if isinstance(selected, Mapping):
        selected_guidance = (
            " A failure hypothesis selected this test action; prefer emitting a "
            f"{selected.get('kind')} proposal if it is still relevant."
        )
    return [
        {
            "role": "system",
            "content": (
                "You are the single structured Lean proof agent. Return only JSON, "
                "never markdown. Emit an object with a `proposals` array. Each proposal "
                "must describe one action for the current branch. Allowed kinds are "
                "`implement`, `repair_implementation`, `run_capability_test`, "
                "`decompose`, `propose_argument`, `refine_argument`, and "
                "`change_representation`. Use IMPLEMENT/REPAIR for proof bodies, "
                "RUN_CAPABILITY_TEST to probe missing Lean capabilities, DECOMPOSE to "
                "split a hard obligation into helper Lean statements with proof holes, "
                "and argument/representation actions only when the mathematical plan "
                "itself should be recorded before implementation. Do not include "
                "`allowed_mutations`; the controller fills the conservative scope."
                + selected_guidance
                + tool_guidance
                + "\n\nMinimal schemas:\n"
                '{"kind":"implement","rationale":"...","proof_text":"..."}\n'
                '{"kind":"run_capability_test","rationale":"...","requirement":"...","signature":"#check ..."}\n'
                '{"kind":"decompose","rationale":"...","strategy":"...","children":[{"child_id":"helper","statement":"lemma helper : ... := by\\n  {{proof}}","dependency_ids":[]}]}\n'
                '{"kind":"propose_argument","rationale":"...","steps":[{"step_id":"s1","claim":"..."}],"alignments":[{"argument_step_id":"s1","relation":"unaligned"}]}'
            ),
        },
        {"role": "user", "content": _build_user_prompt(request)},
    ]


def _decode_json(content: str) -> Any:
    stripped = content.strip()
    fence = _JSON_FENCE_RE.fullmatch(stripped)
    if fence:
        stripped = fence.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc


def _proposal_items(decoded: Any) -> list[Mapping[str, Any]]:
    if isinstance(decoded, list):
        items = decoded
    elif isinstance(decoded, Mapping):
        if isinstance(decoded.get("proposals"), list):
            items = decoded["proposals"]
        elif isinstance(decoded.get("proposal"), Mapping):
            items = [decoded["proposal"]]
        else:
            items = [decoded]
    else:
        raise ValueError("structured response must be a JSON object or array")
    if not items:
        return []
    proposals: list[Mapping[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"proposal[{index}] must be a JSON object")
        proposals.append(item)
    return proposals


def _proposal_from_model_item(
    item: Mapping[str, Any], *, branch_id: str
) -> StructuredActionProposal:
    if "action" in item and "payload" in item:
        return structured_action_proposal_from_dict(_normalize_full_proposal(item, branch_id))

    kind = SearchActionKind(str(item.get("kind", PAYLOAD_KIND_IMPLEMENT)))
    rationale = _str_field(item, "rationale") or _default_rationale(kind)
    action = SearchAction(
        kind=kind,
        target_branch_id=_str_field(item, "target_branch_id") or branch_id,
        target_step_ids=tuple(_str_items(item.get("target_step_ids", ()))),
        allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[kind],
        rationale=rationale,
    )
    return StructuredActionProposal(
        action=action,
        payload=_payload_from_simple_item(kind, item),
        score=_score(item.get("score")),
        metadata=_metadata(item.get("metadata")),
    )


def _normalize_full_proposal(
    item: Mapping[str, Any], branch_id: str
) -> dict[str, Any]:
    action_data = dict(item["action"])
    kind = SearchActionKind(str(action_data["kind"]))
    action_data.setdefault("target_branch_id", branch_id)
    action_data.setdefault("allowed_mutations", [m.value for m in DEFAULT_ALLOWED_MUTATIONS[kind]])
    action_data.setdefault("rationale", _default_rationale(kind))

    payload_data = dict(item["payload"])
    payload_data.setdefault("kind", _payload_kind(kind))
    return {
        "action": action_data,
        "payload": payload_data,
        "score": item.get("score"),
        "metadata": _metadata(item.get("metadata")),
    }


def _payload_from_simple_item(
    kind: SearchActionKind, item: Mapping[str, Any]
):
    payload = item.get("payload")
    source = payload if isinstance(payload, Mapping) else item
    if kind in (SearchActionKind.IMPLEMENT, SearchActionKind.REPAIR_IMPLEMENTATION):
        return ImplementPayload(
            proof_text=_required_str(source, "proof_text"),
            source=_str_field(source, "source"),
        )
    if kind is SearchActionKind.RUN_CAPABILITY_TEST:
        return CapabilityTestPayload(
            requirement=_required_str(source, "requirement"),
            signature=_required_str(source, "signature"),
            expected_outcome=_str_field(source, "expected_outcome"),
        )
    if kind is SearchActionKind.DECOMPOSE:
        children = source.get("children")
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
            raise ValueError("decompose proposal requires a children array")
        return DecomposePayload(
            children=tuple(_child_spec(child, index) for index, child in enumerate(children)),
            strategy=_str_field(source, "strategy"),
        )
    if kind is SearchActionKind.PROPOSE_ARGUMENT:
        return ProposeArgumentPayload(
            steps=_argument_steps(source.get("steps", ())),
            alignments=_alignment_specs(source.get("alignments", ())),
            rationale=_str_field(source, "rationale"),
        )
    if kind is SearchActionKind.REFINE_ARGUMENT:
        return RefineArgumentPayload(
            steps=_argument_steps(source.get("steps", ())),
            alignments=_alignment_specs(source.get("alignments", ())),
            rationale=_str_field(source, "rationale"),
        )
    if kind is SearchActionKind.CHANGE_REPRESENTATION:
        return ChangeRepresentationPayload(
            argument=_argument_steps(source.get("argument", source.get("steps", ()))),
            alignments=_alignment_specs(source.get("alignments", ())),
            rationale=_str_field(source, "rationale"),
        )
    raise ValueError(f"unsupported structured proposal kind {kind.value!r}")


def _payload_kind(kind: SearchActionKind) -> str:
    if kind in (SearchActionKind.IMPLEMENT, SearchActionKind.REPAIR_IMPLEMENTATION):
        return PAYLOAD_KIND_IMPLEMENT
    if kind is SearchActionKind.RUN_CAPABILITY_TEST:
        return PAYLOAD_KIND_CAPABILITY_TEST
    if kind is SearchActionKind.DECOMPOSE:
        return PAYLOAD_KIND_DECOMPOSE
    if kind is SearchActionKind.PROPOSE_ARGUMENT:
        return PAYLOAD_KIND_PROPOSE_ARGUMENT
    if kind is SearchActionKind.REFINE_ARGUMENT:
        return PAYLOAD_KIND_REFINE_ARGUMENT
    if kind is SearchActionKind.CHANGE_REPRESENTATION:
        return PAYLOAD_KIND_CHANGE_REPRESENTATION
    raise ValueError(f"unsupported structured proposal kind {kind.value!r}")


def _child_spec(item: Any, index: int) -> DecomposeChildSpec:
    if not isinstance(item, Mapping):
        raise ValueError(f"children[{index}] must be a JSON object")
    return DecomposeChildSpec(
        child_id=_required_str(item, "child_id"),
        statement=_required_str(item, "statement"),
        dependency_ids=tuple(_str_items(item.get("dependency_ids", ()))),
    )


def _argument_steps(items: Any) -> tuple[ArgumentStepSpec, ...]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ValueError("argument steps must be an array")
    return tuple(_argument_step(item, index) for index, item in enumerate(items))


def _argument_step(item: Any, index: int) -> ArgumentStepSpec:
    if not isinstance(item, Mapping):
        raise ValueError(f"steps[{index}] must be a JSON object")
    confidence = item.get("confidence")
    return ArgumentStepSpec(
        step_id=_required_str(item, "step_id"),
        claim=_required_str(item, "claim"),
        justification=_str_field(item, "justification"),
        depends_on=tuple(_str_items(item.get("depends_on", ()))),
        introduced_fact_ids=tuple(_str_items(item.get("introduced_fact_ids", ()))),
        confidence=float(confidence) if isinstance(confidence, int | float) else None,
    )


def _alignment_specs(items: Any) -> tuple[AlignmentSpec, ...]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ValueError("alignments must be an array")
    return tuple(_alignment_spec(item, index) for index, item in enumerate(items))


def _alignment_spec(item: Any, index: int) -> AlignmentSpec:
    if not isinstance(item, Mapping):
        raise ValueError(f"alignments[{index}] must be a JSON object")
    span = item.get("source_span")
    return AlignmentSpec(
        argument_step_id=_required_str(item, "argument_step_id"),
        relation=_str_field(item, "relation") or "unaligned",
        lean_declaration_id=_optional_str(item.get("lean_declaration_id")),
        goal_fingerprint=_optional_str(item.get("goal_fingerprint")),
        source_span=tuple(span) if isinstance(span, Sequence) and len(span) == 2 else None,
    )


def _required_str(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"structured proposal requires non-empty {key!r}")
    return value.strip()


def _str_field(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    return value.strip() if isinstance(value, str) else ""


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _str_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _score(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _default_rationale(kind: SearchActionKind) -> str:
    return f"structured {kind.value}"
