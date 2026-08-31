"""Configuration shared by the data creation pipeline.

The canonical labels are the labels used by this research project. Source
datasets may use different terminology; aliases make that mapping explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

MAFALDA_GOLD_URL = (
    "https://raw.githubusercontent.com/ChadiHelwe/MAFALDA/"
    "main/datasets/gold_standard_dataset.jsonl"
)

# Edit this dictionary to add/remove fallacies or adapt another source's labels.
# Keys are the canonical labels that downstream stages will use. Values are
# accepted source-dataset spellings.
SELECTED_FALLACIES: dict[str, tuple[str, ...]] = {
    "Hasty generalization": ("hasty generalization",),
    "False cause / causal fallacy": (
        "false causality",
        "causal oversimplification",
        "false cause",
        "causal fallacy",
    ),
    "False dilemma": ("false dilemma",),
    "Faulty analogy": ("false analogy", "faulty analogy"),
    "Circular reasoning": ("circular reasoning",),
}

# Used by Stage 3 later; defined here so experiment dimensions have one home.
EDUCATIONAL_DOMAINS: tuple[str, ...] = (
    "biology",
    "physics",
    "psychology",
    "economics",
    "general science",
)

# Ordered preferences keep Stage 3 from forcing a fallacy into an unnatural
# domain. Only domains also present in EDUCATIONAL_DOMAINS are eligible.
STAGE_3_DOMAIN_PREFERENCES: dict[str, tuple[str, ...]] = {
    "Hasty generalization": ("biology", "psychology", "general science"),
    "False cause / causal fallacy": ("biology", "economics", "psychology"),
    "False dilemma": ("psychology", "general science", "economics"),
    "Faulty analogy": ("physics", "biology", "general science"),
    "Circular reasoning": ("general science", "economics", "psychology"),
}

OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
# Pin the model revision rather than using the moving "latest" alias.
STAGE_2_MODEL = "deepseek/deepseek-v4-flash-0731"
STAGE_2_PROMPT_VERSION = "stage2-schema-v1"
STAGE_2_5_PROMPT_VERSION = "stage2.5-schema-validation-v1"
STAGE_2_6_MODEL = "google/gemini-3.7-flash"
STAGE_2_6_PROMPT_VERSION = "stage2.6-strict-adjudication-v1"
STAGE_3_PROMPT_VERSION = "stage3-case-generation-v1"
STAGE_3_5_MODEL = STAGE_2_6_MODEL
STAGE_3_5_PROMPT_VERSION = "stage3.5-case-validation-v3"
STAGE_3_CONTEXT_PROMPT_VERSION = "stage3-context-first-generation-v5"


@dataclass(frozen=True)
class PipelineConfig:
    """Top-level experiment configuration.

    Stage 1 uses the source and fallacy fields. Later stages can use the
    educational domains and examples-per-combination fields.
    """

    source_url: str = MAFALDA_GOLD_URL
    outputs_dir: Path = PROJECT_ROOT / "outputs"
    selected_fallacies: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(SELECTED_FALLACIES)
    )
    educational_domains: tuple[str, ...] = EDUCATIONAL_DOMAINS
    examples_per_combination: int = 20
    source_timeout_seconds: float = 30.0
    openrouter_api_base_url: str = OPENROUTER_API_BASE_URL
    stage_2_model: str = STAGE_2_MODEL
    stage_2_temperature: float = 0.0
    stage_2_seed: int = 42
    stage_2_records_per_fallacy: int = 3
    stage_2_max_retries: int = 3
    stage_2_max_tokens: int = 400
    stage_2_prompt_version: str = STAGE_2_PROMPT_VERSION
    stage_2_prompt_path: Path = PROMPTS_DIR / "schema_prompt.txt"
    stage_2_5_model: str = STAGE_2_MODEL
    stage_2_5_temperature: float = 0.0
    stage_2_5_seed: int = 42
    stage_2_5_max_retries: int = 3
    stage_2_5_max_tokens: int = 700
    stage_2_5_prompt_version: str = STAGE_2_5_PROMPT_VERSION
    stage_2_5_prompt_path: Path = PROMPTS_DIR / "schema_validation_prompt.txt"
    stage_2_6_model: str = STAGE_2_6_MODEL
    stage_2_6_temperature: float = 0.0
    stage_2_6_seed: int = 42
    stage_2_6_reasoning_effort: str = "low"
    stage_2_6_max_retries: int = 3
    stage_2_6_max_tokens: int = 900
    stage_2_6_prompt_version: str = STAGE_2_6_PROMPT_VERSION
    stage_2_6_prompt_path: Path = PROMPTS_DIR / "schema_adjudication_prompt.txt"
    stage_3_model: str = STAGE_2_MODEL
    stage_3_temperature: float = 0.2
    stage_3_seed: int = 42
    stage_3_domains_per_schema: int = 2
    stage_3_cases_per_domain: int = 2
    stage_3_max_retries: int = 3
    stage_3_max_tokens: int = 800
    stage_3_prompt_version: str = STAGE_3_PROMPT_VERSION
    stage_3_prompt_path: Path = PROMPTS_DIR / "case_generation_prompt.txt"
    stage_3_domain_preferences: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(STAGE_3_DOMAIN_PREFERENCES)
    )
    stage_3_5_model: str = STAGE_3_5_MODEL
    stage_3_5_temperature: float = 0.0
    stage_3_5_seed: int = 42
    stage_3_5_reasoning_effort: str = "low"
    stage_3_5_max_retries: int = 3
    stage_3_5_max_tokens: int = 1400
    stage_3_5_prompt_version: str = STAGE_3_5_PROMPT_VERSION
    stage_3_5_prompt_path: Path = PROMPTS_DIR / "case_validation_prompt.txt"
    stage_3_context_model: str = STAGE_2_MODEL
    stage_3_context_temperature: float = 0.2
    stage_3_context_seed: int = 42
    stage_3_context_domains_per_schema: int = 2
    stage_3_context_cases_per_domain: int = 2
    stage_3_context_max_retries: int = 3
    stage_3_context_max_tokens: int = 800
    stage_3_context_prompt_version: str = STAGE_3_CONTEXT_PROMPT_VERSION
    stage_3_context_prompt_path: Path = PROMPTS_DIR / "context_case_generation_prompt.txt"
