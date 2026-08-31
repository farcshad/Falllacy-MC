"""Stage 3.5: semantic validation and targeted refinement of Stage 3 cases."""

from __future__ import annotations

import argparse
import hashlib
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
from data_creation.extract_schema import validate_schema_response
from data_creation.generate_case import CASE_FIELDS
from data_creation.llm_provider import LLMResponse, OpenRouterProvider, ProviderError, load_dotenv
from data_creation.load_source import create_run_dir


LOGGER = logging.getLogger(__name__)
DECISIONS = ("ACCEPT", "REVISE", "REJECT")
CHECK_FIELDS = (
    "schema_faithful",
    "student_reasoning_plausible",
    "answer_consistent",
    "misconception_valid",
    "misconception_generalized",
    "internally_consistent",
)
VALIDATION_FIELDS = ("decision", "checks", "issues", "revised_case")
CASE_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {field: {"type": "string", "minLength": 1} for field in CASE_FIELDS},
    "required": list(CASE_FIELDS),
    "additionalProperties": False,
}
VALIDATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "checks": {
            "type": "object",
            "properties": {field: {"type": "boolean"} for field in CHECK_FIELDS},
            "required": list(CHECK_FIELDS),
            "additionalProperties": False,
        },
        "issues": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "revised_case": {"anyOf": [CASE_OBJECT_SCHEMA, {"type": "null"}]},
    },
    "required": list(VALIDATION_FIELDS),
    "additionalProperties": False,
}

META_MISCONCEPTION_PREFIX = re.compile(
    r"^(?:the\s+)?(?:student|students|learner|learners|pupil|pupils)\s+"
    r"(?:may\s+|might\s+|can\s+)?(?:believe|believes|think|thinks|assume|assumes|"
    r"mistakenly\s+believe|incorrectly\s+believe)\b",
    re.IGNORECASE,
)


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


def _clean_case(value: Any, *, context: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(CASE_FIELDS):
        raise ValueError(f"{context} must contain exactly {CASE_FIELDS}.")
    cleaned: dict[str, str] = {}
    for field in CASE_FIELDS:
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"{context} field {field!r} must be a non-empty string.")
        cleaned[field] = value[field].strip()
    return cleaned


def load_generated_cases(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stage 3 input is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("Stage 3 input must be a JSON array.")

    required = ("id", "source_schema_id", "domain", "fallacy", "fallacy_schema")
    seen: set[str] = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict) or any(field not in record for field in required):
            raise ValueError(f"Stage 3 record {index} is missing required fields.")
        case_id = str(record["id"])
        if case_id in seen:
            raise ValueError(f"Duplicate Stage 3 case ID: {case_id!r}.")
        seen.add(case_id)
        _clean_case({field: record.get(field) for field in CASE_FIELDS}, context=f"Case {case_id}")
        validate_schema_response(json.dumps(record["fallacy_schema"]))
    if not value:
        raise ValueError("Stage 3 input contains no cases.")
    return value


def render_validation_prompt(template: str, record: dict[str, Any]) -> str:
    current_case = {field: record[field] for field in CASE_FIELDS}
    replacements = {
        "{{CASE_ID}}": str(record["id"]),
        "{{FALLACY}}": str(record["fallacy"]),
        "{{DOMAIN}}": str(record["domain"]),
        "{{FALLACY_SCHEMA}}": json.dumps(record["fallacy_schema"], indent=2, ensure_ascii=False),
        "{{CURRENT_CASE}}": json.dumps(current_case, indent=2, ensure_ascii=False),
    }
    prompt = template
    for marker, replacement in replacements.items():
        prompt = prompt.replace(marker, replacement)
    if "{{" in prompt or "}}" in prompt:
        raise ValueError("The case-validation prompt has an unknown or unreplaced placeholder.")
    return prompt


