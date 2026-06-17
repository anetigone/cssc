"""Namespace-free input normalization and validation for proof tasks."""

from .normalizer import InputNormalizer, NormalizedInput, prepare_tasks
from .parsing import (
    INFORMAL_PROOF_KEYS,
    LEAN_SOURCE_KEYS,
    PROBLEM_KEYS,
    config_imports,
    config_informal_proof,
    config_lean_source,
    config_problem,
    config_value,
    copy_text_field,
    has_inline_source,
    has_nl_source,
    iter_task_config_entries,
)
from .validation import (
    LeanAdapterScaffoldChecker,
    ScaffoldValidationError,
    ScaffoldValidationResult,
    ValidationConfig,
    validate_scaffold_json,
)

__all__ = [
    "InputNormalizer",
    "LeanAdapterScaffoldChecker",
    "NormalizedInput",
    "PROBLEM_KEYS",
    "INFORMAL_PROOF_KEYS",
    "LEAN_SOURCE_KEYS",
    "ScaffoldValidationError",
    "ScaffoldValidationResult",
    "ValidationConfig",
    "config_imports",
    "config_informal_proof",
    "config_lean_source",
    "config_problem",
    "config_value",
    "copy_text_field",
    "has_inline_source",
    "has_nl_source",
    "iter_task_config_entries",
    "prepare_tasks",
    "validate_scaffold_json",
]
