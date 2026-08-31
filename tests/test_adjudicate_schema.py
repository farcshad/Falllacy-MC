from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_creation.adjudicate_schema import (
    run_stage_2_6,
    validate_adjudication_response,
)
from data_creation.config import PipelineConfig
from data_creation.llm_provider import LLMResponse


SCHEMA = {
    "premise_pattern": "One observed C has P.",
    "invalid_inference": "The observation is treated as representative of C.",
    "conclusion_pattern": "All C is concluded to have P.",
}
REVISED_SCHEMA = {
    "premise_pattern": "A small number of observed C have P.",
    "invalid_inference": "Those observations are treated as representative of C.",
    "conclusion_pattern": "C generally is concluded to have P.",
}


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)

    def generate_json(self, **kwargs) -> LLMResponse:
        return LLMResponse(
            content=next(self.responses),
            response_id="adjudication-id",
            model=kwargs["model"],
            provider="fake-adjudicator",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        )


def record(index: int) -> dict:
    return {
        "source_id": f"mafalda_gold_{index:06d}",
        "source_text": "One observed C has P, so all C has P.",
        "annotated_span": "One observed C has P, so all C has P.",
        "canonical_fallacy": "Hasty generalization",
        "original_fallacy": "hasty generalization",
        "final_schema": SCHEMA,
    }


class AdjudicateSchemaTests(unittest.TestCase):
    def test_decision_invariants(self) -> None:
        accept = validate_adjudication_response(
            json.dumps(
                {
                    "decision": "ACCEPT",
                    "source_suitable": True,
                    "schema_faithful": True,
                    "reason": "Faithful.",
                    "final_schema": SCHEMA,
                }
            ),
            SCHEMA,
        )
        self.assertEqual(accept["decision"], "ACCEPT")
        reject = validate_adjudication_response(
            json.dumps(
                {
                    "decision": "REJECT",
                    "source_suitable": False,
                    "schema_faithful": False,
                    "reason": "The span is a rebuttal.",
                    "final_schema": None,
                }
            ),
            SCHEMA,
        )
        self.assertIsNone(reject["final_schema"])
        with self.assertRaises(ValueError):
            validate_adjudication_response(
                json.dumps(
                    {
                        "decision": "REJECT",
                        "source_suitable": True,
                        "schema_faithful": False,
                        "reason": "Contradictory.",
                        "final_schema": None,
                    }
                ),
                SCHEMA,
            )

    def test_run_counts_rejection_as_outcome_not_failure(self) -> None:
        responses = [
            {
                "decision": "ACCEPT",
                "source_suitable": True,
                "schema_faithful": True,
                "reason": "The schema is faithful.",
                "final_schema": SCHEMA,
            },
            {
                "decision": "REVISE",
                "source_suitable": True,
                "schema_faithful": False,
                "reason": "The quantifier needs correction.",
                "final_schema": REVISED_SCHEMA,
            },
            {
                "decision": "REJECT",
                "source_suitable": False,
                "schema_faithful": False,
                "reason": "The span contains no recoverable inference.",
                "final_schema": None,
            },
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "validated_schemas.json"
            input_path.write_text(json.dumps([record(1), record(2), record(3)]), encoding="utf-8")
            config = PipelineConfig(
                outputs_dir=root / "outputs",
                stage_2_6_prompt_path=Path(__file__).parents[1]
                / "data_creation"
                / "prompts"
                / "schema_adjudication_prompt.txt",
            )
            run_dir = run_stage_2_6(
                config,
                input_path=input_path,
                provider=FakeProvider([json.dumps(response) for response in responses]),
                run_id="adjudication-test",
            )
            output = json.loads((run_dir / "adjudicated_schemas.json").read_text())
            manifest = json.loads((run_dir / "stage_2_6_manifest.json").read_text())
            self.assertEqual(len(output), 3)
            self.assertEqual(manifest["accepted_count"], 1)
            self.assertEqual(manifest["revised_count"], 1)
            self.assertEqual(manifest["rejected_count"], 1)
            self.assertEqual(manifest["failure_count"], 0)
            self.assertIsNone(output[2]["final_schema"])


if __name__ == "__main__":
    unittest.main()

