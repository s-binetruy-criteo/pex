# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import

import os

python_full_versions = []
python_versions = []
python_majors = []

os_names = []
platform_systems = []
sys_platforms = []
platform_tag_regexps = []

# N.B.: The following environment variables are used by the Pex runtime to control Pip and must be
# kept in-sync with `locker.py`.
python_versions_file = os.environ.pop("_PEX_PYTHON_VERSIONS_FILE", None)
target_systems_file = os.environ.pop("_PEX_TARGET_SYSTEMS_FILE", None)

if python_versions_file:
    import json

    with open(python_versions_file) as fp:
        python_full_versions = json.load(fp)
    python_versions = sorted(set((version[0], version[1]) for version in python_full_versions))
    python_majors = sorted(set(version[0] for version in python_full_versions))

if target_systems_file:
    import json

    with open(target_systems_file) as fp:
        target_systems = json.load(fp)
    os_names = target_systems["os_names"]
    platform_systems = target_systems["platform_systems"]
    sys_platforms = target_systems["sys_platforms"]
    platform_tag_regexps = target_systems["platform_tag_regexps"]


# 1.) Universal dependency environment marker applicability.
#
# Allows all dependencies in metadata to be followed regardless
# of whether they apply to this system. For example, if this is
# Python 3.10 but a marker says a dependency is only for
# 'python_version < "3.6"' we still want to lock that dependency
# subgraph too.
def patch_marker_evaluate():
    from pip._vendor.packaging import markers  # type: ignore[import]

    original_get_env = markers._get_env
    original_eval_op = markers._eval_op

    skip = object()

    def versions_to_string(versions):
        return [".".join(map(str, version)) for version in versions]

    python_versions_strings = versions_to_string(python_versions) or skip
    python_full_versions_strings = versions_to_string(python_full_versions) or skip
    os_names_strings = os_names or skip
    platform_systems_strings = platform_systems or skip
    sys_platforms_strings = sys_platforms or skip

    def _get_env(environment, name):
        if name == "extra":
            return original_get_env(environment, name)
        if name == "python_version":
            return python_versions_strings
        if name == "python_full_version":
            return python_full_versions_strings
        if name == "os_name":
            return os_names_strings
        if name == "platform_system":
            return platform_systems_strings
        if name == "sys_platform":
            return sys_platforms_strings
        return skip

    def _eval_op(lhs, op, rhs):
        if lhs is skip or rhs is skip:
            return True
        return any(
            original_eval_op(left, op, right)
            for left in (lhs if isinstance(lhs, list) else [lhs])
            for right in (rhs if isinstance(rhs, list) else [rhs])
        )

    markers._get_env = _get_env
    markers._eval_op = _eval_op


patch_marker_evaluate()
del patch_marker_evaluate


# 2.) Universal wheel tag applicability.
#
# Allows all wheel URLs to be checked even when the wheel does not
# match system tags.
def patch_wheel_model():
    from pip._internal.models.wheel import Wheel  # type: ignore[import]

    Wheel.support_index_min = lambda *args, **kwargs: 0

    supported_checks = [lambda *args, **kwargs: True]
    if python_versions:
        import re

        def supported_version(self, *_args, **_kwargs):
            if not hasattr(self, "_versions"):
                versions = set()
                is_abi3 = ["abi3"] == list(self.abis)
                for pyversion in self.pyversions:
                    if pyversion[:2] in ("cp", "pp", "py"):
                        version_str = pyversion[2:]
                        # N.B.: This overblown seeming use of an re
                        # is necessitated by distributions like
                        # pydantic 0.18.* which incorrectly use
                        # `py36+`.
                        match = re.search(r"^(?P<major>\d)(?P<minor>\d+)?", version_str)
                        major = int(match.group("major"))
                        minor = match.group("minor")
                        if is_abi3 and major == 3:
                            versions.add(major)
                        elif minor:
                            versions.add((major, int(minor)))
                        else:
                            versions.add(major)

                self._versions = versions

            return any(
                (version in python_majors) or (version in python_versions)
                for version in self._versions
            )

        supported_checks.append(supported_version)

    if platform_tag_regexps:
        import re

        def supported_platform_tag(self, *_args, **_kwargs):
            if any(plat == "any" for plat in self.plats):
                return True
            for platform_tag_regexp in platform_tag_regexps:
                if any(re.search(platform_tag_regexp, plat) for plat in self.plats):
                    return True
            return False

        supported_checks.append(supported_platform_tag)

    Wheel.supported = lambda *args, **kwargs: all(
        check(*args, **kwargs) for check in supported_checks
    )

    # N.B.: This patch is a noop for the 20.3.4-patched Pip but is required in newer Pip.
    # The method is used as a speedup hack by newer Pip in some cases instead of
    # Wheel.support_index_min.
    Wheel.find_most_preferred_tag = lambda *args, **kwargs: 0


