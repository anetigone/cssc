"""Agent roles that formalize prose tasks into Lean scaffolds."""

from __future__ import annotations

import json
import logging
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
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
PROMPT_VERSION = "formalization-v5-validated-scaffold"

# Bump when scaffold validation semantics change. This is folded into the cache
# key so entries validated under older assumptions are not silently reused.
CACHE_VERSION = "validation-v1"


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
        cache: "VerifiedFormalizationCache | None" = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibChatTransport()
        self.checker = checker
        self.validation = validation or ValidationConfig()
        self.cache = cache

    def formalize(self, request: FormalizationRequest) -> FormalizationResult:
        if self.cache is not None and self.checker is not None:
            cached = self.cache.get(request, model=self.config.model)
            if cached is not None:
                logger.info(
                    "Using cached validated formalization: task_id=%s model=%s",
                    request.task_id,
                    self.config.model,
                )
                return cached

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
                result = FormalizationResult(
                    proof_source=proof_source,
                    natural_language_proof=natural_language_proof,
                    metadata={"model": self.config.model},
                )
                if self.cache is not None:
                    self.cache.put(request, result, model=self.config.model)
                return result

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


class VerifiedFormalizationCache:
    """Disk cache for scaffolds that already passed Lean validation."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, request: FormalizationRequest, *, model: str) -> FormalizationResult | None:
        path = self._path_for(request, model=model)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable formalization cache entry: %s", path, exc_info=True)
            return None
        if data.get("cache_key") != self._key_for(request, model=model):
            return None
        if not data.get("validated"):
            return None
        proof_source = data.get("proof_source")
        if not isinstance(proof_source, str) or not proof_source.strip():
            return None
        natural_language_proof = data.get("natural_language_proof")
        if natural_language_proof is not None and not isinstance(natural_language_proof, str):
            natural_language_proof = None
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata = {**metadata, "formalization_cache_hit": True}
        return FormalizationResult(
            proof_source=proof_source,
            natural_language_proof=natural_language_proof,
            metadata=metadata,
        )

    def put(self, request: FormalizationRequest, result: FormalizationResult, *, model: str) -> None:
        key = self._key_for(request, model=model)
        path = self._path_for(request, model=model)
        payload = {
            "cache_key": key,
            "prompt_version": PROMPT_VERSION,
            "cache_version": CACHE_VERSION,
            "validated": True,
            "model": model,
            "proof_source": result.proof_source,
            "natural_language_proof": result.natural_language_proof,
            "metadata": result.metadata,
        }
        tmp_path = path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except OSError:
            logger.warning("Failed to write formalization cache entry: %s", path, exc_info=True)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _path_for(self, request: FormalizationRequest, *, model: str) -> Path:
        return self.root / f"{self._key_for(request, model=model)}.json"

    @staticmethod
    def _key_for(request: FormalizationRequest, *, model: str) -> str:
        payload = {
            "prompt_version": PROMPT_VERSION,
            "cache_version": CACHE_VERSION,
            "model": model,
            "problem": request.problem,
            "imports": request.imports,
            "informal_proof": request.informal_proof,
            "context": request.context,
            "hole_marker": request.hole_marker,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_messages(request: FormalizationRequest) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You convert natural-language mathematics tasks into Lean 4 proof-completion "
                "scaffolds. Return only JSON with keys proof_source and optional "
                "natural_language_proof. The Lean source must contain exactly one proof hole "
                f"marker {request.hole_marker!r} or one standalone sorry. Use the smallest "
                "Lean imports that the statement needs. Do not use `import Mathlib` unless "
                "no narrower Mathlib module is reasonable. If the task is in core Lean, use "
                "no import. The scaffold must be self-contained: include every namespace "
                "opening or use fully qualified names for symbols such as Filter.Tendsto, "
                "Filter.atTop, Set.Nonempty, sSup, and neighborhood notation. If preferred "
                "imports are provided by the user prompt, use those imports unless Lean "
                "validation feedback shows they are insufficient."
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
