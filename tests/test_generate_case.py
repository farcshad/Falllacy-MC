from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_creation.config import PipelineConfig
from data_creation.generate_case import (
    CASE_FIELDS,
    build_generation_plan,
    case_fingerprint,
    load_usable_schemas,
    run_stage_3,
    validate_case_response,
)
from data_creation.llm_provider import LLMResponse


SCHEMA = {
    "premise_pattern": "One observed C has P.",
    "invalid_inference": "The observation is treated as representative of C.",
    "conclusion_pattern": "All C is concluded to have P.",
}


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)

    def generate_json(self, **kwargs) -> LLMResponse:
        return LLMResponse(
            content=next(self.responses),
            response_id="case-id",
            model=kwargs["model"],
            provider="fake-generator",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        )


def adjudicated_record(decision: str, source_id: str) -> dict:
    return {
        "source_id": source_id,
        "canonical_fallacy": "Hasty generalization",
        "original_fallacy": "hasty generalization",
        "adjudication": {"decision": decision},
        "final_schema": None if decision == "REJECT" else SCHEMA,
    }


def generated_case(index: int) -> dict[str, str]:
    return {
        "situation": f"Situation {index} presents a small observation.",
        "question": f"What conclusion follows in case {index}?",
        "student_reasoning": f"Observed case {index} has P, so all cases have P.",
        "student_answer": f"All cases have property P in example {index}.",
        "misconception": f"A single observation always represents an entire category {index}.",
    }


class GenerateCaseTests(unittest.TestCase):
    def test_filters_rejected_and_builds_expected_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "input.json"
            path.write_text(
                json.dumps(
                    [
                        adjudicated_record("ACCEPT", "source-1"),
                        adjudicated_record("REVISE", "source-2"),
                        adjudicated_record("REJECT", "source-3"),
                    ]
                ),
                encoding="utf-8",
            )
            schemas, rejected = load_usable_schemas(path)
            plan = build_generation_plan(schemas, PipelineConfig())
            self.assertEqual(len(schemas), 2)
            self.assertEqual(rejected, 1)
            self.assertEqual(len(plan), 8)

    def test_case_validation_and_fingerprint(self) -> None:
        case = generated_case(1)
        parsed = validate_case_response(json.dumps(case))
        self.assertEqual(set(parsed), set(CASE_FIELDS))
        self.assertEqual(case_fingerprint(case), case_fingerprint(parsed))
        with self.assertRaises(ValueError):
            validate_case_response(json.dumps({**case, "misconception": ""}))

    def test_run_retries_duplicate_and_emits_four_unique_cases(self) -> None:
        cases = [generated_case(index) for index in range(1, 5)]
        responses = [cases[0], cases[1], cases[0], cases[2], cases[3]]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "adjudicated.json"
            input_path.write_text(
                json.dumps([adjudicated_record("ACCEPT", "source-1")]), encoding="utf-8"
            )
            config = PipelineConfig(
                outputs_dir=root / "outputs",
                stage_3_prompt_path=Path(__file__).parents[1]
                / "data_creation"
                / "prompts"
                / "case_generation_prompt.txt",
            )
            run_dir = run_stage_3(
                config,
                input_path=input_path,
                provider=FakeProvider([json.dumps(response) for response in responses]),
                run_id="generation-test",
            )
            output = json.loads((run_dir / "generated_cases.json").read_text())
            manifest = json.loads((run_dir / "stage_3_manifest.json").read_text())
            self.assertEqual(len(output), 4)
            self.assertEqual(
                len({case_fingerprint({field: case[field] for field in CASE_FIELDS}) for case in output}),
                4,
            )
            self.assertEqual(manifest["retry_count"], 1)
            self.assertEqual(manifest["duplicate_attempt_count"], 1)
            self.assertEqual(manifest["final_exact_duplicate_count"], 0)


if __name__ == "__main__":
    unittest.main()

