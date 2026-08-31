VERSION := $(shell grep ^Version: *spec | sed 's/Version: *//')
SOURCE := dist/beancount_ai-$(VERSION).tar.gz
SRPM := dist/$(shell rpmspec -q --qf "%{name}-%{version}-%{release}.src.rpm\n" *.spec | grep -v python3)
RPM := dist/$(shell rpmspec -q --qf "noarch/%{name}-%{version}-%{release}.noarch.rpm\n" *.spec | grep python3)

.PHONY: qa tox clean dist ruff srpm rpm rpm-notests deps-fedora

clean:
	rm -rf .tox *.egg-info dist .mypy_cache .ruff_cache

# Requires RPM python3-tox-current-env installed.
# Also requires mypy, tox, ruff, pytest.
tox:
	tox --current-env

$(SOURCE): qubes-rpc/* beancount_ai/* beancount_ai/*/* MANIFEST.in pyproject.toml tox.ini mypy.ini Makefile docs/* docs/*/*
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

ruff:
	ruff check --select I --select C beancount_ai/ --fix

# Some dependencies will not be installable because they do not exist in Fedora.
deps-fedora:
	dnf install -yq --setopt=install_weak_deps=False rpm-build ruff python3-mypy systemd-rpm-macros python-rpm-macros pyproject-rpm-macros python3-tox-current-env python3-build python3-setuptools python3-pytest python3-ruff python3-httpx python3-certifi python3-PyMuPDF python3-webdav4 python3-openwebui-client python3-beancount python3-devel python3-pip
