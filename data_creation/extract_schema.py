"""Stage 2: extract topic-independent fallacy schemas from Stage 1 records."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from data_creation.config import PROJECT_ROOT, PipelineConfig
from data_creation.llm_provider import LLMResponse, OpenRouterProvider, ProviderError, load_dotenv
from data_creation.load_source import create_run_dir


LOGGER = logging.getLogger(__name__)
SCHEMA_FIELDS = ("premise_pattern", "invalid_inference", "conclusion_pattern")
SCHEMA_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {field: {"type": "string", "minLength": 1} for field in SCHEMA_FIELDS},
    "required": list(SCHEMA_FIELDS),
    "additionalProperties": False,
}


class JSONGenerator(Protocol):
    def generate_json(
        self,
        *,
        prompt: str,
        model: str,
        temperature: float,
        seed: int,
        max_tokens: int,
        json_schema: dict[str, Any],
    ) -> LLMResponse: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_stage_1_records(path: Path) -> list[dict[str, Any]]:
    """Load and structurally check the Stage 1 JSON array."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stage 1 input is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("Stage 1 input must be a JSON array.")
    required = ("source_id", "source_text", "fallacy", "source_fallacy_labels")
    for index, record in enumerate(value):
        if not isinstance(record, dict) or any(key not in record for key in required):
            raise ValueError(f"Stage 1 record {index} is missing required fields.")
        if not isinstance(record["source_fallacy_labels"], list):
            raise ValueError(f"Stage 1 record {index} has invalid source_fallacy_labels.")
    return value


def select_stratified_pilot(
    records: list[dict[str, Any]],
    canonical_fallacies: list[str],
    *,
    records_per_fallacy: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Reproducibly sample an equal number of records for each fallacy."""

    if records_per_fallacy <= 0:
        raise ValueError("records_per_fallacy must be positive.")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["fallacy"]].append(record)

    selected: list[dict[str, Any]] = []
    for fallacy_index, fallacy in enumerate(canonical_fallacies):
        candidates = sorted(
            grouped.get(fallacy, []),
            key=lambda item: (str(item["source_id"]), str(item["source_text"])),
        )
        if len(candidates) < records_per_fallacy:
            raise ValueError(
                f"Requested {records_per_fallacy} records for {fallacy!r}, "
                f"but only {len(candidates)} are available."
            )
        # An independent per-stratum RNG makes one fallacy's sample stable if
        # another fallacy is added or removed from the configuration.
        rng = random.Random(f"{seed}:{fallacy_index}:{fallacy}")
        indices = sorted(rng.sample(range(len(candidates)), records_per_fallacy))
        selected.extend(candidates[index] for index in indices)
    return selected


def render_prompt(
    template: str,
    *,
    source_text: str,
    canonical_fallacy: str,
    original_fallacy: str,
) -> str:
    replacements = {
        "{{SOURCE_TEXT}}": source_text,
        "{{CANONICAL_FALLACY}}": canonical_fallacy,
        "{{ORIGINAL_FALLACY}}": original_fallacy,
    }
    prompt = template
    for marker, value in replacements.items():
        prompt = prompt.replace(marker, value)
    if "{{" in prompt or "}}" in prompt:
        raise ValueError("The schema prompt contains an unknown or unreplaced placeholder.")
    return prompt


def validate_schema_response(raw_content: str) -> dict[str, str]:
    """Apply Stage 2's intentionally lightweight structural validation."""

    try:
        value = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Schema response must be a JSON object.")
    if set(value) != set(SCHEMA_FIELDS):
        missing = sorted(set(SCHEMA_FIELDS) - set(value))
        extra = sorted(set(value) - set(SCHEMA_FIELDS))
        raise ValueError(f"Schema fields do not match; missing={missing}, extra={extra}.")
    for field in SCHEMA_FIELDS:
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"Schema field {field!r} must be a non-empty string.")
    return {field: value[field].strip() for field in SCHEMA_FIELDS}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _usage_totals(raw_attempts: list[dict[str, Any]]) -> dict[str, int | float]:
    totals: Counter[str] = Counter()
    for attempt in raw_attempts:
        usage = attempt.get("usage")
        if isinstance(usage, dict):
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] += value
    return dict(totals)