def misconception_is_declarative(text: str, fallacy: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    normalized_fallacy = re.sub(r"[^a-z0-9]+", " ", fallacy.casefold()).strip()
    return not META_MISCONCEPTION_PREFIX.match(text.strip()) and normalized != normalized_fallacy


def validate_validation_response(
    raw_content: str,
    current_case: dict[str, str],
    fallacy: str,
) -> dict[str, Any]:
    """Validate JSON shape and decision-specific invariants."""

    try:
        value = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != set(VALIDATION_FIELDS):
        raise ValueError(f"Validation must contain exactly {VALIDATION_FIELDS}.")
    decision = value["decision"]
    if decision not in DECISIONS:
        raise ValueError(f"Unknown validation decision: {decision!r}.")
    checks = value["checks"]
    if not isinstance(checks, dict) or set(checks) != set(CHECK_FIELDS):
        raise ValueError(f"Checks must contain exactly {CHECK_FIELDS}.")
    if any(not isinstance(checks[field], bool) for field in CHECK_FIELDS):
        raise ValueError("Every validation check must be a boolean.")
    issues = value["issues"]
    if not isinstance(issues, list) or any(
        not isinstance(issue, str) or not issue.strip() for issue in issues
    ):
        raise ValueError("Issues must be an array of non-empty strings.")
    cleaned_issues = [issue.strip() for issue in issues]

    if decision == "ACCEPT":
        if not all(checks.values()):
            raise ValueError("ACCEPT requires all six checks to be true.")
        if cleaned_issues:
            raise ValueError("ACCEPT requires an empty issues array.")
        if value["revised_case"] is not None:
            raise ValueError("ACCEPT requires revised_case=null.")
        if not misconception_is_declarative(current_case["misconception"], fallacy):
            raise ValueError(
                "ACCEPT cannot retain a meta-framed misconception or a bare fallacy label."
            )
        revised_case = None
    elif decision == "REVISE":
        if all(checks.values()):
            raise ValueError("REVISE requires at least one failed check on the current case.")
        if not cleaned_issues:
            raise ValueError("REVISE requires at least one issue.")
        revised_case = _clean_case(value["revised_case"], context="revised_case")
        if revised_case == current_case:
            raise ValueError("REVISE must actually change the case.")
        if not misconception_is_declarative(revised_case["misconception"], fallacy):
            raise ValueError(
                "A revised misconception must be declarative and cannot be a bare fallacy label."
            )
    else:
        if all(checks.values()):
            raise ValueError("REJECT requires at least one failed check.")
        if not cleaned_issues:
            raise ValueError("REJECT requires at least one issue.")
        if value["revised_case"] is not None:
            raise ValueError("REJECT requires revised_case=null.")
        revised_case = None

    return {
        "decision": decision,
        "checks": {field: checks[field] for field in CHECK_FIELDS},
        "issues": cleaned_issues,
        "revised_case": revised_case,
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
            for key, amount in usage.items():
                if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                    totals[key] += amount
    return dict(totals)


def _final_text(final_case: dict[str, str] | None, field: str) -> str:
    return final_case[field] if final_case is not None else "Rejected; no final case."


def write_review(path: Path, records: list[dict[str, Any]], failure_count: int) -> None:
    decisions = Counter(record["validation"]["decision"] for record in records)
    failed_checks: Counter[str] = Counter()
    for record in records:
        if record["validation"]["decision"] in {"REVISE", "REJECT"}:
            failed_checks.update(
                field for field, passed in record["validation"]["checks"].items() if not passed
            )
    sections = [
        "# Stage 3.5 — Semantic Case Validation and Refinement",
        "",
        f"- Total: {len(records) + failure_count}",
        f"- Accepted unchanged: {decisions['ACCEPT']}",
        f"- Revised: {decisions['REVISE']}",
        f"- Rejected: {decisions['REJECT']}",
        f"- Failures: {failure_count}",
        "",
        "Rejection is a valid semantic decision, not a pipeline failure.",
        "",
        "## Most common rejection/revision reasons",
        "",
    ]
    if failed_checks:
        for check, count in failed_checks.most_common():
            sections.append(f"- `{check}` failed in {count} revised/rejected case(s).")
    else:
        sections.append("- None.")

    for index, record in enumerate(records, start=1):
        original = record["original_case"]
        validation = record["validation"]
        final_case = record["final_case"]
        issue_text = "; ".join(validation["issues"]) or "None."
        sections.extend(
            [
                "",
                f"## {index}. `{record['id']}`",
                "",
                f"**Domain:** {record['domain']}  ",
                f"**Fallacy:** {record['fallacy']}",
                "",
                f"**Original situation:** {original['situation']}",
                "",
                f"**Original question:** {original['question']}",
                "",
                f"**Original reasoning:** {original['student_reasoning']}",
                "",
                f"**Original answer:** {original['student_answer']}",
                "",
                f"**Original misconception:** {original['misconception']}",
                "",
                f"**Decision:** {validation['decision']}",
                "",
                f"**Issues:** {issue_text}",
                "",
                f"**Final situation:** {_final_text(final_case, 'situation')}",
                "",
                f"**Final question:** {_final_text(final_case, 'question')}",
                "",
                f"**Final reasoning:** {_final_text(final_case, 'student_reasoning')}",
                "",
                f"**Final answer:** {_final_text(final_case, 'student_answer')}",
                "",
                f"**Final misconception:** {_final_text(final_case, 'misconception')}",
            ]
        )
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def run_stage_3_5(
    config: PipelineConfig,
    *,
    input_path: Path,
    provider: JSONGenerator,
    run_id: str | None = None,
) -> Path:
    actual_run_id, run_dir = create_run_dir(config.outputs_dir, run_id)
    raw_path = run_dir / "raw_validation_responses.jsonl"
    failures_path = run_dir / "stage_3_5_failures.jsonl"
    raw_attempts: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    try:
        records = load_generated_cases(input_path)
        template = config.stage_3_5_prompt_path.read_text(encoding="utf-8")
        prompt_sha256 = sha256_file(config.stage_3_5_prompt_path)
        raw_path.touch()
        failures_path.touch()

        for position, record in enumerate(records, start=1):
            original_case = _clean_case(
                {field: record[field] for field in CASE_FIELDS}, context=str(record["id"])
            )
            base_prompt = render_validation_prompt(template, record)
            last_error = "No attempt was made."

            for attempt_number in range(1, config.stage_3_5_max_retries + 1):
                prompt = base_prompt
                if attempt_number > 1:
                    prompt += (
                        "\n\nRetry correction: The prior response violated the output contract: "
                        f"{last_error} Return only a contract-compliant JSON object."
                    )
                attempt_record: dict[str, Any] = {
                    "case_id": record["id"],
                    "source_schema_id": record["source_schema_id"],
                    "fallacy": record["fallacy"],
                    "attempt": attempt_number,
                    "requested_model": config.stage_3_5_model,
                    "timestamp": utc_now(),
                }
                try:
                    response = provider.generate_json(
                        prompt=prompt,
                        model=config.stage_3_5_model,
                        temperature=config.stage_3_5_temperature,
                        seed=config.stage_3_5_seed + position,
                        max_tokens=config.stage_3_5_max_tokens,
                        json_schema=VALIDATION_JSON_SCHEMA,
                        reasoning_effort=config.stage_3_5_reasoning_effort,
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
                    validation = validate_validation_response(
                        response.content, original_case, str(record["fallacy"])
                    )
                    decision = validation["decision"]
                    final_case = (
                        original_case
                        if decision == "ACCEPT"
                        else validation["revised_case"] if decision == "REVISE" else None
                    )
                    attempt_record["structurally_valid"] = True
                    raw_attempts.append(attempt_record)
                    _append_jsonl(raw_path, attempt_record)
                    results.append(
                        {
                            "id": record["id"],
                            "source_schema_id": record["source_schema_id"],
                            "source_schema_decision": record.get("source_schema_decision"),
                            "domain": record["domain"],
                            "fallacy": record["fallacy"],
                            "original_fallacy": record.get("original_fallacy"),
                            "fallacy_schema": record["fallacy_schema"],
                            "original_case": original_case,
                            "stage_3_generation_metadata": record.get("generation_metadata"),
                            "validation": {
                                "decision": decision,
                                "checks": validation["checks"],
                                "issues": validation["issues"],
                            },
                            "final_case": final_case,
                            "validation_metadata": {
                                "model": response.model or config.stage_3_5_model,
                                "requested_model": config.stage_3_5_model,
                                "temperature": config.stage_3_5_temperature,
                                "seed": config.stage_3_5_seed + position,
                                "reasoning_effort": config.stage_3_5_reasoning_effort,
                                "prompt_version": config.stage_3_5_prompt_version,
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
                        "Validated %d/%d: %s -> %s",
                        position,
                        len(records),
                        record["id"],
                        decision,
                    )
                    break
                except (ProviderError, ValueError) as exc:
                    last_error = str(exc)
                    attempt_record.update({"structurally_valid": False, "error": last_error})
                    raw_attempts.append(attempt_record)
                    _append_jsonl(raw_path, attempt_record)
                    LOGGER.warning(
                        "Attempt %d/%d failed for %s: %s",
                        attempt_number,
                        config.stage_3_5_max_retries,
                        record["id"],
                        last_error,
                    )
                    if attempt_number < config.stage_3_5_max_retries:
                        time.sleep(min(2 ** (attempt_number - 1), 4))
            else:
                failure = {
                    "stage": "semantic_case_validation_and_refinement",
                    "case_id": record["id"],
                    "source_schema_id": record["source_schema_id"],
                    "fallacy": record["fallacy"],
                    "attempts": config.stage_3_5_max_retries,
                    "error": last_error,
                    "timestamp": utc_now(),
                }
                failures.append(failure)
                _append_jsonl(failures_path, failure)

        decisions = Counter(record["validation"]["decision"] for record in results)
        _write_json(run_dir / "validated_cases.json", results)
        _write_json(
            run_dir / "accepted_cases.json",
            [record for record in results if record["validation"]["decision"] == "ACCEPT"],
        )
        _write_json(
            run_dir / "revised_cases.json",
            [record for record in results if record["validation"]["decision"] == "REVISE"],
        )
        _write_json(
            run_dir / "rejected_cases.json",
            [record for record in results if record["validation"]["decision"] == "REJECT"],
        )
        write_review(run_dir / "stage_3_5_review.md", results, len(failures))
        manifest = {
            "run_id": actual_run_id,
            "stage": 3.5,
            "stage_name": "semantic_case_validation_and_refinement",
            "created_at": utc_now(),
            "input": {
                "path": str(input_path.resolve()),
                "sha256": sha256_file(input_path),
                "record_count": len(records),
            },
            "validation": {
                "provider": "OpenRouter",
                "requested_model": config.stage_3_5_model,
                "temperature": config.stage_3_5_temperature,
                "base_seed": config.stage_3_5_seed,
                "seed_strategy": "base_seed + one-based input position",
                "reasoning_effort": config.stage_3_5_reasoning_effort,
                "max_tokens": config.stage_3_5_max_tokens,
                "max_attempts_per_case": config.stage_3_5_max_retries,
                "prompt_version": config.stage_3_5_prompt_version,
                "prompt_path": str(config.stage_3_5_prompt_path.resolve()),
                "prompt_sha256": prompt_sha256,
            },
            "total_record_count": len(records),
            "completed_validation_count": len(results),
            "accepted_unchanged_count": decisions["ACCEPT"],
            "revised_count": decisions["REVISE"],
            "rejected_count": decisions["REJECT"],
            "failure_count": len(failures),
            "raw_attempt_count": len(raw_attempts),
            "retry_count": len(raw_attempts) - len(results) - len(failures),
            "usage_totals": usage_totals(raw_attempts),
            "outputs": {
                "validated_cases": "validated_cases.json",
                "accepted_cases": "accepted_cases.json",
                "revised_cases": "revised_cases.json",
                "rejected_cases": "rejected_cases.json",
                "raw_validation_responses": "raw_validation_responses.jsonl",
                "failures": "stage_3_5_failures.jsonl",
                "review": "stage_3_5_review.md",
            },
        }
        _write_json(run_dir / "stage_3_5_manifest.json", manifest)
    except Exception as exc:
        _write_json(
            run_dir / "run_error.json",
            {
                "run_id": actual_run_id,
                "stage": 3.5,
                "stage_name": "semantic_case_validation_and_refinement",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        LOGGER.exception("Stage 3.5 failed; incomplete run retained at %s", run_dir)
        raise

    LOGGER.info(
        "Stage 3.5 completed with %d decisions and %d failures in %s",
        len(results),
        len(failures),
        run_dir,
    )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(description="Validate and refine Stage 3 cases.")
    parser.add_argument("--input", type=Path, required=True, help="Stage 3 generated_cases.json")
    parser.add_argument("--outputs-dir", type=Path, default=defaults.outputs_dir)
    parser.add_argument("--run-id", help="Optional immutable run ID")
    parser.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", defaults.stage_3_5_model))
    parser.add_argument("--temperature", type=float, default=defaults.stage_3_5_temperature)
    parser.add_argument("--seed", type=int, default=defaults.stage_3_5_seed)
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high"),
        default=defaults.stage_3_5_reasoning_effort,
    )
    parser.add_argument("--max-retries", type=int, default=defaults.stage_3_5_max_retries)
    parser.add_argument("--max-tokens", type=int, default=defaults.stage_3_5_max_tokens)
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
    config = PipelineConfig(
        outputs_dir=args.outputs_dir.resolve(),
        stage_3_5_model=args.model,
        stage_3_5_temperature=args.temperature,
        stage_3_5_seed=args.seed,
        stage_3_5_reasoning_effort=args.reasoning_effort,
        stage_3_5_max_retries=args.max_retries,
        stage_3_5_max_tokens=args.max_tokens,
        openrouter_api_base_url=args.api_base_url,
    )
    provider = OpenRouterProvider(
        api_key=api_key,
        base_url=config.openrouter_api_base_url,
        timeout_seconds=args.request_timeout,
    )
    try:
        run_dir = run_stage_3_5(
            config,
            input_path=args.input.resolve(),
            provider=provider,
            run_id=args.run_id,
        )
    except Exception as exc:
        LOGGER.error("Stage 3.5 did not complete: %s", exc)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
