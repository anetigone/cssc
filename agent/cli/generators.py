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
from agent.agents import OpenAIChatFormalizationAgent, ScaffoldChecker, VerifiedFormalizationCache

from .paths import resolve_agent_path
from .tasks import classify_input, require_source


logger = logging.getLogger(__name__)


def build_action_generator(args: Namespace):
    candidates = list(args.candidate)
    for path in args.candidate_file:
        candidates.append(Path(path).read_text(encoding="utf-8"))

    if candidates:
        logger.debug("Using %d static candidate(s)", len(candidates))
        return StaticActionGenerator(candidates)
    if args.use_model:
        env_path = Path(args.env_file)
        if env_path.exists():
            logger.debug("Loading environment file: %s", env_path)
            load_dotenv(env_path, override=False)
        else:
            logger.debug("Environment file does not exist: %s", env_path)
        return OpenAIChatActionGenerator(OpenAIChatConfig.from_env())
    raise ValueError("Provide --candidate, --candidate-file, or --use-model.")


def build_formalization_agent(
    args: Namespace,
    *,
    checker: ScaffoldChecker | None = None,
) -> OpenAIChatFormalizationAgent | None:
    if not _needs_formalizer(args):
        return None
    if not args.use_model:
        raise ValueError("Natural-language tasks require --use-model so the formalizer can create Lean.")
    env_path = Path(args.env_file)
    if env_path.exists():
        logger.debug("Loading environment file for formalizer: %s", env_path)
        load_dotenv(env_path, override=False)
    else:
        logger.debug("Environment file does not exist for formalizer: %s", env_path)
    cache = None
    if args.formalization_cache_dir:
        cache = VerifiedFormalizationCache(
            resolve_agent_path(Path(args.agent_root), args.formalization_cache_dir)
        )
    return OpenAIChatFormalizationAgent(
        OpenAIChatConfig.from_env(),
        checker=checker,
        cache=cache,
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
