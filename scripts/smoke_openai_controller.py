"""Smoke run for the OpenAI-compatible model adapter and proof controller.

Examples:
    python scripts/smoke_openai_controller.py --mock-model
    python scripts/smoke_openai_controller.py

The real run reads OPENAI_API_KEY, OPENAI_BASE_URL, and OPENAI_MODEL.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import (  # noqa: E402
    BudgetConfig,
    ControllerConfig,
    LeanAdapter,
    OpenAIChatActionGenerator,
    OpenAIChatConfig,
    ProofController,
    ProofTask,
)
from agent.env_loader import load_dotenv  # noqa: E402
from agent.model_adapter import ChatTransport, ModelAdapterError  # noqa: E402
from agent.workspace import AttemptWorkspace  # noqa: E402


class MockOpenAITransport(ChatTransport):
    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        prompt = payload["messages"][1]["content"]
        content = "exact And.intro h.right h.left" if "smoke_and_comm" in prompt else "trivial"
        return {
            "id": "mock-smoke",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-model", action="store_true")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--task", choices=["true", "and_comm"], default="and_comm")
    parser.add_argument("--max-checks", type=int, default=1)
    parser.add_argument("--max-model-calls", type=int, default=1)
    parser.add_argument("--lean-timeout", type=float, default=10.0)
    args = parser.parse_args()

    load_dotenv(args.env_file, override=False)

    task = _task_by_name(args.task)

    try:
        if args.mock_model:
            config = OpenAIChatConfig(
                api_key="mock-key",
                model="mock-openai-compatible-model",
                base_url="https://mock.local/v1",
                timeout_seconds=5.0,
            )
            generator = OpenAIChatActionGenerator(config, transport=MockOpenAITransport())
        else:
            generator = OpenAIChatActionGenerator(OpenAIChatConfig.from_env())
    except ModelAdapterError as exc:
        print(json.dumps({"ok": False, "stage": "model_config", "error": str(exc)}, indent=2))
        return 2

    with tempfile.TemporaryDirectory(dir=args.work_dir) as tmp:
        controller = ProofController(
            adapter=LeanAdapter(prefer_lake=False),
            action_generator=generator,
            workspace=AttemptWorkspace(tmp),
            budget_config=BudgetConfig(
                max_checks=args.max_checks,
                max_model_calls=args.max_model_calls,
                per_check_timeout_seconds=args.lean_timeout,
            ),
            config=ControllerConfig(max_candidates_per_model_call=1),
        )
        result = controller.run(task)

    payload = {
        "ok": result.accepted,
        "stop_reason": result.stop_reason,
        "attempts": len(result.attempts),
        "checks_used": result.budget.checks_used,
        "model_calls_used": result.budget.model_calls_used,
    }
    if result.attempts:
        last = result.attempts[-1].check_result
        payload["last_category"] = last.category.value
        payload["last_message"] = last.parsed_feedback.message if last.parsed_feedback else ""
    print(json.dumps(payload, indent=2))
    return 0 if result.accepted else 1


def _task_by_name(name: str) -> ProofTask:
    if name == "true":
        return ProofTask(
            task_id="smoke_true",
            source_template="theorem smoke_true : True := by\n  {{proof}}\n",
        )
    if name == "and_comm":
        return ProofTask(
            task_id="smoke_and_comm",
            source_template=(
                "theorem smoke_and_comm (p q : Prop) : p ∧ q -> q ∧ p := by\n"
                "  intro h\n"
                "  {{proof}}\n"
            ),
        )
    raise ValueError(f"Unknown smoke task: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
