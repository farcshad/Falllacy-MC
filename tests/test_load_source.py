from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from data_creation.config import PipelineConfig, SELECTED_FALLACIES
from data_creation.load_source import filter_records, run_stage_1


FIXTURE = Path(__file__).parent / "fixtures" / "mafalda_sample.jsonl"


class FilterRecordsTests(unittest.TestCase):
    def test_maps_native_labels_and_deduplicates_one_canonical_category(self) -> None:
        failures: list[dict] = []
        records, counts = filter_records(
            io.StringIO(FIXTURE.read_text(encoding="utf-8")),
            SELECTED_FALLACIES,
            failures=failures,
        )

        self.assertEqual(len(records), 5)
        self.assertEqual(counts["False cause / causal fallacy"], 1)
        causal = next(r for r in records if r["fallacy"] == "False cause / causal fallacy")
        self.assertEqual(
            causal["source_fallacy_labels"],
            ["false causality", "causal oversimplification"],
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error_type"], "malformed_json")

    def test_stage_creates_immutable_run_with_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            outputs = Path(temporary_directory) / "outputs"
            config = PipelineConfig(outputs_dir=outputs)
            run_dir = run_stage_1(config, source_path=FIXTURE, run_id="test-run")

            records = json.loads((run_dir / "source_filtered.json").read_text())
            manifest = json.loads((run_dir / "stage_1_manifest.json").read_text())
            failures = (run_dir / "stage_1_failures.jsonl").read_text().splitlines()
            self.assertEqual(len(records), 5)
            self.assertEqual(manifest["record_count"], 5)
            self.assertEqual(manifest["failure_count"], 1)
            self.assertEqual(len(failures), 1)

            with self.assertRaises(FileExistsError):
                run_stage_1(config, source_path=FIXTURE, run_id="test-run")

    def test_failed_run_keeps_an_error_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            outputs = Path(temporary_directory) / "outputs"
            config = PipelineConfig(outputs_dir=outputs)
            missing_source = Path(temporary_directory) / "does-not-exist.jsonl"

            with self.assertRaises(FileNotFoundError):
                run_stage_1(config, source_path=missing_source, run_id="failed-run")

            error = json.loads(
                (outputs / "failed-run" / "run_error.json").read_text(encoding="utf-8")
            )
            self.assertEqual(error["stage"], 1)
            self.assertEqual(error["error_type"], "FileNotFoundError")


if __name__ == "__main__":
    unittest.main()
