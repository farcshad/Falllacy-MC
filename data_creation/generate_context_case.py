"""Context-first Stage 3: generate neutral prompts and seed misconception pathways."""

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
from data_creation.generate_case import choose_domains, load_usable_schemas
from data_creation.llm_provider import LLMResponse, OpenRouterProvider, ProviderError, load_dotenv
from data_creation.load_source import create_run_dir


LOGGER = logging.getLogger(__name__)
CONTEXT_CASE_FIELDS = (
    "context",
    "question",
    "possible_fallacy",
    "possible_reasoning",
    "possible_misconception",
)
CONTEXT_CASE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        field: {"type": "string", "minLength": 1} for field in CONTEXT_CASE_FIELDS
    },
    "required": list(CONTEXT_CASE_FIELDS),
    "additionalProperties": False,
}
STUDENT_TRACE_PATTERN = re.compile(
    r"\b(?:(?:a|the|some|one)\s+)?(?:student|students|learner|learners|resident|"
    r"residents|official|officials|anchor|council member|city council|government|"
    r"teacher|instructor|farmer|owner|observer|classmate|commentator|accountant|"
    r"friend|researcher)\b[^.]{0,80}\b"
    r"(?:think|thinks|thinking|thought|believe|believes|believed|believing|argue|"
    r"argues|argued|arguing|conclude|concludes|concluded|concluding|reason|reasons|"
    r"reasoned|reasoning|answer|answers|answered|answering|claim|claims|claimed|"
    r"claiming|comment|comments|commented|commenting|state|states|stated|stating|"
    r"say|says|said|saying|point|points|pointed|pointing|expect|expects|expected|"
    r"expecting|reply|replies|replied|replying|respond|responds|responded|"
    r"responding)\b(?:\s+to\b)?",
    re.IGNORECASE,
)
CRITIQUE_QUESTION_PATTERN = re.compile(
    r"\b(?:what|which|identify|name|explain|describe)\b[^?]{0,80}\b"
    r"(?:fallacy|flaw(?:ed)?|reasoning error|logical error|argument error)\b",
    re.IGNORECASE,
)
CIRCULAR_LEAK_PATTERN = re.compile(
    r"\bbecause\s+(?:it|this|that)\s+is\s+(?:the\s+|a\s+)?(?:school\s+|state\s+)?"
    r"(?:rule|law|regulation|policy|standard|principle)\b",
    re.IGNORECASE,
)
CAUSAL_LEAK_PATTERN = re.compile(
    r"\b(?:attribut(?:e|es|ed|ing)\b[^.]{0,70}\bto|"
    r"(?:report|reports|reported|reporting)\b[^.]{0,70}\bdirect result|"
    r"(?:highlight|highlights|highlighted|highlighting)\b[^.]{0,70}\b(?:reason|cause)|"
    r"correlation\s+(?:does\s+not|doesn't|is\s+not|isn't)\s+imply\s+causation)\b",
    re.IGNORECASE,
)
EVALUATOR_REASONING_PATTERN = re.compile(
    r"\b(?:the student (?:is|has)|(?:this|the) reasoning (?:assumes|ignores|is)|"
    r"this is an example|the fallacy|is flawed because|fails? to "
    r"(?:consider|understand|recognize)|without considering|ignoring (?:that|the)|"
    r"(?:do not|does not|don't|doesn't|not) consider(?:ing)?|"
    r"there (?:could|may|might) be (?:many|other)|however,?\s+other factors|"
    r"no independent evidence)\b",
    re.IGNORECASE,
)
META_MISCONCEPTION_PATTERN = re.compile(
    r"^(?:the\s+)?(?:student|students|learner|learners)\s+"
    r"(?:may\s+|might\s+|can\s+)?(?:believe|believes|think|thinks|assume|assumes|"
    r"fails?|do not understand|does not understand)\b",
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
    ) -> LLMResponse: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_context_generation_plan(
    schemas: list[dict[str, Any]], config: PipelineConfig
) -> list[dict[str, Any]]:
    if config.stage_3_context_cases_per_domain <= 0:
        raise ValueError("cases_per_domain must be positive.")
    plan: list[dict[str, Any]] = []
    slot = 0
    for schema in schemas:
        domains = choose_domains(
            schema["canonical_fallacy"],
            available_domains=config.educational_domains,
            preferences=config.stage_3_domain_preferences,
            count=config.stage_3_context_domains_per_schema,
        )
        for domain in domains:
            for variation in range(1, config.stage_3_context_cases_per_domain + 1):
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


