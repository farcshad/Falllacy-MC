from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_creation.config import PipelineConfig
from data_creation.generate_context_case import (
    CONTEXT_CASE_FIELDS,
    build_context_generation_plan,
    context_case_fingerprint,
    run_context_stage_3,
    validate_context_case_response,
)
from data_creation.llm_provider import LLMResponse


SCHEMA = {
    "premise_pattern": "A small sample of C has P.",
    "invalid_inference": "The sample is treated as representative of C.",
    "conclusion_pattern": "All C is concluded to have P.",
}


def adjudicated_schema() -> dict:
    return {
        "source_id": "source-1",
        "canonical_fallacy": "Hasty generalization",
        "original_fallacy": "hasty generalization",
        "adjudication": {"decision": "ACCEPT"},
        "final_schema": SCHEMA,
    }


def generated_case(index: int) -> dict[str, str]:
    return {
        "context": f"Two bean plants in group {index} grew faster under red light.",
        "question": f"What might observation {index} suggest about plant responses to light?",
        "possible_fallacy": "Hasty generalization",
        "possible_reasoning": (
            f"Both plants in group {index} grew faster, so red light must work best for every plant."
        ),
        "possible_misconception": (
            f"All plant species share red light as their optimal growing condition {index}."
        ),
    }


class FakeProvider:
    def __init__(self, responses: list[dict[str, str]]) -> None:
        self.responses = iter(responses)

    def generate_json(self, **kwargs) -> LLMResponse:
        return LLMResponse(
            content=json.dumps(next(self.responses)),
            response_id="context-id",
            model=kwargs["model"],
            provider="fake-generator",
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )


class GenerateContextCaseTests(unittest.TestCase):
    def test_plan_has_two_domains_and_two_cases_per_domain(self) -> None:
        config = PipelineConfig()
        plan = build_context_generation_plan(
            [
                {
                    "source_id": "source-1",
                    "canonical_fallacy": "Hasty generalization",
                    "original_fallacy": "hasty generalization",
                    "adjudication_decision": "ACCEPT",
                    "final_schema": SCHEMA,
                }
            ],
            config,
        )
        self.assertEqual(len(plan), 4)
        self.assertEqual(len({item["domain"] for item in plan}), 2)

    def test_validation_enforces_seed_label_and_neutral_input(self) -> None:
        valid = generated_case(1)
        parsed = validate_context_case_response(json.dumps(valid), "Hasty generalization")
        self.assertEqual(set(parsed), set(CONTEXT_CASE_FIELDS))
        self.assertEqual(context_case_fingerprint(valid), context_case_fingerprint(parsed))

        wrong_label = {**valid, "possible_fallacy": "False dilemma"}
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(wrong_label), "Hasty generalization")
        named_fallacy = {**valid, "question": "Is this a hasty generalization?"}
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(named_fallacy), "Hasty generalization")
        reasoning_trace = {
            **valid,
            "context": "A student concludes that every plant grows best under red light.",
        }
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(reasoning_trace), "Hasty generalization")
        quoted_trace = {
            **valid,
            "context": "Some residents argued that red light must be best for every plant.",
        }
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(quoted_trace), "Hasty generalization")
        evaluator_reasoning = {
            **valid,
            "possible_reasoning": (
                "Red light must be best. The reasoning ignores other possible causes."
            ),
        }
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(evaluator_reasoning), "Hasty generalization")
        circular_leak = {
            **valid,
            "context": "The teacher says to follow it because it is the school rule.",
        }
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(circular_leak), "Hasty generalization")
        named_in_reasoning = {
            **valid,
            "possible_reasoning": "This conclusion is a hasty generalization.",
        }
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(named_in_reasoning), "Hasty generalization")
        implied_answer = {
            **valid,
            "context": "The owner expanded the product, believing it caused the sales increase.",
        }
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(implied_answer), "Hasty generalization")
        causal_giveaway = {
            **valid,
            "context": "Officials attributed the growth to red light.",
        }
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(causal_giveaway), "Hasty generalization")
        self_critique = {
            **valid,
            "possible_reasoning": "Red light caused growth. However, other factors could matter.",
        }
        with self.assertRaises(ValueError):
            validate_context_case_response(json.dumps(self_critique), "Hasty generalization")

    def test_run_retries_duplicate_and_writes_context_first_manifest(self) -> None:
        cases = [generated_case(index) for index in range(1, 5)]
        responses = [cases[0], cases[1], cases[0], cases[2], cases[3]]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "adjudicated.json"
            input_path.write_text(json.dumps([adjudicated_schema()]), encoding="utf-8")
            config = PipelineConfig(
                outputs_dir=root / "outputs",
                stage_3_context_prompt_path=Path(__file__).parents[1]
                / "data_creation"
                / "prompts"
                / "context_case_generation_prompt.txt",
            )
            run_dir = run_context_stage_3(
                config,
                input_path=input_path,
                provider=FakeProvider(responses),
                run_id="context-test",
            )
            output = json.loads((run_dir / "generated_cases.json").read_text())
            manifest = json.loads((run_dir / "stage_3_manifest.json").read_text())
            self.assertEqual(len(output), 4)
            self.assertEqual(manifest["stage_name"], "context_first_misconception_generation")
            self.assertEqual(manifest["retry_count"], 1)
            self.assertEqual(manifest["duplicate_attempt_count"], 1)
            self.assertEqual(manifest["final_exact_duplicate_count"], 0)


if __name__ == "__main__":
    unittest.main()
