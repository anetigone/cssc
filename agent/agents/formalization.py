"""Agent roles that formalize prose tasks into Lean scaffolds."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..input.validation import (
    ScaffoldValidationError,
    ScaffoldValidationResult,
    ValidationConfig,
    validate_scaffold_json,
)
from .openai import (
    ChatTransport,
    ModelAdapterError,
    OpenAIChatConfig,
    UrllibChatTransport,
    chat_completions_url,
    first_choice_content,
    parse_json_object,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FormalizationRequest:
    """Input for a natural-language to Lean formalization step."""

    problem: str
    task_id: str = "natural_language_task"
    imports: tuple[str, ...] = ()
    informal_proof: str | None = None
    context: str | None = None
    hole_marker: str = "{{proof}}"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FormalizationResult:
    """Lean scaffold generated from a natural-language problem."""

    proof_source: str
    natural_language_proof: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class FormalizationAgent(Protocol):
    """Boundary for agents that produce checker-ready Lean scaffolds."""

    def formalize(self, request: FormalizationRequest) -> FormalizationResult:
        """Return Lean source containing exactly one proof hole."""
        ...


class ScaffoldChecker(Protocol):
    """Boundary for validators that check a formalized Lean scaffold compiles."""

    def validate_scaffold(
        self,
        source: str,
        *,
        imports: tuple[str, ...],
        hole_marker: str = "{{proof}}",
        inactive_fill: str = "sorry",
    ) -> ScaffoldValidationResult:
        """Return whether ``source`` compiles after filling its active hole."""
        ...


class StaticFormalizationAgent:
    """Deterministic formalizer useful for tests and curated datasets."""

    def __init__(self, result: FormalizationResult | str) -> None:
        self.result = (
            result if isinstance(result, FormalizationResult) else FormalizationResult(result)
        )
        self.requests: list[FormalizationRequest] = []

    def formalize(self, request: FormalizationRequest) -> FormalizationResult:
        self.requests.append(request)
        return self.result


class OpenAIChatFormalizationAgent:
    """Generate a Lean scaffold from prose through an OpenAI-compatible endpoint.

    When a ``checker`` is configured, the agent validates the scaffold with Lean
    and retries a bounded number of times if the scaffold does not compile.
    """

    def __init__(
        self,
        config: OpenAIChatConfig,
        *,
        transport: ChatTransport | None = None,
        checker: ScaffoldChecker | None = None,
        validation: ValidationConfig | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibChatTransport()
        self.checker = checker
        self.validation = validation or ValidationConfig()

    def formalize(self, request: FormalizationRequest) -> FormalizationResult:
        messages = _build_messages(request)
        max_attempts = 1 + self.validation.max_retries
        last_validation_message = ""

        for attempt in range(max_attempts):
            if attempt > 0:
                messages = list(messages)
                messages.append(
                    {
                        "role": "user",
                        "content": _build_retry_prompt(last_validation_message),
                    }
                )

            payload = {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                **self.config.extra_body,
            }
            response = self.transport.post_json(
                chat_completions_url(self.config.base_url),
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout_seconds=self.config.timeout_seconds,
            )
            data = parse_json_object(
                first_choice_content(response),
                context="Formalizer response",
            )
            proof_source, natural_language_proof = validate_scaffold_json(data)

            if self.checker is None:
                logger.info(
                    "Generated formalization: task_id=%s model=%s",
                    request.task_id,
                    self.config.model,
                )
                return FormalizationResult(
                    proof_source=proof_source,
                    natural_language_proof=natural_language_proof,
                    metadata={"model": self.config.model},
                )

            validation_result = self.checker.validate_scaffold(
                proof_source,
                imports=request.imports,
                hole_marker=request.hole_marker,
                inactive_fill=self.validation.inactive_fill,
            )
            if validation_result.ok:
                logger.info(
                    "Generated and validated formalization: task_id=%s model=%s",
                    request.task_id,
                    self.config.model,
                )
                return FormalizationResult(
                    proof_source=proof_source,
                    natural_language_proof=natural_language_proof,
                    metadata={"model": self.config.model},
                )

            last_validation_message = validation_result.message
            logger.warning(
                "Formalized scaffold failed validation (attempt %d/%d): %s",
                attempt + 1,
                max_attempts,
                last_validation_message[:200],
            )

        raise ModelAdapterError(
            f"Formalizer scaffold failed Lean validation after {max_attempts} attempt(s)."
        )


def _build_messages(request: FormalizationRequest) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You convert natural-language mathematics tasks into Lean 4 proof-completion "
                "scaffolds. Return only JSON with keys proof_source and optional "
                "natural_language_proof. The Lean source must contain exactly one proof hole "
                f"marker {request.hole_marker!r} or one standalone sorry."
            ),
        },
        {"role": "user", "content": _build_user_prompt(request)},
    ]


def _build_user_prompt(request: FormalizationRequest) -> str:
    parts = [f"Task id: {request.task_id}", "Problem:", request.problem]
    if request.imports:
        parts.append("Preferred imports: " + ", ".join(request.imports))
    if request.informal_proof:
        parts.extend(["Informal proof sketch:", request.informal_proof])
    if request.context:
        parts.extend(["Additional context:", request.context])
    parts.extend(
        [
            "Return JSON only, for example:",
            json.dumps(
                {
                    "proof_source": f"theorem example_name : True := by\\n  {request.hole_marker}",
                    "natural_language_proof": "A concise proof in ordinary language.",
                },
                ensure_ascii=False,
            ),
        ]
    )
    return "\n".join(parts)


def _build_retry_prompt(validation_message: str) -> str:
    return (
        "The previous scaffold failed to compile or validate. "
        "Please fix the Lean source and return corrected JSON.\n\n"
        f"Validation feedback:\n{validation_message}"
    )
