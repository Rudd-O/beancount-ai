VERSION := $(shell grep ^Version: *spec | sed 's/Version: *//')
SOURCE := dist/beancount_ai-$(VERSION).tar.gz
SRPM := dist/$(shell rpmspec -q --qf "%{name}-%{version}-%{release}.src.rpm\n" *.spec | grep -v python3)
RPM := dist/$(shell rpmspec -q --qf "noarch/%{name}-%{version}-%{release}.noarch.rpm\n" *.spec | grep python3)

.PHONY: qa tox clean dist srpm rpm

clean:
	rm -rf .tox *.egg-info dist .mypy_cache .ruff_cache

# Requires RPM python3-tox-current-env installed.
# Also requires mypy, tox, ruff, pytest.
tox:
	tox --current-env

$(SOURCE): qubes-rpc/* beancount_ai/* beancount_ai/*/* MANIFEST.in pyproject.toml tox.ini beancount-ai.spec Makefile
	python3 -m build

dist: $(SOURCE)

$(SRPM): $(SOURCE)
	rpmbuild --define '%_sourcedir dist' --define '%_srcrpmdir dist' -bs *spec

srpm: $(SRPM)

$(RPM): $(SRPM)
	rpmbuild --define '%_rpmdir dist' --rebuild $(SRPM)

rpm: $(RPM)

rpm-notests: $(SRPM)
	rpmbuild --define '%disable_tests true' --define '%_rpmdir dist' --rebuild $(SRPM)

qa: tox
