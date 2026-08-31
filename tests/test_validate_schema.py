from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_creation.config import PipelineConfig
from data_creation.llm_provider import LLMResponse
from data_creation.validate_schema import (
    build_annotated_span_index,
    consolidated_span,
    run_stage_2_5,
    validate_validation_response,
)


SCHEMA = {
    "premise_pattern": "A small sample of C has P.",
    "invalid_inference": "The sample is treated as representative of C.",
    "conclusion_pattern": "All of C is concluded to have P.",
}


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)

    def generate_json(self, **kwargs) -> LLMResponse:
        return LLMResponse(
            content=next(self.responses),
            response_id="validation-id",
            model=kwargs["model"],
            provider="fake-validator",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        )


def stage_2_record(source_id: str, text: str) -> dict:
    return {
        "source_id": source_id,
        "source_text": text,
        "canonical_fallacy": "Hasty generalization",
        "original_fallacy": "hasty generalization",
        "original_fallacy_labels": ["hasty generalization"],
        "schema": SCHEMA,
    }


class ValidateSchemaTests(unittest.TestCase):
    def test_recovers_exact_annotated_span(self) -> None:
        text = "One C has P. Therefore all C has P."
        start = text.index("Therefore")
        source = json.dumps(
            {"text": text, "labels": [[start, len(text), "hasty generalization"]]}
        )
        record = stage_2_record("mafalda_gold_000001", text)
        index, failures = build_annotated_span_index(source, [record])
        spans = index[(record["source_id"], record["canonical_fallacy"])]
        self.assertEqual(failures, [])
        self.assertEqual(spans[0]["text"], "Therefore all C has P.")
        self.assertEqual(consolidated_span(spans), "Therefore all C has P.")

    def test_validation_contract_for_valid_and_revised_results(self) -> None:
        valid = validate_validation_response(
            json.dumps({"valid": True, "issue": None, "revised_schema": None})
        )
        self.assertTrue(valid["valid"])
        revised = validate_validation_response(
            json.dumps(
                {
                    "valid": False,
                    "issue": "It followed the rebuttal.",
                    "revised_schema": SCHEMA,
                }
            )
        )
        self.assertFalse(revised["valid"])
        with self.assertRaises(ValueError):
            validate_validation_response(
                json.dumps({"valid": True, "issue": "Maybe", "revised_schema": None})
            )

    def test_run_preserves_original_and_applies_only_invalid_revision(self) -> None:
        text1 = "One C has P. Therefore all C has P."
        text2 = "One D has Q. Therefore all D has Q."
        records = [
            stage_2_record("mafalda_gold_000001", text1),
            stage_2_record("mafalda_gold_000002", text2),
        ]
        revised_schema = {
            "premise_pattern": "One observed D has Q.",
            "invalid_inference": "That D is treated as representative.",
            "conclusion_pattern": "All D is concluded to have Q.",
        }
        responses = [
            json.dumps({"valid": True, "issue": None, "revised_schema": None}),
            json.dumps(
                {
                    "valid": False,
                    "issue": "The placeholders were too generic.",
                    "revised_schema": revised_schema,
                }
            ),
        ]
        source_lines = "\n".join(
            [
                json.dumps(
                    {
                        "text": text1,
                        "labels": [[0, len(text1), "hasty generalization"]],
                    }
                ),
                json.dumps(
                    {
                        "text": text2,
                        "labels": [[0, len(text2), "hasty generalization"]],
                    }
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "schemas.json"
            source_path = root / "source.jsonl"
            input_path.write_text(json.dumps(records), encoding="utf-8")
            source_path.write_text(source_lines, encoding="utf-8")
            config = PipelineConfig(
                outputs_dir=root / "outputs",
                stage_2_5_prompt_path=Path(__file__).parents[1]
                / "data_creation"
                / "prompts"
                / "schema_validation_prompt.txt",
            )
            run_dir = run_stage_2_5(
                config,
                input_path=input_path,
                source_path=source_path,
                provider=FakeProvider(responses),
                run_id="validation-test",
            )
            output = json.loads((run_dir / "validated_schemas.json").read_text())
            manifest = json.loads((run_dir / "stage_2_5_manifest.json").read_text())
            self.assertEqual(output[0]["original_schema"], output[0]["final_schema"])
            self.assertEqual(output[1]["original_schema"], SCHEMA)
            self.assertEqual(output[1]["final_schema"], revised_schema)
            self.assertEqual(manifest["accepted_unchanged_count"], 1)
            self.assertEqual(manifest["revised_count"], 1)
            self.assertTrue((run_dir / "stage_2_5_review.md").exists())


if __name__ == "__main__":
    unittest.main()

