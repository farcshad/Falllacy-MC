"""Pipeline entry point.

Stage-specific modules remain independently runnable so their artifacts can be
reviewed before proceeding. This entry point continues to run Stage 1; run
``python -m data_creation.extract_schema`` for Stage 2.
"""

from data_creation.load_source import main


if __name__ == "__main__":
    raise SystemExit(main())
