"""Stage 2.6: precision-first adjudication of Stage 2.5 schemas."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from data_creation.config import PROJECT_ROOT, PipelineConfig
from data_creation.extract_schema import SCHEMA_FIELDS, validate_schema_response
from data_creation.llm_provider import LLMResponse, OpenRouterProvider, ProviderError, load_dotenv
from data_creation.load_source import create_run_dir


LOGGER = logging.getLogger(__name__)
DECISIONS = ("ACCEPT", "REVISE", "REJECT")
ADJUDICATION_FIELDS = (
    "decision",
    "source_suitable",
    "schema_faithful",
    "reason",
    "final_schema",
)
SCHEMA_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {field: {"type": "string", "minLength": 1} for field in SCHEMA_FIELDS},
    "required": list(SCHEMA_FIELDS),
    "additionalProperties": False,
}
ADJUDICATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "source_suitable": {"type": "boolean"},
        "schema_faithful": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1},
        "final_schema": {"anyOf": [SCHEMA_OBJECT_SCHEMA, {"type": "null"}]},
    },
    "required": list(ADJUDICATION_FIELDS),
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
        reasoning_effort: str,
    ) -> LLMResponse: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_stage_2_5_records(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stage 2.5 input is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("Stage 2.5 input must be a JSON array.")
    required = (
        "source_id",
        "source_text",
        "annotated_span",
        "canonical_fallacy",
        "original_fallacy",
        "final_schema",
    )
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict) or any(key not in record for key in required):
            raise ValueError(f"Stage 2.5 record {index} is missing required fields.")
        if not isinstance(record["annotated_span"], str) or not record["annotated_span"].strip():
            raise ValueError(f"Stage 2.5 record {index} has no usable annotated span.")
        validate_schema_response(json.dumps(record["final_schema"]))
        key = (str(record["source_id"]), str(record["canonical_fallacy"]))
        if key in seen:
            raise ValueError(f"Duplicate Stage 2.5 source/fallacy pair: {key!r}.")
        seen.add(key)
    return value


def render_adjudication_prompt(template: str, record: dict[str, Any]) -> str:
    replacements = {
        "{{SOURCE_ID}}": str(record["source_id"]),
        "{{SOURCE_TEXT}}": str(record["source_text"]),
        "{{ANNOTATED_SPAN}}": str(record["annotated_span"]),
        "{{CANONICAL_FALLACY}}": str(record["canonical_fallacy"]),
        "{{ORIGINAL_FALLACY}}": str(record["original_fallacy"]),
        "{{CURRENT_SCHEMA}}": json.dumps(record["final_schema"], indent=2, ensure_ascii=False),
    }
    prompt = template
    for marker, value in replacements.items():
        prompt = prompt.replace(marker, value)
    if "{{" in prompt or "}}" in prompt:
        raise ValueError("The adjudication prompt contains an unknown or unreplaced placeholder.")
    return prompt


def validate_adjudication_response(
    raw_content: str, current_schema: dict[str, str]
) -> dict[str, Any]:
    """Validate both JSON shape and decision-specific semantic invariants."""

    try:
        value = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != set(ADJUDICATION_FIELDS):
        raise ValueError(f"Adjudication must contain exactly {ADJUDICATION_FIELDS}.")
    decision = value["decision"]
    if decision not in DECISIONS:
        raise ValueError(f"Unknown adjudication decision: {decision!r}.")
    if not isinstance(value["source_suitable"], bool) or not isinstance(
        value["schema_faithful"], bool
    ):
        raise ValueError("Suitability and faithfulness fields must be booleans.")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise ValueError("Adjudication reason must be a non-empty string.")

    if decision == "REJECT":
        if value["source_suitable"] or value["schema_faithful"]:
            raise ValueError("REJECT requires source_suitable=false and schema_faithful=false.")
        if value["final_schema"] is not None:
            raise ValueError("REJECT requires final_schema=null.")
        final_schema = None
    else:
        if not isinstance(value["final_schema"], dict):
            raise ValueError(f"{decision} requires a non-null final_schema.")
        final_schema = validate_schema_response(json.dumps(value["final_schema"]))
        if decision == "ACCEPT":
            if not value["source_suitable"] or not value["schema_faithful"]:
                raise ValueError("ACCEPT requires both suitability and faithfulness to be true.")
            if final_schema != current_schema:
                raise ValueError("ACCEPT must preserve the current schema exactly.")
        else:
            if not value["source_suitable"] or value["schema_faithful"]:
                raise ValueError("REVISE requires source_suitable=true and schema_faithful=false.")
            if final_schema == current_schema:
                raise ValueError("REVISE must actually change the schema.")

    return {
        "decision": decision,
        "source_suitable": value["source_suitable"],
        "schema_faithful": value["schema_faithful"],
        "reason": value["reason"].strip(),
        "final_schema": final_schema,
    }


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
    counts = Counter(record["adjudication"]["decision"] for record in records)
    sections = [
        "# Stage 2.6 — Strict Schema Adjudication",
        "",
        f"- Total: {len(records)}",
        f"- Accepted: {counts['ACCEPT']}",
        f"- Revised: {counts['REVISE']}",
        f"- Rejected: {counts['REJECT']}",
        f"- Failed API/validation: {failure_count}",
        "",
        "Rejection is a successful adjudication outcome, not a pipeline failure.",
    ]
    for index, record in enumerate(records, start=1):
        decision = record["adjudication"]["decision"]
        schema = record["final_schema"]
        sections.extend(
            [
                "",
                f"## {index}. `{record['source_id']}`",
                "",
                f"**Fallacy:** {record['canonical_fallacy']}",
                "",
                "**Annotated span**",
                "",
                _blockquote(record["annotated_span"]),
                "",
                f"**Decision:** {decision}",
                "",
                f"**Reason:** {record['adjudication']['reason']}",
                "",
                "**Final schema**",
                "",
            ]
        )
        if schema is None:
            sections.append("Rejected; no final schema.")
        else:
            sections.extend(
                [
                    f"- Premise: {schema['premise_pattern']}",
                    f"- Invalid inference: {schema['invalid_inference']}",
                    f"- Conclusion: {schema['conclusion_pattern']}",
                ]
            )
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def run_stage_2_6(
    config: PipelineConfig,
    *,
    input_path: Path,
    provider: JSONGenerator,
    run_id: str | None = None,
) -> Path:
    actual_run_id, run_dir = create_run_dir(config.outputs_dir, run_id)
    raw_path = run_dir / "raw_responses.jsonl"
    failures_path = run_dir / "stage_2_6_failures.jsonl"
    raw_attempts: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    try:
        records = load_stage_2_5_records(input_path)
        template = config.stage_2_6_prompt_path.read_text(encoding="utf-8")
        prompt_sha256 = sha256_file(config.stage_2_6_prompt_path)
        raw_path.touch()
        failures_path.touch()

        for position, record in enumerate(records, start=1):
            current_schema = validate_schema_response(json.dumps(record["final_schema"]))
            base_prompt = render_adjudication_prompt(template, record)
            last_error = "No attempt was made."

            for attempt_number in range(1, config.stage_2_6_max_retries + 1):
                prompt = base_prompt
                if attempt_number > 1:
                    prompt += (
                        "\n\nRetry correction: The prior response violated the output contract: "
                        f"{last_error} Return only a contract-compliant JSON object."
                    )
                attempt_record: dict[str, Any] = {
                    "source_id": record["source_id"],
                    "canonical_fallacy": record["canonical_fallacy"],
                    "attempt": attempt_number,
                    "requested_model": config.stage_2_6_model,
                    "timestamp": utc_now(),
                }
                try:
                    response = provider.generate_json(
                        prompt=prompt,
                        model=config.stage_2_6_model,
                        temperature=config.stage_2_6_temperature,
                        seed=config.stage_2_6_seed,
                        max_tokens=config.stage_2_6_max_tokens,
                        json_schema=ADJUDICATION_JSON_SCHEMA,
                        reasoning_effort=config.stage_2_6_reasoning_effort,
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
                    adjudication = validate_adjudication_response(
                        response.content, current_schema
                    )
                    attempt_record["structurally_valid"] = True
                    raw_attempts.append(attempt_record)
                    _append_jsonl(raw_path, attempt_record)
                    results.append(
                        {
                            "source_id": record["source_id"],
                            "source_text": record["source_text"],
                            "annotated_span": record["annotated_span"],
                            "canonical_fallacy": record["canonical_fallacy"],
                            "original_fallacy": record["original_fallacy"],
                            "final_schema_from_stage_2_5": current_schema,
                            "adjudication": {
                                "decision": adjudication["decision"],
                                "source_suitable": adjudication["source_suitable"],
                                "schema_faithful": adjudication["schema_faithful"],
                                "reason": adjudication["reason"],
                            },
                            "final_schema": adjudication["final_schema"],
                            "adjudication_metadata": {
                                "model": response.model or config.stage_2_6_model,
                                "requested_model": config.stage_2_6_model,
                                "temperature": config.stage_2_6_temperature,
                                "seed": config.stage_2_6_seed,
                                "reasoning_effort": config.stage_2_6_reasoning_effort,
                                "prompt_version": config.stage_2_6_prompt_version,
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
                        "Adjudicated %d/%d for %s: %s",
                        position,
                        len(records),
                        record["source_id"],
                        adjudication["decision"],
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
                        config.stage_2_6_max_retries,
                        record["source_id"],
                        last_error,
                    )
                    if attempt_number < config.stage_2_6_max_retries:
                        time.sleep(min(2 ** (attempt_number - 1), 4))
            else:
                failure = {
                    "stage": "strict_schema_adjudication",
                    "source_id": record["source_id"],
                    "canonical_fallacy": record["canonical_fallacy"],
                    "attempts": config.stage_2_6_max_retries,
                    "error": last_error,
                    "timestamp": utc_now(),
                }
                failures.append(failure)
                _append_jsonl(failures_path, failure)

        _write_json(run_dir / "adjudicated_schemas.json", results)
        write_review(run_dir / "stage_2_6_review.md", results, len(failures))
        decisions = Counter(result["adjudication"]["decision"] for result in results)
        manifest = {
            "run_id": actual_run_id,
            "stage": 2.6,
            "stage_name": "strict_schema_adjudication",
            "created_at": utc_now(),
            "input": {
                "path": str(input_path.resolve()),
                "sha256": sha256_file(input_path),
                "record_count": len(records),
            },
            "adjudication": {
                "provider": "OpenRouter",
                "requested_model": config.stage_2_6_model,
                "temperature": config.stage_2_6_temperature,
                "seed": config.stage_2_6_seed,
                "reasoning_effort": config.stage_2_6_reasoning_effort,
                "max_tokens": config.stage_2_6_max_tokens,
                "max_attempts_per_record": config.stage_2_6_max_retries,
                "prompt_version": config.stage_2_6_prompt_version,
                "prompt_path": str(config.stage_2_6_prompt_path.resolve()),
                "prompt_sha256": prompt_sha256,
            },
            "total_record_count": len(records),
            "completed_adjudication_count": len(results),
            "accepted_count": decisions["ACCEPT"],
            "revised_count": decisions["REVISE"],
            "rejected_count": decisions["REJECT"],
            "failure_count": len(failures),
            "raw_attempt_count": len(raw_attempts),
            "usage_totals": usage_totals(raw_attempts),
            "outputs": {
                "adjudicated_schemas": "adjudicated_schemas.json",
                "review": "stage_2_6_review.md",
                "raw_responses": "raw_responses.jsonl",
                "failures": "stage_2_6_failures.jsonl",
            },
        }
        _write_json(run_dir / "stage_2_6_manifest.json", manifest)
    except Exception as exc:
        _write_json(
            run_dir / "run_error.json",
            {
                "run_id": actual_run_id,
                "stage": 2.6,
                "stage_name": "strict_schema_adjudication",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        LOGGER.exception("Stage 2.6 failed; incomplete run retained at %s", run_dir)
        raise

    LOGGER.info(
        "Stage 2.6 completed with %d outcomes and %d failures in %s",
        len(results),
        len(failures),
        run_dir,
    )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(description="Strictly adjudicate Stage 2.5 schemas.")
    parser.add_argument("--input", type=Path, required=True, help="Stage 2.5 validated_schemas.json")
    parser.add_argument("--outputs-dir", type=Path, default=defaults.outputs_dir)
    parser.add_argument("--run-id", help="Optional immutable run ID")
    parser.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", defaults.stage_2_6_model))
    parser.add_argument("--temperature", type=float, default=defaults.stage_2_6_temperature)
    parser.add_argument("--seed", type=int, default=defaults.stage_2_6_seed)
    parser.add_argument(
        "--reasoning-effort", choices=("none", "minimal", "low", "medium", "high"),
        default=defaults.stage_2_6_reasoning_effort,
    )
    parser.add_argument("--max-retries", type=int, default=defaults.stage_2_6_max_retries)
    parser.add_argument("--max-tokens", type=int, default=defaults.stage_2_6_max_tokens)
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("OPENROUTER_BASE_URL", defaults.openrouter_api_base_url),
    )
    parser.add_argument("--request-timeout", type=float, default=90.0)
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
        stage_2_6_model=args.model,
        stage_2_6_temperature=args.temperature,
        stage_2_6_seed=args.seed,
        stage_2_6_reasoning_effort=args.reasoning_effort,
        stage_2_6_max_retries=args.max_retries,
        stage_2_6_max_tokens=args.max_tokens,
        openrouter_api_base_url=args.api_base_url,
    )
    provider = OpenRouterProvider(
        api_key=api_key,
        base_url=config.openrouter_api_base_url,
        timeout_seconds=args.request_timeout,
    )
    try:
        run_dir = run_stage_2_6(
            config,
            input_path=args.input.resolve(),
            provider=provider,
            run_id=args.run_id,
        )
    except Exception as exc:
        LOGGER.error("Stage 2.6 did not complete: %s", exc)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

