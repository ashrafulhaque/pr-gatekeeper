.PHONY: install playground run test clean

install:
	uv sync

playground:
	uv run agents-cli playground --port 18081

run:
	uv run python -m app.agent_runtime_app

test:
	uv run pytest tests/unit tests/integration

clean:
	rm -rf .venv __pycache__ .adk *.db artifacts/
