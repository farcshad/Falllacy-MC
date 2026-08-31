"""Stage 2.5: semantically validate and, when needed, refine Stage 2 schemas."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from data_creation.config import PROJECT_ROOT, PipelineConfig
from data_creation.extract_schema import SCHEMA_FIELDS, validate_schema_response
from data_creation.llm_provider import LLMResponse, OpenRouterProvider, ProviderError, load_dotenv
from data_creation.load_source import create_run_dir, normalize_label, read_source


LOGGER = logging.getLogger(__name__)
VALIDATION_FIELDS = ("valid", "issue", "revised_schema")
SCHEMA_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {field: {"type": "string", "minLength": 1} for field in SCHEMA_FIELDS},
    "required": list(SCHEMA_FIELDS),
    "additionalProperties": False,
}
VALIDATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "issue": {"type": ["string", "null"]},
        "revised_schema": {"anyOf": [SCHEMA_OBJECT_SCHEMA, {"type": "null"}]},
    },
    "required": list(VALIDATION_FIELDS),
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


def load_stage_2_schemas(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stage 2 schemas are not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("Stage 2 schemas must be a JSON array.")
    required = (
        "source_id",
        "source_text",
        "canonical_fallacy",
        "original_fallacy",
        "schema",
    )
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict) or any(key not in record for key in required):
            raise ValueError(f"Stage 2 record {index} is missing required fields.")
        validate_schema_response(json.dumps(record["schema"]))
        key = (str(record["source_id"]), str(record["canonical_fallacy"]))
        if key in seen:
            raise ValueError(f"Duplicate Stage 2 source/fallacy pair: {key!r}.")
        seen.add(key)
    return value


def _iter_span_annotations(labels: Any) -> list[tuple[int, int, str]]:
    annotations: list[tuple[int, int, str]] = []
    if not isinstance(labels, list):
        return annotations
    for annotation in labels:
        if (
            isinstance(annotation, list)
            and len(annotation) >= 3
            and isinstance(annotation[0], int)
            and isinstance(annotation[1], int)
            and isinstance(annotation[2], str)
        ):
            annotations.append((annotation[0], annotation[1], annotation[2]))
    return annotations


def build_annotated_span_index(
    source_jsonl: str,
    stage_2_records: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    """Recover exact MAFALDA spans for each Stage 2 source/fallacy pair."""

    source_rows: dict[int, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    for line_number, line in enumerate(io.StringIO(source_jsonl), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            failures.append(
                {
                    "stage": "recover_annotated_spans",
                    "line_number": line_number,
                    "error": f"Malformed source JSON: {exc}",
                }
            )
            continue
        if isinstance(row, dict):
            source_rows[line_number] = row

    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in stage_2_records:
        source_id = str(record["source_id"])
        match = re.fullmatch(r"mafalda_gold_(\d+)", source_id)
        key = (source_id, str(record["canonical_fallacy"]))
        if match is None:
            failures.append(
                {
                    "stage": "recover_annotated_spans",
                    "source_id": source_id,
                    "error": "Source ID does not encode a MAFALDA line number.",
                }
            )
            index[key] = []
            continue
        row = source_rows.get(int(match.group(1)))
        if row is None or not isinstance(row.get("text"), str):
            failures.append(
                {
                    "stage": "recover_annotated_spans",
                    "source_id": source_id,
                    "error": "Corresponding MAFALDA source row was not found.",
                }
            )
            index[key] = []
            continue
        source_text = row["text"]
        if source_text != record["source_text"]:
            failures.append(
                {
                    "stage": "recover_annotated_spans",
                    "source_id": source_id,
                    "error": "Stage 2 text does not match the official source row.",
                }
            )
            index[key] = []
            continue

        originals = record.get("original_fallacy_labels")
        if not isinstance(originals, list) or not originals:
            originals = [part.strip() for part in str(record["original_fallacy"]).split("|")]
        wanted = {normalize_label(str(label)) for label in originals}
        spans: list[dict[str, Any]] = []
        seen: set[tuple[int, int, str]] = set()
        for start, end, label in _iter_span_annotations(row.get("labels")):
            if normalize_label(label) not in wanted:
                continue
            if not (0 <= start < end <= len(source_text)):
                failures.append(
                    {
                        "stage": "recover_annotated_spans",
                        "source_id": source_id,
                        "error": f"Invalid source offsets [{start}, {end}] for {label!r}.",
                    }
                )
                continue
            identity = (start, end, label)
            if identity in seen:
                continue
            seen.add(identity)
            spans.append(
                {
                    "text": source_text[start:end],
                    "original_fallacy": label,
                    "start": start,
                    "end": end,
                }
            )
        index[key] = spans
    return index, failures


def consolidated_span(spans: list[dict[str, Any]]) -> str | None:
    """Create the single optional annotated_span field without losing provenance."""

    distinct_texts: list[str] = []
    for span in spans:
        text = str(span["text"]).strip()
        if text and text not in distinct_texts:
            distinct_texts.append(text)
    if not distinct_texts:
        return None
    return "\n[Separate annotated span]\n".join(distinct_texts)


def render_validation_prompt(
    template: str,
    *,
    source_text: str,
    annotated_span: str | None,
    canonical_fallacy: str,
    original_fallacy: str,
    generated_schema: dict[str, str],
) -> str:
    replacements = {
        "{{SOURCE_TEXT}}": source_text,
        "{{ANNOTATED_SPAN}}": annotated_span or "Not available; use the full source argument.",
        "{{CANONICAL_FALLACY}}": canonical_fallacy,
        "{{ORIGINAL_FALLACY}}": original_fallacy,
        "{{GENERATED_SCHEMA}}": json.dumps(generated_schema, indent=2, ensure_ascii=False),
    }
    prompt = template
    for marker, value in replacements.items():
        prompt = prompt.replace(marker, value)
    if "{{" in prompt or "}}" in prompt:
        raise ValueError("The validation prompt contains an unknown or unreplaced placeholder.")
    return prompt


def validate_validation_response(raw_content: str) -> dict[str, Any]:
    try:
        value = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != set(VALIDATION_FIELDS):
        raise ValueError("Validation response must contain exactly valid, issue, revised_schema.")
    if not isinstance(value["valid"], bool):
        raise ValueError("Validation field 'valid' must be a boolean.")
    if value["valid"]:
        if value["issue"] is not None or value["revised_schema"] is not None:
            raise ValueError("A valid result must have null issue and revised_schema.")
        return {"valid": True, "issue": None, "revised_schema": None}
    if not isinstance(value["issue"], str) or not value["issue"].strip():
        raise ValueError("An invalid result must have a non-empty issue.")
    if not isinstance(value["revised_schema"], dict):
        raise ValueError("An invalid result must provide revised_schema.")
    revised = validate_schema_response(json.dumps(value["revised_schema"]))
    return {"valid": False, "issue": value["issue"].strip(), "revised_schema": revised}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def usage_totals(raw_attempts: list[dict[str, Any]]) -> dict[str, int | float]:
    totals: Counter[str] = Counter()
    for attempt in raw_attempts:
        usage = attempt.get("usage")
        if isinstance(usage, dict):
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] += value
    return dict(totals)


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.strip().splitlines())


def write_review(path: Path, records: list[dict[str, Any]], failure_count: int) -> None:
    accepted = sum(record["semantic_validation"]["valid"] for record in records)
    revised = len(records) - accepted
    sections = [
        "# Stage 2.5 — Semantic Schema Validation Review",
        "",
        f"- Total schemas: {len(records)}",
        f"- Accepted unchanged: {accepted}",
        f"- Revised: {revised}",
        f"- Failed validation/API: {failure_count}",
        "",
        "The judgments below are LLM semantic-validation results and remain subject to manual review.",
    ]
    for index, record in enumerate(records, start=1):
        original = record["original_schema"]
        final = record["final_schema"]
        validation = record["semantic_validation"]
        issue = validation["issue"] or "None; accepted unchanged."
        span = record["annotated_span"] or "Not available."
        sections.extend(
            [
                "",
                f"## {index}. `{record['source_id']}`",
                "",
                "**SOURCE**",
                "",
                _blockquote(record["source_text"]),
                "",
                f"**FALLACY:** {record['canonical_fallacy']}  ",
                f"**ORIGINAL ANNOTATION:** {record['original_fallacy']}",
                "",
                "**ANNOTATED SPAN**",
                "",
                _blockquote(span),
                "",
                "**ORIGINAL SCHEMA**",
                "",
                f"- Premise: {original['premise_pattern']}",
                f"- Invalid inference: {original['invalid_inference']}",
                f"- Conclusion: {original['conclusion_pattern']}",
                "",
                "**VALIDATION**",
                "",
                f"- Valid: {'yes' if validation['valid'] else 'no'}",
                f"- Issue: {issue}",
                "",
                "**FINAL SCHEMA**",
                "",
                f"- Premise: {final['premise_pattern']}",
                f"- Invalid inference: {final['invalid_inference']}",
                f"- Conclusion: {final['conclusion_pattern']}",
            ]
        )
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def run_stage_2_5(
    config: PipelineConfig,
    *,
    input_path: Path,
    provider: JSONGenerator,
    source_path: Path | None = None,
    run_id: str | None = None,
) -> Path:
    actual_run_id, run_dir = create_run_dir(config.outputs_dir, run_id)
    raw_path = run_dir / "raw_validation_responses.jsonl"
    failures_path = run_dir / "stage_2_5_failures.jsonl"
    raw_attempts: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    try:
        schemas = load_stage_2_schemas(input_path)
        payload = read_source(
            source_path=source_path,
            source_url=config.source_url,
            timeout_seconds=config.source_timeout_seconds,
        )
        span_index, span_failures = build_annotated_span_index(payload.text, schemas)
        for failure in span_failures:
            failures.append(failure)
            _append_jsonl(failures_path, failure)

        template = config.stage_2_5_prompt_path.read_text(encoding="utf-8")
        prompt_sha256 = sha256_file(config.stage_2_5_prompt_path)
        raw_path.touch()
        failures_path.touch(exist_ok=True)

        for position, record in enumerate(schemas, start=1):
            key = (str(record["source_id"]), str(record["canonical_fallacy"]))
            spans = span_index.get(key, [])
            annotated_span = consolidated_span(spans)
            original_schema = validate_schema_response(json.dumps(record["schema"]))
            base_prompt = render_validation_prompt(
                template,
                source_text=record["source_text"],
                annotated_span=annotated_span,
                canonical_fallacy=record["canonical_fallacy"],
                original_fallacy=record["original_fallacy"],
                generated_schema=original_schema,
            )
            last_error = "No attempt was made."

            for attempt_number in range(1, config.stage_2_5_max_retries + 1):
                prompt = base_prompt
                if attempt_number > 1:
                    prompt += (
                        "\n\nRetry correction: The prior response failed structural validation: "
                        f"{last_error} Return only the required JSON object."
                    )
                attempt_record: dict[str, Any] = {
                    "source_id": record["source_id"],
                    "canonical_fallacy": record["canonical_fallacy"],
                    "attempt": attempt_number,
                    "requested_model": config.stage_2_5_model,
                    "timestamp": utc_now(),
                }
                try:
                    response = provider.generate_json(
                        prompt=prompt,
                        model=config.stage_2_5_model,
                        temperature=config.stage_2_5_temperature,
                        seed=config.stage_2_5_seed,
                        max_tokens=config.stage_2_5_max_tokens,
                        json_schema=VALIDATION_JSON_SCHEMA,
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
                    validation = validate_validation_response(response.content)
                    attempt_record["structurally_valid"] = True
                    raw_attempts.append(attempt_record)
                    _append_jsonl(raw_path, attempt_record)
                    final_schema = (
                        original_schema if validation["valid"] else validation["revised_schema"]
                    )
                    results.append(
                        {
                            "source_id": record["source_id"],
                            "source_text": record["source_text"],
                            "canonical_fallacy": record["canonical_fallacy"],
                            "original_fallacy": record["original_fallacy"],
                            "annotated_span": annotated_span,
                            "annotated_spans": spans,
                            "original_schema": original_schema,
                            "semantic_validation": {
                                "valid": validation["valid"],
                                "issue": validation["issue"],
                            },
                            "final_schema": final_schema,
                            "validation_metadata": {
                                "model": response.model or config.stage_2_5_model,
                                "requested_model": config.stage_2_5_model,
                                "temperature": config.stage_2_5_temperature,
                                "seed": config.stage_2_5_seed,
                                "prompt_version": config.stage_2_5_prompt_version,
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
                        "Validated schema %d/%d for %s: %s",
                        position,
                        len(schemas),
                        record["source_id"],
                        "accepted" if validation["valid"] else "revised",
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
                        config.stage_2_5_max_retries,
                        record["source_id"],
                        last_error,
                    )
                    if attempt_number < config.stage_2_5_max_retries:
                        time.sleep(min(2 ** (attempt_number - 1), 4))
            else:
                failure = {
                    "stage": "validate_schema",
                    "source_id": record["source_id"],
                    "canonical_fallacy": record["canonical_fallacy"],
                    "attempts": config.stage_2_5_max_retries,
                    "error": last_error,
                    "timestamp": utc_now(),
                }
                failures.append(failure)
                _append_jsonl(failures_path, failure)

        _write_json(run_dir / "validated_schemas.json", results)
        write_review(run_dir / "stage_2_5_review.md", results, len(failures))
        accepted_count = sum(result["semantic_validation"]["valid"] for result in results)
        manifest = {
            "run_id": actual_run_id,
            "stage": 2.5,
            "stage_name": "semantic_schema_validation",
            "created_at": utc_now(),
            "input": {
                "path": str(input_path.resolve()),
                "sha256": sha256_file(input_path),
                "record_count": len(schemas),
            },
            "source_annotations": {
                "location": payload.location,
                "sha256": payload.sha256,
                "records_with_annotated_span": sum(
                    result["annotated_span"] is not None for result in results
                ),
                "span_recovery_failure_count": len(span_failures),
            },
            "validation": {
                "provider": "OpenRouter",
                "requested_model": config.stage_2_5_model,
                "temperature": config.stage_2_5_temperature,
                "seed": config.stage_2_5_seed,
                "max_tokens": config.stage_2_5_max_tokens,
                "max_attempts_per_record": config.stage_2_5_max_retries,
                "prompt_version": config.stage_2_5_prompt_version,
                "prompt_path": str(config.stage_2_5_prompt_path.resolve()),
                "prompt_sha256": prompt_sha256,
            },
            "total_schema_count": len(schemas),
            "completed_validation_count": len(results),
            "accepted_unchanged_count": accepted_count,
            "revised_count": len(results) - accepted_count,
            "failure_count": len(failures),
            "raw_attempt_count": len(raw_attempts),
            "usage_totals": usage_totals(raw_attempts),
            "outputs": {
                "validated_schemas": "validated_schemas.json",
                "raw_validation_responses": "raw_validation_responses.jsonl",
                "failures": "stage_2_5_failures.jsonl",
                "review": "stage_2_5_review.md",
            },
        }
        _write_json(run_dir / "stage_2_5_manifest.json", manifest)
    except Exception as exc:
        _write_json(
            run_dir / "run_error.json",
            {
                "run_id": actual_run_id,
                "stage": 2.5,
                "stage_name": "semantic_schema_validation",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        LOGGER.exception("Stage 2.5 failed; incomplete run retained at %s", run_dir)
        raise

    LOGGER.info(
        "Stage 2.5 completed with %d results and %d failures in %s",
        len(results),
        len(failures),
        run_dir,
    )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(description="Semantically validate Stage 2 schemas.")
    parser.add_argument("--input", type=Path, required=True, help="Stage 2 schemas.json")
    parser.add_argument(
        "--source-path", type=Path, help="Optional local official MAFALDA JSONL source"
    )
    parser.add_argument("--outputs-dir", type=Path, default=defaults.outputs_dir)
    parser.add_argument("--run-id", help="Optional immutable run ID")
    parser.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", defaults.stage_2_5_model))
    parser.add_argument("--temperature", type=float, default=defaults.stage_2_5_temperature)
    parser.add_argument("--seed", type=int, default=defaults.stage_2_5_seed)
    parser.add_argument("--max-retries", type=int, default=defaults.stage_2_5_max_retries)
    parser.add_argument("--max-tokens", type=int, default=defaults.stage_2_5_max_tokens)
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
        stage_2_5_model=args.model,
        stage_2_5_temperature=args.temperature,
        stage_2_5_seed=args.seed,
        stage_2_5_max_retries=args.max_retries,
        stage_2_5_max_tokens=args.max_tokens,
        openrouter_api_base_url=args.api_base_url,
    )
    provider = OpenRouterProvider(
        api_key=api_key,
        base_url=config.openrouter_api_base_url,
        timeout_seconds=args.request_timeout,
    )
    try:
        run_dir = run_stage_2_5(
            config,
            input_path=args.input.resolve(),
            provider=provider,
            source_path=args.source_path.resolve() if args.source_path else None,
            run_id=args.run_id,
        )
    except Exception as exc:
        LOGGER.error("Stage 2.5 did not complete: %s", exc)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