def render_context_prompt(
    template: str,
    *,
    schema: dict[str, Any],
    domain: str,
    variation: int,
    variations_total: int,
    prior_cases: list[dict[str, str]],
) -> str:
    novelty_context = (
        "No earlier case exists for this schema-domain pair."
        if not prior_cases
        else (
            "Previously accepted case(s) for this schema and domain are below. "
            "Do not paraphrase or reuse them:\n"
            + json.dumps(prior_cases, indent=2, ensure_ascii=False)
        )
    )
    replacements = {
        "{{FALLACY}}": schema["canonical_fallacy"],
        "{{FALLACY_SCHEMA}}": json.dumps(schema["final_schema"], indent=2, ensure_ascii=False),
        "{{DOMAIN}}": domain,
        "{{VARIATION_NUMBER}}": str(variation),
        "{{VARIATIONS_TOTAL}}": str(variations_total),
        "{{NOVELTY_CONTEXT}}": novelty_context,
    }
    prompt = template
    for marker, replacement in replacements.items():
        prompt = prompt.replace(marker, replacement)
    if "{{" in prompt or "}}" in prompt:
        raise ValueError("The context-generation prompt has an unknown placeholder.")
    return prompt


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _fallacy_terms(fallacy: str) -> set[str]:
    terms = {_normalized(fallacy)}
    terms.update(_normalized(part) for part in fallacy.split("/") if part.strip())
    return {term for term in terms if term}


def validate_context_case_response(raw_content: str, expected_fallacy: str) -> dict[str, str]:
    """Validate shape plus the explicitly requested local neutrality rules."""

    try:
        value = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != set(CONTEXT_CASE_FIELDS):
        raise ValueError(f"Response must contain exactly {CONTEXT_CASE_FIELDS}.")
    cleaned: dict[str, str] = {}
    for field in CONTEXT_CASE_FIELDS:
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"Field {field!r} must be a non-empty string.")
        cleaned[field] = value[field].strip()

    if cleaned["possible_fallacy"] != expected_fallacy:
        raise ValueError(
            f"possible_fallacy must exactly match source label {expected_fallacy!r}."
        )
    learner_input = f"{cleaned['context']} {cleaned['question']}"
    learner_input_normalized = _normalized(learner_input)
    for term in _fallacy_terms(expected_fallacy):
        if re.search(rf"\b{re.escape(term)}\b", learner_input_normalized):
            raise ValueError("Context/question explicitly contains the fallacy label.")
        if re.search(rf"\b{re.escape(term)}\b", _normalized(cleaned["possible_reasoning"])):
            raise ValueError("possible_reasoning explicitly names the fallacy.")
    if STUDENT_TRACE_PATTERN.search(cleaned["context"]):
        raise ValueError("Context contains an already-written student reasoning trace.")
    if CIRCULAR_LEAK_PATTERN.search(learner_input):
        raise ValueError("Context/question contains a self-justifying circular rule claim.")
    if CAUSAL_LEAK_PATTERN.search(learner_input):
        raise ValueError("Context/question contains an explicit causal attribution or giveaway.")
    if CRITIQUE_QUESTION_PATTERN.search(cleaned["question"]):
        raise ValueError("Question asks the learner to identify or critique faulty reasoning.")
    if EVALUATOR_REASONING_PATTERN.search(cleaned["possible_reasoning"]):
        raise ValueError("possible_reasoning uses evaluator language.")
    if META_MISCONCEPTION_PATTERN.search(cleaned["possible_misconception"]):
        raise ValueError("possible_misconception must be a direct declarative belief.")

    misconception = _normalized(cleaned["possible_misconception"])
    reasoning = _normalized(cleaned["possible_reasoning"])
    if misconception and misconception in learner_input_normalized:
        raise ValueError("Context/question directly contains the generated misconception.")
    if reasoning and reasoning in learner_input_normalized:
        raise ValueError("Context/question directly contains the generated reasoning trace.")
    return cleaned


def context_case_fingerprint(case: dict[str, str]) -> str:
    normalized = [_normalized(case[field]) for field in CONTEXT_CASE_FIELDS]
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
            for key, amount in usage.items():
                if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                    totals[key] += amount
    return dict(totals)


