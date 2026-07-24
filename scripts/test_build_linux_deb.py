#!/usr/bin/env python3
# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Unit tests for build-linux-deb.py.

Run with: pytest scripts/test_build_linux_deb.py
"""

import importlib.util
from pathlib import Path

import pytest

# the script has a hyphenated name so it cannot be imported normally; load it
# from its path instead
_MODULE_PATH = Path(__file__).with_name("build-linux-deb.py")
_spec = importlib.util.spec_from_file_location("build_linux_deb", _MODULE_PATH)
bld = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bld)


def _make_patches_dir(clone_dir, series="bugfix/foo.patch\n"):
    """Create a debian/patches/ dir with a series file under clone_dir."""
    patches_dir = clone_dir / "debian" / "patches"
    patches_dir.mkdir(parents=True)
    (patches_dir / "series").write_text(series)
    return patches_dir


# --- apply_series_patches -------------------------------------------------

def test_apply_series_patches_empty_is_noop(tmp_path):
    patches_dir = _make_patches_dir(tmp_path)
    bld.apply_series_patches(tmp_path, [])
    # the series is untouched and no extra files are created
    assert (patches_dir / "series").read_text() == "bugfix/foo.patch\n"
    assert list(patches_dir.iterdir()) == [patches_dir / "series"]


def test_apply_series_patches_copies_and_appends_in_order(tmp_path):
    patches_dir = _make_patches_dir(tmp_path)
    p1 = tmp_path / "aaa.patch"
    p1.write_text("patch a\n")
    p2 = tmp_path / "bbb.patch"
    p2.write_text("patch b\n")

    bld.apply_series_patches(tmp_path, [str(p1), str(p2)])

    # the patch files are copied into debian/patches/ verbatim
    assert (patches_dir / "aaa.patch").read_text() == "patch a\n"
    assert (patches_dir / "bbb.patch").read_text() == "patch b\n"

    series = (patches_dir / "series").read_text()
    # the salsa series is preserved and our patches are appended after it, in
    # the order they were given (so they apply last, on top of the salsa ones)
    assert series.startswith("bugfix/foo.patch\n")
    assert series.index("aaa.patch") < series.index("bbb.patch")
    assert series.index("bugfix/foo.patch") < series.index("aaa.patch")


def test_apply_series_patches_missing_patch_is_fatal(tmp_path):
    _make_patches_dir(tmp_path)
    with pytest.raises(SystemExit):
        bld.apply_series_patches(tmp_path, [str(tmp_path / "nope.patch")])


# --- upstream_version / find_orig_tarball ---------------------------------

@pytest.mark.parametrize("version, expected", [
    ("7.0.13-1~bpo13+1", "7.0.13"),
    ("1:7.0.13-1~bpo13+1", "7.0.13"),
    ("7.2~rc3-1~exp1", "7.2~rc3"),
    ("6.12.48-1", "6.12.48"),
])
def test_upstream_version(version, expected):
    assert bld.upstream_version(version) == expected


def test_find_orig_tarball_ignores_other_versions(tmp_path):
    # a leftover tarball from an earlier run of a different branch; sorting
    # alone would pick this one as it is "greater" than 7.0.13
    (tmp_path / "linux_7.2~rc3.orig.tar.xz").touch()
    (tmp_path / "linux_7.0.13.orig.tar.xz").touch()

    orig = bld.find_orig_tarball(tmp_path, "7.0.13")
    assert orig.name == "linux_7.0.13.orig.tar.xz"


def test_find_orig_tarball_missing_is_fatal(tmp_path):
    (tmp_path / "linux_7.2~rc3.orig.tar.xz").touch()
    with pytest.raises(SystemExit):
        bld.find_orig_tarball(tmp_path, "7.0.13")


# --- get_latest_dated_tag -------------------------------------------------

def test_get_latest_dated_tag_picks_newest_matching_prefix(monkeypatch):
    lines = [
        "h1\trefs/tags/qcom-next-7.2-rc1-20260101",
        "h2\trefs/tags/qcom-next-7.2-rc1-20260715",  # newest of the prefix
        "h3\trefs/tags/qcom-next-7.1-20250101",
        "h4\trefs/tags/other-prefix-20260716",       # wrong prefix, ignored
        "h5\trefs/heads/master",                      # not a tag, ignored
    ]

    class _Result:
        stdout = "\n".join(lines) + "\n"

    monkeypatch.setattr(bld.subprocess, "run",
                        lambda *a, **k: _Result())

    tag = bld.get_latest_dated_tag("repo", "qcom-next-")
    assert tag == "qcom-next-7.2-rc1-20260715"


def test_get_latest_dated_tag_returns_none_when_no_match(monkeypatch):
    class _Result:
        stdout = "h1\trefs/tags/other-20260101\n"

    monkeypatch.setattr(bld.subprocess, "run",
                        lambda *a, **k: _Result())

    assert bld.get_latest_dated_tag("repo", "qcom-next-") is None


# --- check_dependencies ---------------------------------------------------

def test_check_dependencies_adds_debian_packages(monkeypatch):
    seen = []
    monkeypatch.setattr(bld, "check_package_installed",
                        lambda pkg: seen.append(pkg) or True)

    bld.check_dependencies(debian_mode=True)
    assert "devscripts" in seen
    assert "quilt" in seen


def test_check_dependencies_skips_debian_packages_by_default(monkeypatch):
    seen = []
    monkeypatch.setattr(bld, "check_package_installed",
                        lambda pkg: seen.append(pkg) or True)

    bld.check_dependencies(debian_mode=False)
    assert "devscripts" not in seen
    assert "quilt" not in seen


# --- check_debian_compiler ------------------------------------------------

def _make_defines(clone_dir, c_compiler="gcc-14"):
    config_dir = clone_dir / "debian" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "defines.toml").write_text(
        f"[build]\nc_compiler = '{c_compiler}'\n"
    )


def test_check_debian_compiler_accepts_installed_cross_compiler(
        tmp_path, monkeypatch):
    _make_defines(tmp_path)
    seen = []
    monkeypatch.setattr(bld.shutil, "which",
                        lambda cc: seen.append(cc) or "/usr/bin/" + cc)

    bld.check_debian_compiler(tmp_path)
    # the versioned *cross* compiler is what the Debian rules invoke, not the
    # native one
    assert seen == [f"{bld.DEBIAN_GNU_TYPE}-gcc-14"]


def test_check_debian_compiler_missing_is_fatal(tmp_path, monkeypatch):
    _make_defines(tmp_path)
    monkeypatch.setattr(bld.shutil, "which", lambda cc: None)

    with pytest.raises(SystemExit):
        bld.check_debian_compiler(tmp_path)
