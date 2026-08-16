.PHONY: qa tox clean dist srpm rpm

clean:
	rm -rf .tox *.egg-info dist .mypy_cache .ruff_cache

# Requires RPM python3-tox-current-env installed.
# Also requires mypy, tox, ruff, pytest.
tox:
	tox --current-env

dist:
	python3 -m build

srpm: dist
	rpmbuild --define '%_sourcedir dist' --define '%_srcrpmdir dist' -bs *spec

rpm: srpm
	rpmbuild --define '%_rpmdir dist' --rebuild dist/python-beancount-ai-*.src.rpm

qa: tox
