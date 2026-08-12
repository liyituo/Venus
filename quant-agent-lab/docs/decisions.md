# Engineering decisions

## Offline local state

The MVP uses Python `sqlite3` instead of adding an installation step for SQLAlchemy. This keeps the core path offline and still provides structured persistence. The store boundary can be replaced by SQLAlchemy later without changing domain objects or application-service contracts.

## CLI implementation

The CLI uses `argparse` rather than Typer. The environment has Typer, but `argparse` is part of the standard library and avoids version-sensitive command bootstrapping. The command surface still covers the required workflows.

## Report rendering

JSON is the structured source of truth. Markdown is generated from the same `DailyReport` object with deterministic templates. The narrative interface is injectable, but the MVP does not call an LLM or OpenAI API.

## Demo strategy

The moving-average strategy is intentionally simple and explanatory. It exists to exercise signal -> target -> risk -> approval -> paper execution. It is not a performance claim or an investment recommendation.

## Runtime compatibility

The package declares Python 3.12+ and was executed with Python 3.13 in this workspace. The runtime check did not require dependency installation; installed FastAPI, Pydantic, PyYAML, pytest, Ruff, and mypy were reused.

## Local reset

`demo` resets only project-local runtime tables and the audit mirror before reseeding deterministic fixtures. It does not touch source/configuration or any parent-workspace files.
