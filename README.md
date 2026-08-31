# Extracting Student Misconceptions through Fallacious Reasoning Patterns

This repository builds a synthetic educational dataset whose primary target is
the misconception revealed by a student's context, reasoning, and answer. A
fallacy is retained as an intermediate explanatory signal.

## Current milestone: Stage 1

Stage 1 reads the official MAFALDA gold-standard JSONL dataset, maps its native
fallacy labels to this project's configurable labels, and writes only matching
examples. The source file is read-only and is never modified or copied over.

Run against the official source:

```bash
python -m data_creation.run_pipeline
```

Run against an already downloaded source file:

```bash
python -m data_creation.run_pipeline --source-path /path/to/gold_standard_dataset.jsonl
```

Each execution creates a new directory under `outputs/`. An explicit `--run-id`
is useful for named experiments; the command refuses to overwrite an existing
run. Each run contains:

- `source_filtered.json`: filtered records with canonical and original labels
- `stage_1_manifest.json`: source hash, provenance, settings, and counts
- `stage_1_failures.jsonl`: inspectable parse/record failures (empty when clean)

If a whole run fails (for example, because its source is unavailable), its new
run directory is retained with a `run_error.json` artifact explaining why.

The selected fallacies and their MAFALDA aliases are configured in
`data_creation/config.py`. The causal category intentionally groups MAFALDA's
`false causality` and `causal oversimplification`; “Faulty analogy” maps to
MAFALDA's `false analogy`.

Run the dependency-free tests with:

```bash
python -m unittest discover -s tests -v
```

## Stage 2 pilot

Stage 2 deterministically selects three records per configured fallacy and asks
OpenRouter for a topic-independent reasoning schema. Configure
`OPENROUTER_API_KEY` in `.env`, then run:

```bash
python -m data_creation.extract_schema \
  --input outputs/stage1-mafalda-pilot-verified/source_filtered.json
```

The default model is the pinned `deepseek/deepseek-v4-flash-0731` revision with
temperature 0 and seed 42. Each immutable Stage 2 run contains `schemas.json`,
all raw attempts in `raw_responses.jsonl`, an empty-or-populated failure log,
the exact pilot selection, and a reproducibility manifest. Stage 2 performs
only structural validation; semantic validation remains a later stage.

## Stage 2.5 pilot

Stage 2.5 makes a separate LLM call for each Stage 2 schema. It recovers the
target MAFALDA annotation span from the untouched official JSONL source, checks
whether the schema follows that specific reasoning, and revises invalid schemas
without changing or discarding the originals.

```bash
python -m data_creation.validate_schema \
  --input outputs/stage2-schema-pilot-v1/schemas.json
```

The immutable run includes validated schemas, every raw response, failures, a
manifest, and a complete Markdown review. Stage 3 remains intentionally absent.

## Stage 2.6 pilot

Stage 2.6 strictly adjudicates the same 15 records and treats rejection as a
successful precision-preserving outcome. It uses the annotated span as primary
evidence and a different default model from Stages 2 and 2.5:

```bash
python -m data_creation.adjudicate_schema \
  --input outputs/stage2-5-semantic-pilot-v1/validated_schemas.json
```

The default is `google/gemini-3.7-flash`, temperature 0, seed 42, and low
reasoning effort. Decision-specific invariants are checked locally before any
record is accepted into the adjudication output.

## Stage 3 pilot

Stage 3 uses only ACCEPT and REVISE outcomes to generate small educational
misconception cases. The default pilot chooses two preferred domains per schema
and creates two distinct cases per domain:

```bash
python -m data_creation.generate_case \
  --input outputs/stage2-6-strict-pilot-v1/adjudicated_schemas.json
```

The fallacy-to-domain preferences, domain count, case count, model, temperature,
and retry settings are configurable. Rejected schemas are counted and ignored.
Only structural validation is performed at this stage; semantic case validation
is intentionally deferred until the pilot has been reviewed.

## Stage 3.5 pilot

Stage 3.5 independently checks schema faithfulness, student plausibility, answer
consistency, misconception quality/generalization, and internal consistency. It
keeps clean cases unchanged, makes only targeted repairs, and treats semantic
rejection as a valid result rather than an execution failure:

```bash
python -m data_creation.validate_case \
  --input outputs/stage3-educational-pilot-v1/generated_cases.json
```

The default validator is `google/gemini-3.7-flash` at temperature 0 with low
reasoning effort. Every run is immutable and includes the complete adjudication,
separate accepted/revised/rejected files, raw responses, failures, a manifest,
and a compact human-readable review. Stage 3 generation is not invoked.
