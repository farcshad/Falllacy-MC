"""Stage 1: load and filter source fallacy examples without altering them."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import re
import sys
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

from data_creation.config import PipelineConfig


LOGGER = logging.getLogger(__name__)
SOURCE_DATASET_NAME = "MAFALDA gold standard"
SOURCE_DATASET_REPOSITORY = "https://github.com/ChadiHelwe/MAFALDA"


@dataclass(frozen=True)
class SourcePayload:
    """An opened source plus reproducibility information."""

    text: str
    location: str
    sha256: str


def normalize_label(label: str) -> str:
    """Normalize a label only for matching; never mutate stored source text."""

    return re.sub(r"\s+", " ", label.replace("_", " ").replace("-", " ")).strip().casefold()


def build_alias_index(
    selected_fallacies: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    """Return normalized source label -> canonical label, rejecting ambiguity."""

    index: dict[str, str] = {}
    for canonical, aliases in selected_fallacies.items():
        for alias in (canonical, *aliases):
            normalized = normalize_label(alias)
            previous = index.get(normalized)
            if previous is not None and previous != canonical:
                raise ValueError(
                    f"Source label alias {alias!r} maps to both {previous!r} "
                    f"and {canonical!r}."
                )
            index[normalized] = canonical
    return index


def read_source(
    *, source_path: Path | None, source_url: str, timeout_seconds: float
) -> SourcePayload:
    """Read source data from a local file or URL, without writing a raw copy."""

    if source_path is not None:
        raw = source_path.read_bytes()
        location = str(source_path.resolve())
    else:
        request = urllib.request.Request(
            source_url,
            headers={"User-Agent": "misconception-data-pipeline/0.1"},
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
        location = source_url

    return SourcePayload(
        text=raw.decode("utf-8-sig"),
        location=location,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _iter_label_strings(value: Any) -> Iterator[str]:
    """Yield native labels from MAFALDA-style span annotations.

    MAFALDA currently represents each annotation as [start, end, label]. The
    recursive fallback keeps Stage 1 tolerant of nested disjunctive annotations.
    """

    if isinstance(value, dict):
        for key in ("label", "fallacy", "type"):
            label = value.get(key)
            if isinstance(label, str):
                yield label
                return
        for nested in value.values():
            yield from _iter_label_strings(nested)
        return

    if not isinstance(value, (list, tuple)):
        return

    if (
        len(value) >= 3
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
        and isinstance(value[2], str)
    ):
        yield value[2]
        return

    for nested in value:
        yield from _iter_label_strings(nested)


def filter_records(
    stream: TextIO,
    selected_fallacies: dict[str, tuple[str, ...]],
    *,
    failures: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Parse JSONL and emit one record per source-text/canonical-fallacy pair."""

    alias_index = build_alias_index(selected_fallacies)
    output: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    failures = failures if failures is not None else []

    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        try:
            raw_record = json.loads(line)
        except json.JSONDecodeError as exc:
            failures.append(
                {
                    "stage": "load_source",
                    "line_number": line_number,
                    "error_type": "malformed_json",
                    "error": str(exc),
                }
            )
            LOGGER.error("Skipping malformed JSON on source line %d: %s", line_number, exc)
            continue

        source_text = raw_record.get("text")
        labels = raw_record.get("labels")
        if not isinstance(source_text, str) or not source_text.strip():
            failures.append(
                {
                    "stage": "load_source",
                    "line_number": line_number,
                    "error_type": "missing_source_text",
                    "error": "Expected a non-empty string in field 'text'.",
                }
            )
            LOGGER.error("Skipping source line %d: missing text", line_number)
            continue

        native_by_canonical: dict[str, list[str]] = {}
        for native_label in _iter_label_strings(labels):
            canonical = alias_index.get(normalize_label(native_label))
            if canonical is not None:
                native_by_canonical.setdefault(canonical, [])
                if native_label not in native_by_canonical[canonical]:
                    native_by_canonical[canonical].append(native_label)

        source_id = f"mafalda_gold_{line_number:06d}"
        for canonical in selected_fallacies:  # preserve configured order
            native_labels = native_by_canonical.get(canonical)
            if not native_labels:
                continue
            output.append(
                {
                    "source_id": source_id,
                    "source_text": source_text,
                    "fallacy": canonical,
                    "source_fallacy_labels": native_labels,
                    "source_line_number": line_number,
                }
            )
            counts[canonical] += 1

    return output, dict(counts)


