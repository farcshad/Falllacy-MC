"""Stage 3: generate educational misconception cases from adjudicated schemas."""

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
from data_creation.llm_provider import LLMResponse, OpenRouterProvider, ProviderError, load_dotenv
from data_creation.load_source import create_run_dir


LOGGER = logging.getLogger(__name__)
CASE_FIELDS = (
    "situation",
    "question",
    "student_reasoning",
    "student_answer",
    "misconception",
)
CASE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {field: {"type": "string", "minLength": 1} for field in CASE_FIELDS},
    "required": list(CASE_FIELDS),
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


def load_usable_schemas(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Load Stage 2.6 output and exclude REJECT decisions."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stage 2.6 input is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("Stage 2.6 input must be a JSON array.")
    usable: list[dict[str, Any]] = []
    rejected_count = 0
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict):
            raise ValueError(f"Stage 2.6 record {index} is not an object.")
        required = (
            "source_id",
            "canonical_fallacy",
            "original_fallacy",
            "adjudication",
            "final_schema",
        )
        if any(key not in record for key in required):
            raise ValueError(f"Stage 2.6 record {index} is missing required fields.")
        decision = record["adjudication"].get("decision")
        if decision == "REJECT":
            if record["final_schema"] is not None:
                raise ValueError(f"Rejected record {index} unexpectedly has a final schema.")
            rejected_count += 1
            continue
        if decision not in {"ACCEPT", "REVISE"}:
            raise ValueError(f"Stage 2.6 record {index} has unknown decision {decision!r}.")
        schema = validate_schema_response(json.dumps(record["final_schema"]))
        key = (str(record["source_id"]), str(record["canonical_fallacy"]))
        if key in seen:
            raise ValueError(f"Duplicate usable source/fallacy pair: {key!r}.")
        seen.add(key)
        usable.append(
            {
                "source_id": str(record["source_id"]),
                "canonical_fallacy": str(record["canonical_fallacy"]),
                "original_fallacy": str(record["original_fallacy"]),
                "adjudication_decision": decision,
                "final_schema": schema,
            }
        )
    if not usable:
        raise ValueError("Stage 2.6 input contains no ACCEPT or REVISE schemas.")
    return usable, rejected_count


def choose_domains(
    fallacy: str,
    *,
    available_domains: tuple[str, ...],
    preferences: dict[str, tuple[str, ...]],
    count: int,
) -> tuple[str, ...]:
    if count <= 0:
        raise ValueError("domains_per_schema must be positive.")
    ordered = [
        domain for domain in preferences.get(fallacy, ()) if domain in available_domains
    ]
    ordered.extend(domain for domain in available_domains if domain not in ordered)
    if len(ordered) < count:
        raise ValueError(
            f"Requested {count} domains for {fallacy!r}, but only {len(ordered)} are available."
        )
    return tuple(ordered[:count])


def build_generation_plan(
    schemas: list[dict[str, Any]], config: PipelineConfig
) -> list[dict[str, Any]]:
    if config.stage_3_cases_per_domain <= 0:
        raise ValueError("cases_per_domain must be positive.")
    plan: list[dict[str, Any]] = []
    slot = 0
    for schema in schemas:
        domains = choose_domains(
            schema["canonical_fallacy"],
            available_domains=config.educational_domains,
            preferences=config.stage_3_domain_preferences,
            count=config.stage_3_domains_per_schema,
        )
        for domain in domains:
            for variation in range(1, config.stage_3_cases_per_domain + 1):
                slot += 1
                plan.append(
                    {
                        "case_id": f"case_{slot:04d}",
                        "schema": schema,
                        "domain": domain,
                        "variation": variation,
                    }
                )
    return plan


