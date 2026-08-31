from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_creation.config import PipelineConfig
from data_creation.llm_provider import LLMResponse
from data_creation.validate_case import (
    CHECK_FIELDS,
    load_generated_cases,
    misconception_is_declarative,
    run_stage_3_5,
    validate_validation_response,
)


SCHEMA = {
    "premise_pattern": "One observed member of C has P.",
    "invalid_inference": "The observation is treated as representative of C.",
    "conclusion_pattern": "All members of C are concluded to have P.",
}


def case(case_id: str) -> dict:
    return {
        "id": case_id,
        "source_schema_id": "source-1",
        "source_schema_decision": "ACCEPT",
        "domain": "biology",
        "fallacy": "Hasty generalization",
        "original_fallacy": "hasty generalization",
        "fallacy_schema": SCHEMA,
        "situation": "One bean plant grew quickly in blue light.",
        "question": "How do bean plants grow under blue light?",
        "student_reasoning": "This plant grew quickly, so every bean plant will grow quickly.",
        "student_answer": "All bean plants grow quickly in blue light.",
        "misconception": "One specimen reliably represents every member of its species.",
        "generation_metadata": {"model": "generator"},
    }


def outcome(decision: str) -> dict:
    checks = {field: True for field in CHECK_FIELDS}
    if decision != "ACCEPT":
        checks["misconception_valid"] = False
    revised = None
    issues: list[str] = []
    if decision == "REVISE":
        revised = {
            field: case("unused")[field]
            for field in (
                "situation",
                "question",
                "student_reasoning",
                "student_answer",
                "misconception",
            )
        }
        revised["misconception"] = "A single specimen always represents its entire species."
        issues = ["The misconception needed a more direct declarative formulation."]
    elif decision == "REJECT":
        issues = ["The case cannot be repaired while preserving the schema."]
    return {
        "decision": decision,
        "checks": checks,
        "issues": issues,
        "revised_case": revised,
    }


class FakeProvider:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = iter(responses)

    def generate_json(self, **kwargs) -> LLMResponse:
        return LLMResponse(
            content=json.dumps(next(self.responses)),
            response_id="validation-id",
            model=kwargs["model"],
            provider="fake-validator",
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )


class ValidateCaseTests(unittest.TestCase):
    def test_decision_invariants_and_misconception_style(self) -> None:
        current = {field: case("case_1")[field] for field in (
            "situation", "question", "student_reasoning", "student_answer", "misconception"
        )}
        accepted = validate_validation_response(
            json.dumps(outcome("ACCEPT")), current, "Hasty generalization"
        )
        self.assertEqual(accepted["decision"], "ACCEPT")
        self.assertTrue(misconception_is_declarative(current["misconception"], "Hasty generalization"))
        self.assertFalse(
            misconception_is_declarative(
                "Students may believe that one example proves a rule.", "Hasty generalization"
            )
        )
        self.assertFalse(misconception_is_declarative("Hasty generalization", "Hasty generalization"))
        invalid = outcome("ACCEPT")
        invalid["checks"]["schema_faithful"] = False
        with self.assertRaises(ValueError):
            validate_validation_response(json.dumps(invalid), current, "Hasty generalization")

    def test_load_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "cases.json"
            path.write_text(json.dumps([case("case_1"), case("case_1")]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_generated_cases(path)

    def test_run_splits_accept_revise_and_reject(self) -> None:
        records = [case("case_1"), case("case_2"), case("case_3")]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "generated_cases.json"
            input_path.write_text(json.dumps(records), encoding="utf-8")
            config = PipelineConfig(
                outputs_dir=root / "outputs",
                stage_3_5_prompt_path=Path(__file__).parents[1]
                / "data_creation"
                / "prompts"
                / "case_validation_prompt.txt",
            )
            run_dir = run_stage_3_5(
                config,
                input_path=input_path,
                provider=FakeProvider([outcome("ACCEPT"), outcome("REVISE"), outcome("REJECT")]),
                run_id="validation-test",
            )
            validated = json.loads((run_dir / "validated_cases.json").read_text())
            manifest = json.loads((run_dir / "stage_3_5_manifest.json").read_text())
            self.assertEqual(len(validated), 3)
            self.assertEqual(len(json.loads((run_dir / "accepted_cases.json").read_text())), 1)
            self.assertEqual(len(json.loads((run_dir / "revised_cases.json").read_text())), 1)
            self.assertEqual(len(json.loads((run_dir / "rejected_cases.json").read_text())), 1)
            self.assertEqual(manifest["failure_count"], 0)
            self.assertIsNone(validated[2]["final_case"])


if __name__ == "__main__":
    unittest.main()
