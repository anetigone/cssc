"""Configuration objects for named agent roles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AgentRole(str, Enum):
    """Named roles in the proof-agent pipeline."""

    FORMALIZER = "formalizer"
    PROOF_GENERATOR = "proof_generator"


@dataclass(frozen=True)
class RoleModelConfig:
    """Model and sampling knobs for one agent role."""

    role: AgentRole
    model: str
    temperature: float = 0.2
    max_tokens: int = 1024