def render_case_prompt(
    template: str,
    *,
    schema: dict[str, Any],
    domain: str,
    variation: int,
    variations_total: int,
    prior_cases: list[dict[str, str]],
) -> str:
    if prior_cases:
        novelty_context = (
            "Previously accepted case(s) for this schema and domain are shown below. "
            "Do not paraphrase or reuse their topic, entities, question, answer, or misconception:\n"
            + json.dumps(prior_cases, indent=2, ensure_ascii=False)
        )
    else:
        novelty_context = "No earlier case exists for this schema-domain pair."
    replacements = {
        "{{FALLACY}}": schema["canonical_fallacy"],
        "{{FALLACY_SCHEMA}}": json.dumps(schema["final_schema"], indent=2, ensure_ascii=False),
        "{{DOMAIN}}": domain,
        "{{VARIATION_NUMBER}}": str(variation),
        "{{VARIATIONS_TOTAL}}": str(variations_total),
        "{{NOVELTY_CONTEXT}}": novelty_context,
    }
    prompt = template
    for marker, value in replacements.items():
        prompt = prompt.replace(marker, value)
    if "{{" in prompt or "}}" in prompt:
        raise ValueError("The case-generation prompt has an unknown or unreplaced placeholder.")
    return prompt


def validate_case_response(raw_content: str) -> dict[str, str]:
    try:
        value = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != set(CASE_FIELDS):
        raise ValueError(f"Case response must contain exactly {CASE_FIELDS}.")
    for field in CASE_FIELDS:
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"Case field {field!r} must be a non-empty string.")
    return {field: value[field].strip() for field in CASE_FIELDS}


def case_fingerprint(case: dict[str, str]) -> str:
    normalized = [
        re.sub(r"\s+", " ", case[field]).strip().casefold() for field in CASE_FIELDS
    ]
    return hashlib.sha256("\x1f".join(normalized).encode("utf-8")).hexdigest()


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


