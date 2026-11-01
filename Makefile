.PHONY: test sync lint format-check typecheck pytest package package-check simulate simulate-delivery

test: sync lint format-check typecheck pytest package package-check

sync:
	uv lock --check
	uv sync --locked

lint:
	uv run --locked ruff check .

format-check:
	uv run --locked ruff format --check .

typecheck:
	uv run --locked mypy

pytest:
	uv run --locked pytest

package:
	uv run --locked python scripts/build_package.py build

package-check:
	uv run --locked python scripts/build_package.py check

simulate:
	uv run --locked python -m scripts.run_lastfm_simulation

simulate-delivery:
	uv run --locked python -m scripts.run_delivery_simulation
