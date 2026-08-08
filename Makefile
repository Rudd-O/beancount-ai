.PHONY: qa lint typecheck clean

lint:
	ruff check pycash-server/pycash_server/
	ruff check pycash-client/pycash_client/

typecheck: typecheck-server typecheck-client

typecheck-server:
	cd pycash-server && python -m mypy .

typecheck-client:
	cd pycash-client && python -m mypy .

qa: lint typecheck