def run_stage_2(
    config: PipelineConfig,
    *,
    input_path: Path,
    provider: JSONGenerator,
    run_id: str | None = None,
) -> Path:
    """Run the stratified Stage 2 pilot and return the immutable run directory."""

    actual_run_id, run_dir = create_run_dir(config.outputs_dir, run_id)
    raw_path = run_dir / "raw_responses.jsonl"
    failures_path = run_dir / "stage_2_failures.jsonl"
    raw_attempts: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    try:
        records = load_stage_1_records(input_path)
        pilot = select_stratified_pilot(
            records,
            list(config.selected_fallacies),
            records_per_fallacy=config.stage_2_records_per_fallacy,
            seed=config.stage_2_seed,
        )
        template = config.stage_2_prompt_path.read_text(encoding="utf-8")
        prompt_sha256 = sha256_file(config.stage_2_prompt_path)
        _write_json(run_dir / "pilot_selection.json", pilot)
        raw_path.touch()
        failures_path.touch()

        for position, source in enumerate(pilot, start=1):
            canonical_fallacy = source["fallacy"]
            original_labels = [str(label) for label in source["source_fallacy_labels"]]
            original_fallacy = " | ".join(original_labels)
            base_prompt = render_prompt(
                template,
                source_text=source["source_text"],
                canonical_fallacy=canonical_fallacy,
                original_fallacy=original_fallacy,
            )
            last_error = "No attempt was made."

            for attempt_number in range(1, config.stage_2_max_retries + 1):
                prompt = base_prompt
                if attempt_number > 1:
                    prompt += (
                        "\n\nRetry correction: The previous attempt failed structural validation: "
                        f"{last_error} Return only the required JSON object."
                    )
                attempt_record: dict[str, Any] = {
                    "source_id": source["source_id"],
                    "canonical_fallacy": canonical_fallacy,
                    "original_fallacy": original_fallacy,
                    "attempt": attempt_number,
                    "requested_model": config.stage_2_model,
                    "timestamp": utc_now(),
                }
                try:
                    response = provider.generate_json(
                        prompt=prompt,
                        model=config.stage_2_model,
                        temperature=config.stage_2_temperature,
                        seed=config.stage_2_seed,
                        max_tokens=config.stage_2_max_tokens,
                        json_schema=SCHEMA_JSON_SCHEMA,
                    )
                    attempt_record.update(
                        {
                            "response_id": response.response_id,
                            "response_model": response.model,
                            "provider": response.provider,
                            "usage": response.usage,
                            "raw_response": response.content,
                        }
                    )
                    schema = validate_schema_response(response.content)
                    attempt_record["structurally_valid"] = True
                    raw_attempts.append(attempt_record)
                    _append_jsonl(raw_path, attempt_record)
                    successes.append(
                        {
                            "source_id": source["source_id"],
                            "source_text": source["source_text"],
                            "canonical_fallacy": canonical_fallacy,
                            "original_fallacy": original_fallacy,
                            "original_fallacy_labels": original_labels,
                            "schema": schema,
                            "generation_metadata": {
                                "model": response.model or config.stage_2_model,
                                "requested_model": config.stage_2_model,
                                "temperature": config.stage_2_temperature,
                                "seed": config.stage_2_seed,
                                "prompt_version": config.stage_2_prompt_version,
                                "prompt_sha256": prompt_sha256,
                                "timestamp": utc_now(),
                                "attempts": attempt_number,
                                "response_id": response.response_id,
                                "provider": response.provider,
                                "usage": response.usage,
                            },
                        }
                    )
                    LOGGER.info(
                        "Extracted schema %d/%d for %s (%s)",
                        position,
                        len(pilot),
                        source["source_id"],
                        canonical_fallacy,
                    )
                    break
                except (ProviderError, ValueError) as exc:
                    last_error = str(exc)
                    attempt_record.update(
                        {"structurally_valid": False, "error": last_error}
                    )
                    raw_attempts.append(attempt_record)
                    _append_jsonl(raw_path, attempt_record)
                    LOGGER.warning(
                        "Attempt %d/%d failed for %s: %s",
                        attempt_number,
                        config.stage_2_max_retries,
                        source["source_id"],
                        last_error,
                    )
                    if attempt_number < config.stage_2_max_retries:
                        time.sleep(min(2 ** (attempt_number - 1), 4))
            else:
                failure = {
                    "stage": "extract_schema",
                    "source_id": source["source_id"],
                    "canonical_fallacy": canonical_fallacy,
                    "original_fallacy": original_fallacy,
                    "attempts": config.stage_2_max_retries,
                    "error": last_error,
                    "timestamp": utc_now(),
                }
                failures.append(failure)
                _append_jsonl(failures_path, failure)

        _write_json(run_dir / "schemas.json", successes)
        manifest = {
            "run_id": actual_run_id,
            "stage": 2,
            "stage_name": "extract_schema",
            "created_at": utc_now(),
            "input": {
                "path": str(input_path.resolve()),
                "sha256": sha256_file(input_path),
                "available_record_count": len(records),
            },
            "sampling": {
                "method": "independent deterministic stratified random sample",
                "seed": config.stage_2_seed,
                "records_per_fallacy": config.stage_2_records_per_fallacy,
                "canonical_fallacies": list(config.selected_fallacies),
                "selected_record_count": len(pilot),
            },
            "generation": {
                "provider": "OpenRouter",
                "requested_model": config.stage_2_model,
                "temperature": config.stage_2_temperature,
                "seed": config.stage_2_seed,
                "max_tokens": config.stage_2_max_tokens,
                "max_attempts_per_record": config.stage_2_max_retries,
                "prompt_version": config.stage_2_prompt_version,
                "prompt_path": str(config.stage_2_prompt_path.resolve()),
                "prompt_sha256": prompt_sha256,
            },
            "success_count": len(successes),
            "failure_count": len(failures),
            "raw_attempt_count": len(raw_attempts),
            "usage_totals": _usage_totals(raw_attempts),
            "outputs": {
                "schemas": "schemas.json",
                "raw_responses": "raw_responses.jsonl",
                "failures": "stage_2_failures.jsonl",
                "pilot_selection": "pilot_selection.json",
            },
        }
        _write_json(run_dir / "stage_2_manifest.json", manifest)
    except Exception as exc:
        _write_json(
            run_dir / "run_error.json",
            {
                "run_id": actual_run_id,
                "stage": 2,
                "stage_name": "extract_schema",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        LOGGER.exception("Stage 2 failed; incomplete run retained at %s", run_dir)
        raise

    LOGGER.info(
        "Stage 2 completed with %d schemas and %d failures in %s",
        len(successes),
        len(failures),
        run_dir,
    )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(description="Extract abstract fallacy schemas (Stage 2).")
    parser.add_argument("--input", type=Path, required=True, help="Stage 1 source_filtered.json")
    parser.add_argument("--outputs-dir", type=Path, default=defaults.outputs_dir)
    parser.add_argument("--run-id", help="Optional immutable run ID")
    parser.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", defaults.stage_2_model))
    parser.add_argument("--temperature", type=float, default=defaults.stage_2_temperature)
    parser.add_argument("--seed", type=int, default=defaults.stage_2_seed)
    parser.add_argument(
        "--records-per-fallacy", type=int, default=defaults.stage_2_records_per_fallacy
    )
    parser.add_argument("--max-retries", type=int, default=defaults.stage_2_max_retries)
    parser.add_argument("--max-tokens", type=int, default=defaults.stage_2_max_tokens)
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("OPENROUTER_BASE_URL", defaults.openrouter_api_base_url),
    )
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        LOGGER.error("OPENROUTER_API_KEY is not configured in the environment or .env file.")
        return 2
    defaults = PipelineConfig()
    config = PipelineConfig(
        outputs_dir=args.outputs_dir.resolve(),
        stage_2_model=args.model,
        stage_2_temperature=args.temperature,
        stage_2_seed=args.seed,
        stage_2_records_per_fallacy=args.records_per_fallacy,
        stage_2_max_retries=args.max_retries,
        stage_2_max_tokens=args.max_tokens,
        openrouter_api_base_url=args.api_base_url,
    )
    provider = OpenRouterProvider(
        api_key=api_key,
        base_url=config.openrouter_api_base_url,
        timeout_seconds=args.request_timeout,
    )
    try:
        run_dir = run_stage_2(
            config,
            input_path=args.input.resolve(),
            provider=provider,
            run_id=args.run_id,
        )
    except Exception as exc:
        LOGGER.error("Stage 2 did not complete: %s", exc)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