def write_review(path: Path, cases: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    sections = [
        "# Stage 3 — Educational Misconception Case Pilot",
        "",
        f"- Total requested: {summary['requested']}",
        f"- Successfully generated: {summary['successful']}",
        f"- Structural failures: {summary['failures']}",
        f"- Retries: {summary['retries']}",
        f"- Duplicate attempts: {summary['duplicate_attempts']}",
        f"- Final exact duplicates: {summary['final_duplicates']}",
    ]
    for case in cases:
        sections.extend(
            [
                "",
                f"## `{case['id']}`",
                "",
                f"**Schema source:** `{case['source_schema_id']}`  ",
                f"**Domain:** {case['domain']}  ",
                f"**Fallacy:** {case['fallacy']}",
                "",
                f"**Situation:** {case['situation']}",
                "",
                f"**Question:** {case['question']}",
                "",
                f"**Student reasoning:** {case['student_reasoning']}",
                "",
                f"**Student answer:** {case['student_answer']}",
                "",
                f"**Misconception:** {case['misconception']}",
            ]
        )
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def run_stage_3(
    config: PipelineConfig,
    *,
    input_path: Path,
    provider: JSONGenerator,
    run_id: str | None = None,
) -> Path:
    actual_run_id, run_dir = create_run_dir(config.outputs_dir, run_id)
    raw_path = run_dir / "raw_responses.jsonl"
    failures_path = run_dir / "stage_3_failures.jsonl"
    raw_attempts: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    prior_by_pair: dict[tuple[str, str], list[dict[str, str]]] = {}
    duplicate_attempt_count = 0

    try:
        schemas, rejected_input_count = load_usable_schemas(input_path)
        plan = build_generation_plan(schemas, config)
        template = config.stage_3_prompt_path.read_text(encoding="utf-8")
        prompt_sha256 = sha256_file(config.stage_3_prompt_path)
        _write_json(run_dir / "generation_plan.json", plan)
        raw_path.touch()
        failures_path.touch()

        for position, item in enumerate(plan, start=1):
            schema = item["schema"]
            pair_key = (schema["source_id"], item["domain"])
            prior_cases = prior_by_pair.get(pair_key, [])
            base_prompt = render_case_prompt(
                template,
                schema=schema,
                domain=item["domain"],
                variation=item["variation"],
                variations_total=config.stage_3_cases_per_domain,
                prior_cases=prior_cases,
            )
            last_error = "No attempt was made."

            for attempt_number in range(1, config.stage_3_max_retries + 1):
                prompt = base_prompt
                if attempt_number > 1:
                    prompt += (
                        "\n\nRetry correction: The previous response failed structural validation: "
                        f"{last_error} Return a new, contract-compliant case."
                    )
                attempt_record: dict[str, Any] = {
                    "case_id": item["case_id"],
                    "source_schema_id": schema["source_id"],
                    "domain": item["domain"],
                    "fallacy": schema["canonical_fallacy"],
                    "variation": item["variation"],
                    "attempt": attempt_number,
                    "requested_model": config.stage_3_model,
                    "timestamp": utc_now(),
                }
                try:
                    response = provider.generate_json(
                        prompt=prompt,
                        model=config.stage_3_model,
                        temperature=config.stage_3_temperature,
                        seed=config.stage_3_seed + position,
                        max_tokens=config.stage_3_max_tokens,
                        json_schema=CASE_JSON_SCHEMA,
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
                    generated = validate_case_response(response.content)
                    fingerprint = case_fingerprint(generated)
                    if fingerprint in fingerprints:
                        duplicate_attempt_count += 1
                        raise ValueError("Exact duplicate of a previously accepted generated case.")
                    attempt_record["structurally_valid"] = True
                    raw_attempts.append(attempt_record)
                    _append_jsonl(raw_path, attempt_record)
                    fingerprints.add(fingerprint)
                    prior_by_pair.setdefault(pair_key, []).append(generated)
                    cases.append(
                        {
                            "id": item["case_id"],
                            "source_schema_id": schema["source_id"],
                            "source_schema_decision": schema["adjudication_decision"],
                            "domain": item["domain"],
                            "fallacy": schema["canonical_fallacy"],
                            "original_fallacy": schema["original_fallacy"],
                            "fallacy_schema": schema["final_schema"],
                            **generated,
                            "generation_metadata": {
                                "model": response.model or config.stage_3_model,
                                "requested_model": config.stage_3_model,
                                "temperature": config.stage_3_temperature,
                                "seed": config.stage_3_seed + position,
                                "prompt_version": config.stage_3_prompt_version,
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
                        "Generated case %d/%d: %s (%s, %s)",
                        position,
                        len(plan),
                        item["case_id"],
                        schema["source_id"],
                        item["domain"],
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
                        config.stage_3_max_retries,
                        item["case_id"],
                        last_error,
                    )
                    if attempt_number < config.stage_3_max_retries:
                        time.sleep(min(2 ** (attempt_number - 1), 4))
            else:
                failure = {
                    "stage": "generate_educational_case",
                    "case_id": item["case_id"],
                    "source_schema_id": schema["source_id"],
                    "domain": item["domain"],
                    "fallacy": schema["canonical_fallacy"],
                    "attempts": config.stage_3_max_retries,
                    "error": last_error,
                    "timestamp": utc_now(),
                }
                failures.append(failure)
                _append_jsonl(failures_path, failure)

        final_duplicate_count = len(cases) - len(
            {case_fingerprint({field: case[field] for field in CASE_FIELDS}) for case in cases}
        )
        retry_count = len(raw_attempts) - len(cases)
        _write_json(run_dir / "generated_cases.json", cases)
        summary = {
            "requested": len(plan),
            "successful": len(cases),
            "failures": len(failures),
            "retries": retry_count,
            "duplicate_attempts": duplicate_attempt_count,
            "final_duplicates": final_duplicate_count,
        }
        write_review(run_dir / "stage_3_review.md", cases, summary)
        domain_assignments = {
            schema["source_id"]: list(
                choose_domains(
                    schema["canonical_fallacy"],
                    available_domains=config.educational_domains,
                    preferences=config.stage_3_domain_preferences,
                    count=config.stage_3_domains_per_schema,
                )
            )
            for schema in schemas
        }
        manifest = {
            "run_id": actual_run_id,
            "stage": 3,
            "stage_name": "educational_misconception_case_generation",
            "created_at": utc_now(),
            "input": {
                "path": str(input_path.resolve()),
                "sha256": sha256_file(input_path),
                "usable_schema_count": len(schemas),
                "ignored_rejected_schema_count": rejected_input_count,
                "usable_source_ids": [schema["source_id"] for schema in schemas],
            },
            "pilot_design": {
                "available_domains": list(config.educational_domains),
                "domains_per_schema": config.stage_3_domains_per_schema,
                "cases_per_domain": config.stage_3_cases_per_domain,
                "domain_assignments": domain_assignments,
            },
            "generation": {
                "provider": "OpenRouter",
                "requested_model": config.stage_3_model,
                "temperature": config.stage_3_temperature,
                "base_seed": config.stage_3_seed,
                "seed_strategy": "base_seed + one-based planned case position",
                "max_tokens": config.stage_3_max_tokens,
                "max_attempts_per_case": config.stage_3_max_retries,
                "prompt_version": config.stage_3_prompt_version,
                "prompt_path": str(config.stage_3_prompt_path.resolve()),
                "prompt_sha256": prompt_sha256,
            },
            "total_requested": len(plan),
            "successfully_generated": len(cases),
            "structural_failure_count": len(failures),
            "retry_count": retry_count,
            "duplicate_attempt_count": duplicate_attempt_count,
            "final_exact_duplicate_count": final_duplicate_count,
            "raw_attempt_count": len(raw_attempts),
            "usage_totals": usage_totals(raw_attempts),
            "outputs": {
                "generated_cases": "generated_cases.json",
                "raw_responses": "raw_responses.jsonl",
                "failures": "stage_3_failures.jsonl",
                "review": "stage_3_review.md",
                "generation_plan": "generation_plan.json",
            },
        }
        _write_json(run_dir / "stage_3_manifest.json", manifest)
    except Exception as exc:
        _write_json(
            run_dir / "run_error.json",
            {
                "run_id": actual_run_id,
                "stage": 3,
                "stage_name": "educational_misconception_case_generation",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        LOGGER.exception("Stage 3 failed; incomplete run retained at %s", run_dir)
        raise

    LOGGER.info(
        "Stage 3 completed with %d cases and %d failures in %s",
        len(cases),
        len(failures),
        run_dir,
    )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(description="Generate educational misconception cases.")
    parser.add_argument("--input", type=Path, required=True, help="Stage 2.6 adjudicated_schemas.json")
    parser.add_argument("--outputs-dir", type=Path, default=defaults.outputs_dir)
    parser.add_argument("--run-id", help="Optional immutable run ID")
    parser.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", defaults.stage_3_model))
    parser.add_argument("--temperature", type=float, default=defaults.stage_3_temperature)
    parser.add_argument("--seed", type=int, default=defaults.stage_3_seed)
    parser.add_argument("--domains-per-schema", type=int, default=defaults.stage_3_domains_per_schema)
    parser.add_argument("--cases-per-domain", type=int, default=defaults.stage_3_cases_per_domain)
    parser.add_argument("--max-retries", type=int, default=defaults.stage_3_max_retries)
    parser.add_argument("--max-tokens", type=int, default=defaults.stage_3_max_tokens)
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
        stage_3_model=args.model,
        stage_3_temperature=args.temperature,
        stage_3_seed=args.seed,
        stage_3_domains_per_schema=args.domains_per_schema,
        stage_3_cases_per_domain=args.cases_per_domain,
        stage_3_max_retries=args.max_retries,
        stage_3_max_tokens=args.max_tokens,
        openrouter_api_base_url=args.api_base_url,
    )
    provider = OpenRouterProvider(
        api_key=api_key,
        base_url=config.openrouter_api_base_url,
        timeout_seconds=args.request_timeout,
    )
    try:
        run_dir = run_stage_3(
            config,
            input_path=args.input.resolve(),
            provider=provider,
            run_id=args.run_id,
        )
    except Exception as exc:
        LOGGER.error("Stage 3 did not complete: %s", exc)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