def create_run_dir(outputs_dir: Path, requested_run_id: str | None = None) -> tuple[str, Path]:
    """Create a new run directory and refuse to overwrite an existing run."""

    run_id = requested_run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError(
            "run_id must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_' or '-'."
        )
    run_dir = outputs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_failures(path: Path, failures: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for failure in failures:
            handle.write(json.dumps(failure, ensure_ascii=False) + "\n")


def run_stage_1(
    config: PipelineConfig,
    *,
    source_path: Path | None = None,
    run_id: str | None = None,
) -> Path:
    """Execute Stage 1 and return its newly created run directory."""

    actual_run_id, run_dir = create_run_dir(config.outputs_dir, run_id)
    failures: list[dict[str, Any]] = []

    try:
        payload = read_source(
            source_path=source_path,
            source_url=config.source_url,
            timeout_seconds=config.source_timeout_seconds,
        )
        records, counts = filter_records(
            io.StringIO(payload.text), config.selected_fallacies, failures=failures
        )
        if not records:
            raise RuntimeError(
                "No source examples matched the selected fallacies. Check the source "
                "file and label aliases in data_creation/config.py."
            )

        _write_json(run_dir / "source_filtered.json", records)
        _write_failures(run_dir / "stage_1_failures.jsonl", failures)
        manifest = {
            "run_id": actual_run_id,
            "stage": 1,
            "stage_name": "load_source",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "dataset": SOURCE_DATASET_NAME,
                "repository": SOURCE_DATASET_REPOSITORY,
                "location": payload.location,
                "sha256": payload.sha256,
            },
            "selected_fallacies": list(config.selected_fallacies),
            "record_count": len(records),
            "counts_by_fallacy": counts,
            "failure_count": len(failures),
            "output_file": "source_filtered.json",
            "failure_file": "stage_1_failures.jsonl",
        }
        _write_json(run_dir / "stage_1_manifest.json", manifest)
    except Exception as exc:
        # A failed run is still an experiment artifact. Keep its identity and
        # failure details instead of leaving an unexplained empty directory.
        error_record = {
            "run_id": actual_run_id,
            "stage": 1,
            "stage_name": "load_source",
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        try:
            _write_json(run_dir / "run_error.json", error_record)
        except OSError:
            LOGGER.exception("Could not write the run error artifact")
        LOGGER.exception("Stage 1 failed; incomplete run retained at %s", run_dir)
        raise

    LOGGER.info("Stage 1 wrote %d filtered records to %s", len(records), run_dir)
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load and filter MAFALDA examples (Stage 1).")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--source-path",
        type=Path,
        help="Read a local MAFALDA JSONL file instead of downloading the official source.",
    )
    source_group.add_argument(
        "--source-url",
        help="Override the official MAFALDA gold-standard raw URL.",
    )
    parser.add_argument("--outputs-dir", type=Path, help="Parent directory for immutable runs.")
    parser.add_argument("--run-id", help="Optional explicit run ID; must not already exist.")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    defaults = PipelineConfig()
    config = PipelineConfig(
        source_url=args.source_url or defaults.source_url,
        outputs_dir=(args.outputs_dir or defaults.outputs_dir).resolve(),
    )
    try:
        run_dir = run_stage_1(config, source_path=args.source_path, run_id=args.run_id)
    except Exception as exc:
        LOGGER.error("Stage 1 did not complete: %s", exc)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
