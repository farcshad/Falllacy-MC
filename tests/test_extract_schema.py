from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_creation.config import PipelineConfig, SELECTED_FALLACIES
from data_creation.extract_schema import (
    _usage_totals,
    load_stage_1_records,
    render_prompt,
    run_stage_2,
    select_stratified_pilot,
    validate_schema_response,
)
from data_creation.llm_provider import LLMResponse


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)

    def generate_json(self, **kwargs) -> LLMResponse:
        return LLMResponse(
            content=next(self.responses),
            response_id="fake-id",
            model=kwargs["model"],
            provider="fake",
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )


def make_records(per_fallacy: int = 4) -> list[dict]:
    records = []
    for fallacy in SELECTED_FALLACIES:
        for index in range(per_fallacy):
            records.append(
                {
                    "source_id": f"{fallacy}-{index}",
                    "source_text": f"Concrete argument {index}",
                    "fallacy": fallacy,
                    "source_fallacy_labels": [next(iter(SELECTED_FALLACIES[fallacy]))],
                }
            )
    return records


class ExtractSchemaTests(unittest.TestCase):
    def test_usage_totals_include_cost_but_not_boolean_flags(self) -> None:
        totals = _usage_totals(
            [
                {"usage": {"total_tokens": 10, "cost": 0.01, "is_byok": False}},
                {"usage": {"total_tokens": 20, "cost": 0.02, "is_byok": True}},
            ]
        )
        self.assertEqual(totals["total_tokens"], 30)
        self.assertAlmostEqual(totals["cost"], 0.03)
        self.assertNotIn("is_byok", totals)

    def test_sampling_is_stratified_and_reproducible(self) -> None:
        records = make_records()
        first = select_stratified_pilot(
            records, list(SELECTED_FALLACIES), records_per_fallacy=3, seed=42
        )
        second = select_stratified_pilot(
            list(reversed(records)), list(SELECTED_FALLACIES), records_per_fallacy=3, seed=42
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 15)
        for fallacy in SELECTED_FALLACIES:
            self.assertEqual(sum(r["fallacy"] == fallacy for r in first), 3)

    def test_schema_validation_is_strict_and_nonempty(self) -> None:
        valid = json.dumps(
            {
                "premise_pattern": "Event A occurs.",
                "invalid_inference": "A is assumed to imply B.",
                "conclusion_pattern": "Event B must occur.",
            }
        )
        self.assertEqual(validate_schema_response(valid)["premise_pattern"], "Event A occurs.")
        with self.assertRaises(ValueError):
            validate_schema_response("```json\n{}\n```")
        with self.assertRaises(ValueError):
            validate_schema_response(
                json.dumps(
                    {
                        "premise_pattern": "",
                        "invalid_inference": "x",
                        "conclusion_pattern": "y",
                    }
                )
            )

    def test_prompt_substitution_preserves_braces_in_source(self) -> None:
        prompt = render_prompt(
            "S={{SOURCE_TEXT}} F={{CANONICAL_FALLACY}} O={{ORIGINAL_FALLACY}}",
            source_text="Claim with {literal braces}",
            canonical_fallacy="False dilemma",
            original_fallacy="false dilemma",
        )
        self.assertIn("{literal braces}", prompt)

    def test_run_retries_malformed_response_and_writes_outputs(self) -> None:
        schema = json.dumps(
            {
                "premise_pattern": "A premise about X holds.",
                "invalid_inference": "The premise is used without adequate support.",
                "conclusion_pattern": "A broader conclusion about X is asserted.",
            }
        )
        # One invalid attempt, then one valid retry, followed by 14 valid calls.
        provider = FakeProvider(["not-json", schema] + [schema] * 14)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "source_filtered.json"
            input_path.write_text(json.dumps(make_records()), encoding="utf-8")
            config = PipelineConfig(
                outputs_dir=root / "outputs",
                stage_2_prompt_path=Path(__file__).parents[1]
                / "data_creation"
                / "prompts"
                / "schema_prompt.txt",
            )
            run_dir = run_stage_2(
                config, input_path=input_path, provider=provider, run_id="stage2-test"
            )
            schemas = json.loads((run_dir / "schemas.json").read_text())
            manifest = json.loads((run_dir / "stage_2_manifest.json").read_text())
            raw_lines = (run_dir / "raw_responses.jsonl").read_text().splitlines()
            self.assertEqual(len(schemas), 15)
            self.assertEqual(manifest["success_count"], 15)
            self.assertEqual(manifest["failure_count"], 0)
            self.assertEqual(manifest["raw_attempt_count"], 16)
            self.assertEqual(len(raw_lines), 16)


if __name__ == "__main__":
    unittest.main()
