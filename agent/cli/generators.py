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
    load_dotenv,
)

from .paths import resolve_agent_path
from .tasks import _require_source


logger = logging.getLogger(__name__)


def build_action_generator(args: Namespace):
    candidates = list(args.candidate)
    for path in args.candidate_file:
        candidates.append(Path(path).read_text(encoding="utf-8"))

    if candidates and args.use_model:
        raise ValueError("Use either static candidates or --use-model, not both.")
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


def build_retriever(args: Namespace) -> LexicalLeanRetriever | None:
    if not args.enable_retrieval and not args.retrieval_source:
        return None
    agent_root = Path(args.agent_root)
    sources = args.retrieval_source or [_require_source(args)]
    sources = [resolve_agent_path(agent_root, source) for source in sources]
    logger.debug("Building lexical retriever from %d source path(s)", len(sources))
    return LexicalLeanRetriever.from_paths(sources)
