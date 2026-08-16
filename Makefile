.PHONY: qa tox clean

clean:
	rm -rf .tox *.egg-info dist .mypy_cache .ruff_cache

# Requires RPM python3-tox-current-env installed.
# Also requires mypy, tox, ruff, pytest.
tox:
	tox --current-env

qa: tox
