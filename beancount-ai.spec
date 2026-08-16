# See https://docs.fedoraproject.org/en-US/packaging-guidelines/Python/#_example_spec_file

%define debug_package %{nil}

%define package_name beancount-ai
%define module_name beancount_ai

%define mybuildnumber %{?build_number}%{?!build_number:1}

Name:           python-%{package_name}
Version:        0.1.0
Release:        %{mybuildnumber}%{?dist}
Summary:        AI-powered tooling to process Beancount receipts

License:        BSD
URL:            https://github.com/Rudd-O/%{package_name}
Source:         %{module_name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  pyproject-rpm-macros, python3-devel, python3-setuptools

%global _description %{expand:
A set of programs to help you use AI to import and manage scanned receipts into Beancount.}

%description %_description

%package -n python3-%{package_name}
Summary:        %{summary}

%description -n python3-%{package_name} %_description

%prep
%autosetup -p1 -n %{module_name}-%{version}

%generate_buildrequires
%pyproject_buildrequires -t


%build
%pyproject_wheel


%install
%pyproject_install

%pyproject_save_files %{module_name}


%check
%tox


%files -n python3-%{package_name} -f %{pyproject_files}
%{_bindir}/bean-ai
%{_bindir}/bean-ai-server

%changelog
* Sun Aug 16 2026 Manuel Amador <rudd-o@rudd-o.com> 0.1.0
- First RPM packaging release