def write_context_review(
    path: Path, cases: list[dict[str, Any]], summary: dict[str, Any]
) -> None:
    sections = [
        "# Context-First Stage 3 — Misconception Generation Pilot",
        "",
        f"- Total requested: {summary['requested']}",
        f"- Successfully generated: {summary['successful']}",
        f"- Failures: {summary['failures']}",
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
                f"**Domain:** {case['domain']}  ",
                f"**Source schema:** `{case['source_schema_id']}`  ",
                f"**Fallacy:** {case['possible_fallacy']}",
                "",
                f"**Context:** {case['context']}",
                "",
                f"**Question:** {case['question']}",
                "",
                f"**Possible reasoning:** {case['possible_reasoning']}",
                "",
                f"**Possible misconception:** {case['possible_misconception']}",
            ]
        )
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def run_context_stage_3(
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
        plan = build_context_generation_plan(schemas, config)
        template = config.stage_3_context_prompt_path.read_text(encoding="utf-8")
        prompt_sha256 = sha256_file(config.stage_3_context_prompt_path)
        _write_json(run_dir / "generation_plan.json", plan)
        raw_path.touch()
        failures_path.touch()

        for position, item in enumerate(plan, start=1):
            schema = item["schema"]
            pair_key = (schema["source_id"], item["domain"])
            base_prompt = render_context_prompt(
                template,
                schema=schema,
                domain=item["domain"],
                variation=item["variation"],
                variations_total=config.stage_3_context_cases_per_domain,
                prior_cases=prior_by_pair.get(pair_key, []),
            )
            last_error = "No attempt was made."

            for attempt_number in range(1, config.stage_3_context_max_retries + 1):
                prompt = base_prompt
                if attempt_number > 1:
                    prompt += (
                        "\n\nRetry correction: The previous response failed structural or "
                        f"neutrality validation: {last_error} Return a new compliant case."
                    )
                attempt_record: dict[str, Any] = {
                    "case_id": item["case_id"],
                    "source_schema_id": schema["source_id"],
                    "domain": item["domain"],
                    "fallacy": schema["canonical_fallacy"],
                    "variation": item["variation"],
                    "attempt": attempt_number,
                    "requested_model": config.stage_3_context_model,
                    "timestamp": utc_now(),
                }
                try:
                    response = provider.generate_json(
                        prompt=prompt,
                        model=config.stage_3_context_model,
                        temperature=config.stage_3_context_temperature,
                        seed=config.stage_3_context_seed + position,
                        max_tokens=config.stage_3_context_max_tokens,
                        json_schema=CONTEXT_CASE_JSON_SCHEMA,
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
                    generated = validate_context_case_response(
                        response.content, schema["canonical_fallacy"]
                    )
                    fingerprint = context_case_fingerprint(generated)
                    if fingerprint in fingerprints:
                        duplicate_attempt_count += 1
                        raise ValueError("Exact duplicate of a previously accepted case.")
                    attempt_record["structurally_valid"] = True
                    raw_attempts.append(attempt_record)
                    _append_jsonl(raw_path, attempt_record)
                    fingerprints.add(fingerprint)
                    prior_by_pair.setdefault(pair_key, []).append(generated)
                    cases.append(
                        {
                            "id": item["case_id"],
                            "domain": item["domain"],
                            **generated,
                            "source_schema_id": schema["source_id"],
                            "source_schema_decision": schema["adjudication_decision"],
                            "original_fallacy": schema["original_fallacy"],
                            "fallacy_schema": schema["final_schema"],
                            "generation_metadata": {
                                "research_formulation": "context_first_misconception_generation",
                                "model": response.model or config.stage_3_context_model,
                                "requested_model": config.stage_3_context_model,
                                "temperature": config.stage_3_context_temperature,
                                "seed": config.stage_3_context_seed + position,
                                "prompt_version": config.stage_3_context_prompt_version,
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
                        "Generated %d/%d: %s (%s, %s)",
                        position,
                        len(plan),
                        item["case_id"],
                        schema["source_id"],
                        item["domain"],
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
                        config.stage_3_context_max_retries,
                        item["case_id"],
                        last_error,
                    )
                    if attempt_number < config.stage_3_context_max_retries:
                        time.sleep(min(2 ** (attempt_number - 1), 4))
            else:
                failure = {
                    "stage": "context_first_misconception_generation",
                    "case_id": item["case_id"],
                    "source_schema_id": schema["source_id"],
                    "domain": item["domain"],
                    "fallacy": schema["canonical_fallacy"],
                    "attempts": config.stage_3_context_max_retries,
                    "error": last_error,
                    "timestamp": utc_now(),
                }
                failures.append(failure)
                _append_jsonl(failures_path, failure)

        final_duplicates = len(cases) - len(
            {
                context_case_fingerprint(
                    {field: case[field] for field in CONTEXT_CASE_FIELDS}
                )
                for case in cases
            }
        )
        retries = len(raw_attempts) - len(cases) - len(failures)
        _write_json(run_dir / "generated_cases.json", cases)
        summary = {
            "requested": len(plan),
            "successful": len(cases),
            "failures": len(failures),
            "retries": retries,
            "duplicate_attempts": duplicate_attempt_count,
            "final_duplicates": final_duplicates,
        }
        write_context_review(run_dir / "stage_3_review.md", cases, summary)
        domain_assignments = {
            schema["source_id"]: list(
                choose_domains(
                    schema["canonical_fallacy"],
                    available_domains=config.educational_domains,
                    preferences=config.stage_3_domain_preferences,
                    count=config.stage_3_context_domains_per_schema,
                )
            )
            for schema in schemas
        }
        manifest = {
            "run_id": actual_run_id,
            "stage": 3,
            "stage_name": "context_first_misconception_generation",
            "research_task": "(context, question) -> possible fallacy -> possible reasoning -> possible misconception",
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
                "domains_per_schema": config.stage_3_context_domains_per_schema,
                "cases_per_domain": config.stage_3_context_cases_per_domain,
                "domain_assignments": domain_assignments,
            },
            "generation": {
                "provider": "OpenRouter",
                "requested_model": config.stage_3_context_model,
                "temperature": config.stage_3_context_temperature,
                "base_seed": config.stage_3_context_seed,
                "seed_strategy": "base_seed + one-based planned case position",
                "max_tokens": config.stage_3_context_max_tokens,
                "max_attempts_per_case": config.stage_3_context_max_retries,
                "prompt_version": config.stage_3_context_prompt_version,
                "prompt_path": str(config.stage_3_context_prompt_path.resolve()),
                "prompt_sha256": prompt_sha256,
            },
            "structural_validation": {
                "required_fields_nonempty": True,
                "fallacy_must_match_source_schema": True,
                "exact_duplicate_rejection": True,
                "learner_input_fallacy_label_rejection": True,
                "learner_input_direct_misconception_rejection": True,
                "learner_input_direct_reasoning_trace_rejection": True,
                "semantic_validation_deferred": True,
            },
            "total_requested": len(plan),
            "successfully_generated": len(cases),
            "failure_count": len(failures),
            "retry_count": retries,
            "duplicate_attempt_count": duplicate_attempt_count,
            "final_exact_duplicate_count": final_duplicates,
            "raw_attempt_count": len(raw_attempts),
            "usage_totals": usage_totals(raw_attempts),
            "outputs": {
                "generated_cases": "generated_cases.json",
                "generation_plan": "generation_plan.json",
                "raw_responses": "raw_responses.jsonl",
                "failures": "stage_3_failures.jsonl",
                "review": "stage_3_review.md",
            },
        }
        _write_json(run_dir / "stage_3_manifest.json", manifest)
    except Exception as exc:
        _write_json(
            run_dir / "run_error.json",
            {
                "run_id": actual_run_id,
                "stage": 3,
                "stage_name": "context_first_misconception_generation",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        LOGGER.exception("Context-first Stage 3 failed; incomplete run retained at %s", run_dir)
        raise

    LOGGER.info(
        "Context-first Stage 3 completed with %d cases and %d failures in %s",
        len(cases),
        len(failures),
        run_dir,
    )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(description="Generate context-first misconception cases.")
    parser.add_argument("--input", type=Path, required=True, help="Stage 2.6 adjudicated_schemas.json")
    parser.add_argument("--outputs-dir", type=Path, default=defaults.outputs_dir)
    parser.add_argument("--run-id", help="Optional immutable run ID")
    parser.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", defaults.stage_3_context_model))
    parser.add_argument("--temperature", type=float, default=defaults.stage_3_context_temperature)
    parser.add_argument("--seed", type=int, default=defaults.stage_3_context_seed)
    parser.add_argument(
        "--domains-per-schema", type=int, default=defaults.stage_3_context_domains_per_schema
    )
    parser.add_argument(
        "--cases-per-domain", type=int, default=defaults.stage_3_context_cases_per_domain
    )
    parser.add_argument("--max-retries", type=int, default=defaults.stage_3_context_max_retries)
    parser.add_argument("--max-tokens", type=int, default=defaults.stage_3_context_max_tokens)
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
        stage_3_context_model=args.model,
        stage_3_context_temperature=args.temperature,
        stage_3_context_seed=args.seed,
        stage_3_context_domains_per_schema=args.domains_per_schema,
        stage_3_context_cases_per_domain=args.cases_per_domain,
        stage_3_context_max_retries=args.max_retries,
        stage_3_context_max_tokens=args.max_tokens,
        openrouter_api_base_url=args.api_base_url,
    )
    provider = OpenRouterProvider(
        api_key=api_key,
        base_url=config.openrouter_api_base_url,
        timeout_seconds=args.request_timeout,
    )
    try:
        run_dir = run_context_stage_3(
            config,
            input_path=args.input.resolve(),
            provider=provider,
            run_id=args.run_id,
        )
    except Exception as exc:
        LOGGER.error("Context-first Stage 3 did not complete: %s", exc)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
