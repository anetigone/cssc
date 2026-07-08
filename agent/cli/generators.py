"""Action generator and retriever construction for the CLI."""

from __future__ import annotations

import logging
from argparse import Namespace
from pathlib import Path

from agent import (
    ChatActionGenerator,
    ChatConfig,
    ChatContextSummarizer,
    ChatFormalizationAgent,
    LexicalLeanRetriever,
    StaticActionGenerator,
    TaskInputKind,
    load_dotenv,
)
from agent.agents import (
    LeanEnvironmentToolProvider,
    LeanProofToolProvider,
    ScaffoldChecker,
    VerifiedFormalizationCache,
)

from .paths import resolve_agent_path
from .tasks import classify_input, require_source


logger = logging.getLogger(__name__)


def build_context_summarizer(args: Namespace) -> ChatContextSummarizer | None:
    if not getattr(args, "context_summarizer", False):
        return None
    if args.use_model is False:
        raise ValueError(
            "Context summarizer requires a model. Remove --no-model or disable the summarizer."
        )
    _ensure_env_loaded(args)
    return ChatContextSummarizer(_model_config(args, role="context"))


def build_action_generator(args: Namespace, *, project_root: Path | None = None):
    candidates = list(args.candidate)
    for path in args.candidate_file:
        candidates.append(Path(path).read_text(encoding="utf-8"))

    if candidates:
        if args.use_model is True:
            logger.warning(
                "Static candidates take precedence; --use-model is ignored. "
                "Drop --candidate/--candidate-file to use the model."
            )
        logger.debug("Using %d static candidate(s)", len(candidates))
        return StaticActionGenerator(candidates)
    if args.use_model is not False:
        _ensure_env_loaded(args)
        if getattr(args, "execution_mode", "minimal") == "structured":
            from agent.agents.structured import ChatStructuredActionGenerator

            return ChatStructuredActionGenerator(
                _model_config(args, role="proof"),
                tools=_proof_tools(args, project_root),
                max_tool_rounds=1,
            )
        return ChatActionGenerator(
            _model_config(args, role="proof"),
            tools=_proof_tools(args, project_root),
            max_tool_rounds=1,
        )
    raise ValueError(
        "Model calls are disabled. Provide --candidate/--candidate-file or remove --no-model."
    )


def build_formalization_agent(
    args: Namespace,
    *,
    checker: ScaffoldChecker | None = None,
    project_root: Path | None = None,
) -> ChatFormalizationAgent | None:
    if not _needs_formalizer(args):
        return None
    if args.use_model is False:
        raise ValueError(
            "Natural-language formalization requires a model. Remove --no-model or provide Lean input."
        )
    _ensure_env_loaded(args)
    cache = None
    if args.formalization_cache_dir:
        cache = VerifiedFormalizationCache(
            resolve_agent_path(Path(args.agent_root), args.formalization_cache_dir)
        )
    return ChatFormalizationAgent(
        _model_config(args, role="formalizer"),
        checker=checker,
        cache=cache,
        tools=_lean_tools(args, project_root),
    )


def _ensure_env_loaded(args: Namespace) -> None:
    env_path = Path(args.env_file)
    if env_path.exists():
        logger.debug("Loading environment file: %s", env_path)
        load_dotenv(env_path, override=False)
    else:
        logger.debug("Environment file does not exist: %s", env_path)


def _model_config(args: Namespace, *, role: str = "proof") -> ChatConfig:
    """Build a ChatConfig, honouring per-role overrides when they are set.

    ``role`` selects the CLI override prefix (``--<role>-model`` etc.). The
    ``getattr(..., None)`` defaults matter because callers (notably tests) build
    a Namespace that does not carry the per-role destinations; absence means
    "fall back to the global default / environment".
    """
    model = getattr(args, f"{role}_model", None) or getattr(args, "model", None)
    temperature = getattr(args, f"{role}_temperature", None)
    if temperature is None:
        temperature = getattr(args, "temperature", None)
    max_tokens = (
        getattr(args, f"{role}_max_tokens", None)
        or getattr(args, "max_tokens", None)
        or getattr(args, "model_max_tokens", None)
        or 16384
    )
    return ChatConfig.from_env(
        timeout_seconds=getattr(args, "model_timeout", 60.0),
        max_tokens=max_tokens,
        model=model,
        temperature=temperature,
    )


def _lean_tools(args: Namespace, project_root: Path | None):
    if project_root is None:
        return None
    return LeanEnvironmentToolProvider(
        project_root=project_root,
        lake_executable=getattr(args, "lake_executable", None),
        lean_executable=getattr(args, "lean_executable", None),
    ).tools()


def _proof_tools(args: Namespace, project_root: Path | None):
    if project_root is None:
        return None
    return LeanProofToolProvider(
        project_root=project_root,
        lake_executable=getattr(args, "lake_executable", None),
        lean_executable=getattr(args, "lean_executable", None),
        timeout_seconds=min(float(getattr(args, "lean_timeout", 60.0)), 60.0),
    ).tools()


def _needs_formalizer(args: Namespace) -> bool:
    return classify_input(args) == TaskInputKind.NATURAL_LANGUAGE


def build_retriever(args: Namespace) -> LexicalLeanRetriever | None:
    if not args.enable_retrieval and not args.retrieval_source:
        return None
    agent_root = Path(args.agent_root)
    sources = args.retrieval_source or [require_source(args)]
    sources = [resolve_agent_path(agent_root, source) for source in sources]
    logger.debug("Building lexical retriever from %d source path(s)", len(sources))
    return LexicalLeanRetriever.from_paths(sources)
