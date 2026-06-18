"""Action generator and retriever construction for the CLI."""

from __future__ import annotations

import logging
from argparse import Namespace
from pathlib import Path

from agent import (
    LexicalLeanRetriever,
    OpenAIChatActionGenerator,
    OpenAIChatConfig,
    StaticActionGenerator,
    TaskInputKind,
    load_dotenv,
)
from agent.agents import (
    LeanEnvironmentToolProvider,
    OpenAIChatFormalizationAgent,
    ScaffoldChecker,
    VerifiedFormalizationCache,
)

from .paths import resolve_agent_path
from .tasks import classify_input, require_source


logger = logging.getLogger(__name__)


def build_action_generator(args: Namespace, *, project_root: Path | None = None):
    candidates = list(args.candidate)
    for path in args.candidate_file:
        candidates.append(Path(path).read_text(encoding="utf-8"))

    if candidates:
        if args.use_model:
            logger.warning(
                "Static candidates take precedence; --use-model is ignored. "
                "Drop --candidate/--candidate-file to use the model."
            )
        logger.debug("Using %d static candidate(s)", len(candidates))
        return StaticActionGenerator(candidates)
    if args.use_model:
        _ensure_env_loaded(args)
        tools = None
        if project_root is not None:
            tools = LeanEnvironmentToolProvider(
                project_root=project_root,
                lake_executable=getattr(args, "lake_executable", None),
                lean_executable=getattr(args, "lean_executable", None),
            ).tools()
        return OpenAIChatActionGenerator(_model_config(args), tools=tools)
    raise ValueError("Provide --candidate, --candidate-file, or --use-model.")


def build_formalization_agent(
    args: Namespace,
    *,
    checker: ScaffoldChecker | None = None,
    project_root: Path | None = None,
) -> OpenAIChatFormalizationAgent | None:
    if not _needs_formalizer(args):
        return None
    if not args.use_model:
        raise ValueError("Natural-language tasks require --use-model so the formalizer can create Lean.")
    _ensure_env_loaded(args)
    cache = None
    if args.formalization_cache_dir:
        cache = VerifiedFormalizationCache(
            resolve_agent_path(Path(args.agent_root), args.formalization_cache_dir)
        )
    tools = None
    if project_root is not None:
        tools = LeanEnvironmentToolProvider(
            project_root=project_root,
            lake_executable=getattr(args, "lake_executable", None),
            lean_executable=getattr(args, "lean_executable", None),
        ).tools()
    return OpenAIChatFormalizationAgent(
        _model_config(args),
        checker=checker,
        cache=cache,
        tools=tools,
    )


def _ensure_env_loaded(args: Namespace) -> None:
    env_path = Path(args.env_file)
    if env_path.exists():
        logger.debug("Loading environment file: %s", env_path)
        load_dotenv(env_path, override=False)
    else:
        logger.debug("Environment file does not exist: %s", env_path)


def _model_config(args: Namespace) -> OpenAIChatConfig:
    return OpenAIChatConfig.from_env(
        timeout_seconds=args.model_timeout,
        max_tokens=args.model_max_tokens,
    )


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