patch_wheel_model()
del patch_wheel_model


# 3.) Universal Python version applicability.
#
# Much like 2 (wheel applicability), we want to gather distributions
# even when they require different Pythons than the system Python.
#
# Unlike the other two patches, this patch diverges between the pip-legacy-resolver and the
# pip-2020-resolver.
def patch_requires_python():
    # The pip-legacy-resolver patch.
    from pip._internal.utils import packaging  # type: ignore[import]

    if python_full_versions:
        orig_check_requires_python = packaging.check_requires_python

        def check_requires_python(requires_python, *_args, **_kw):
            # Ensure any dependency we lock is compatible with the full interpreter range
            # specified since we have no way to force Pip to backtrack and follow paths for any
            # divergences. Most (all?) true divergences should be covered by forked environment
            # markers.
            return all(
                orig_check_requires_python(requires_python, python_full_version)
                for python_full_version in python_full_versions
            )

        packaging.check_requires_python = check_requires_python
    else:
        packaging.check_requires_python = lambda *_args, **_kw: True

    # The pip-2020-resolver patch.
    from pip._internal.resolution.resolvelib.candidates import (  # type: ignore[import]
        RequiresPythonCandidate,
    )
    from pip._internal.resolution.resolvelib.requirements import (  # type: ignore[import]
        RequiresPythonRequirement,
    )

    if python_full_versions:
        orig_get_candidate_lookup = RequiresPythonRequirement.get_candidate_lookup
        orig_is_satisfied_by = RequiresPythonRequirement.is_satisfied_by

        # Ensure we do a proper, but minimal, comparison for Python versions. Previously we
        # always tested all `Requires-Python` specifier sets against Python full versions. That
        # can be pathologically slow (see: https://github.com/pantsbuild/pants/issues/14998); so
        # we avoid using Python full versions unless the `Requires-Python` specifier set
        # requires that data. In other words:
        #
        # Need full versions to evaluate properly:
        # + Requires-Python: >=3.7.6
        # + Requires-Python: >=3.7,!=3.7.6,<4
        #
        # Do not need full versions to evaluate properly:
        # + Requires-Python: >=3.7,<4
        # + Requires-Python: ==3.7.*
        # + Requires-Python: >=3.6.0
        #
        def needs_full_versions(spec):
            components = spec.version.split(".", 2)
            if len(components) < 3:
                return False
            major_, minor_, patch = components
            if spec.operator in ("<", "<=", ">", ">=") and patch == "0":
                return False
            return patch != "*"

        def _py_versions(self):
            if not hasattr(self, "__py_versions"):
                self.__py_versions = (
                    version
                    for version in (
                        python_full_versions
                        if any(needs_full_versions(spec) for spec in self.specifier)
                        else python_versions
                    )
                    if ".".join(map(str, version)) in self.specifier
                )
            return self.__py_versions

        def get_candidate_lookup(self):
            for py_version in self._py_versions():
                delegate = RequiresPythonRequirement(
                    self.specifier, RequiresPythonCandidate(py_version)
                )
                candidate_lookup = orig_get_candidate_lookup(delegate)
                if candidate_lookup != (None, None):
                    return candidate_lookup
            return None, None

        def is_satisfied_by(self, *_args, **_kw):
            # Ensure any dependency we lock is compatible with the full interpreter range
            # specified since we have no way to force Pip to backtrack and follow paths for any
            # divergences. Most (all?) true divergences should be covered by forked environment
            # markers.
            return all(
                orig_is_satisfied_by(self, RequiresPythonCandidate(py_version))
                for py_version in self._py_versions()
            )

        RequiresPythonRequirement._py_versions = _py_versions
        RequiresPythonRequirement.get_candidate_lookup = get_candidate_lookup
        RequiresPythonRequirement.is_satisfied_by = is_satisfied_by
    else:
        RequiresPythonRequirement.get_candidate_lookup = lambda self: (self._candidate, None)
        RequiresPythonRequirement.is_satisfied_by = lambda *_args, **_kw: True


patch_requires_python()
del patch_requires_python
