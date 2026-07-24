#!/usr/bin/env python3
# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Build an arm64 Debian kernel package (.deb).

The script cross-builds a Linux kernel .deb for arm64 from one of two kinds
of source tree:

* a "mainline-style" git tree (torvalds/linux, linux-next, qcom-next or an
  arbitrary --repo/--ref): the tree is cloned, seeded with ``make defconfig``
  and built with ``make bindeb-pkg``.

* the Debian kernel-team packaging repository (--debian,
  https://salsa.debian.org/kernel-team/linux): only the ``debian/`` packaging
  lives in that repo, so the matching upstream source is fetched with Debian's
  own tooling (uscan), the Debian patch series is applied and the config is
  generated with the Debian scripts. The resulting patched tree and config are
  then built with the same ``bindeb-pkg`` path.

In both cases, custom ``.config`` fragments (positional arguments) are merged
on top of the base config with ``scripts/kconfig/merge_config.sh``, and custom
kernel patches (--kernel-patch) can be layered on top of the tree.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# git repo/ref to use

GIT_UPSTREAM = {
    "linux": {
        "repo": "https://github.com/torvalds/linux",
        "ref": "master",
        "ref_prefix": None,
    },
    "linux-next": {
        "repo": "https://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git",  # noqa: E501
        "ref": "master",
        "ref_prefix": "next-",
    },
    "qcom-next": {
        "repo": "https://github.com/qualcomm-linux/kernel",
        "ref": "qcom-next",
        "ref_prefix": "qcom-next-",
    },
    "debian": {
        # the Debian kernel-team packaging repo (debian/ only, no source)
        "repo": "https://salsa.debian.org/kernel-team/linux",
        "ref": "debian/latest",
        "ref_prefix": None,
    },
}

# arch/featureset/flavour to build for the Debian kernel; the default arm64
# flavour is "arm64" with the "none" featureset (see debian/config/arm64/
# defines.toml)
DEBIAN_ARCH = "arm64"
DEBIAN_FEATURESET = "none"
DEBIAN_FLAVOUR = "arm64"
# identifies the per-flavour build the Debian scripts produce, e.g. the setup
# target "setup_arm64_none_arm64" and the build dir "build_arm64_none_arm64"
DEBIAN_BUILD_ID = f"{DEBIAN_ARCH}_{DEBIAN_FEATURESET}_{DEBIAN_FLAVOUR}"

# base config to use
BASE_CONFIG = "defconfig"
# package set to build
DEB_PKG_SET = "bindeb-pkg"


def get_latest_dated_tag(repo, prefix):
    """
    Find the latest prefix-...-date tag from the repository.
    The date is expected to be the last component of the tag.
    """
    log_i(f"Fetching tags from {repo}...")
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", repo],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        fatal(f"Failed to fetch tags from {repo}: {e.stderr}")

    latest_tag = None
    latest_date = -1

    for line in result.stdout.splitlines():
        # output format: <hash>\trefs/tags/<tag>
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]

        if not tag.startswith(prefix):
            continue

        # check for date at the end
        tag_parts = tag.split("-")
        date_str = tag_parts[-1]

        if len(date_str) == 8 and date_str.isdigit():
            try:
                date_val = int(date_str)
                if date_val > latest_date:
                    latest_date = date_val
                    latest_tag = tag
                elif date_val == latest_date:
                    # tie-breaker: prefer lexicographically larger tag
                    # (usually newer version)
                    if latest_tag is None or tag > latest_tag:
                        latest_tag = tag
            except ValueError:
                pass

    return latest_tag


def log_i(msg):
    print(f"I: {msg}", file=sys.stderr)


def fatal(msg):
    print(f"F: {msg}", file=sys.stderr)
    sys.exit(1)


def check_package_installed(pkg):
    """Check if a package is installed using dpkg."""
    try:
        # dpkg -l "${pkg}" 2>&1 | grep -q "^ii  ${pkg}"
        result = subprocess.run(
            ["dpkg", "-l", pkg],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            # Match exactly "ii  <pkg>" at start of line
            if line.startswith(f"ii  {pkg}"):
                return True
    except subprocess.SubprocessError:
        pass
    return False


def check_dependencies(debian_mode=False):
    packages = [
        # needed to clone repository
        "git",
        # will pull gcc-aarch64-linux-gnu; should pull a native compiler on
        # arm64 and a cross-compiler on other architectures
        "crossbuild-essential-arm64",
        # linux build-dependencies; see linux/scripts/package/mkdebian
        "make",
        "flex",
        "bison",
        "bc",
        "libdw-dev",
        "libelf-dev",
        "libssl-dev",
        "libssl-dev:arm64",
        # linux build-dependencies for debs
        "dpkg-dev",
        "debhelper",
        "kmod",
        "python3",
        "rsync",
        # for nproc
        "coreutils",
    ]

    if debian_mode:
        packages += [
            # provides uscan, used to fetch the upstream source
            "devscripts",
            # the Debian kernel packaging applies its patches with quilt
            "quilt",
            # TODO: something something
            # These packages are required when building directly from the Salsa checkout.
            # The generated source files included in upstream release tarballs are not
            # present in the Git repository, so the build must regenerate them using
            # dacite and Jinja2.
            "python3-dacite",
            "python3-jinja2",
        ]

    log_i(f"Checking build-dependencies ({' '.join(packages)})")

    missing = []
    for pkg in packages:
        if check_package_installed(pkg):
            continue
        missing.append(pkg)

    if missing:
        fatal(f"Missing build-dependencies: {' '.join(missing)}")


def apply_series_patches(clone_dir, kernel_patches):
    """
    Copy custom patches into the Debian quilt series so they are applied
    after the salsa patches when the source tree is generated.
    """
    if not kernel_patches:
        return

    patches_dir = clone_dir / "debian" / "patches"
    series_file = patches_dir / "series"
    names = []
    for patch in kernel_patches:
        patch = Path(patch)
        if not patch.exists():
            fatal(f"Kernel patch '{patch}' does not exist")
        dest = patches_dir / patch.name
        log_i(f"Adding custom patch {patch} to Debian series")
        shutil.copyfile(patch, dest)
        names.append(patch.name)

    # append to the series so they apply last (on top of the salsa patches)
    with open(series_file, "a", encoding="utf-8") as f:
        f.write("\n# custom patches added by build-linux-deb.py\n")
        for name in names:
            f.write(f"{name}\n")


def upstream_version(version):
    """
    Strip the epoch and the Debian revision from a Debian version, e.g.
    "1:7.0.13-1~bpo13+1" -> "7.0.13". This is dpkg's
    DEB_VERSION_EPOCH_UPSTREAM, which names the orig tarball.
    """
    return version.split(":", 1)[-1].rsplit("-", 1)[0]


def find_orig_tarball(parent_dir, version):
    """
    Locate the orig tarball matching the given upstream version, the same way
    debian/rules' TAR_ORIG does. Globbing for any linux_*.orig.tar.* would
    happily pick up a leftover tarball from an earlier run of a different
    branch.
    """
    origs = sorted(parent_dir.glob(f"linux_{version}.orig.tar.*"))
    if not origs:
        fatal(f"No upstream orig tarball for version {version} in "
              f"{parent_dir}")
    return origs[0]


def prepare_debian_source(clone_dir, repo, ref, kernel_patches):
    """
    Clone the Debian kernel-team packaging repo, fetch the upstream source
    with the Debian scripts, apply the salsa (and any custom) patches and
    generate the arm64 config.

    Returns a tuple of (linux_dir, base_config) where linux_dir is the
    patched source tree and base_config is the Debian-generated .config.
    """
    log_i(f"Cloning Debian kernel ({repo}:{ref}) into {clone_dir}")
    # TODO: can we allow previous runs?!
    #subprocess.run(
    #    ["git", "clone", "--depth=1", "--branch", ref, repo, str(clone_dir)],
    #    check=True,
    #)

    # the salsa repo ships only the debian/ packaging; fetch the matching
    # upstream source using Debian's own tooling (handles RC versions and
    # DFSG file exclusion). This produces ../linux_<version>.orig.tar.* .
    log_i("Fetching upstream source with uscan")
    subprocess.run(
        [
            "uscan",
            "--download-current-version",
            "--vcs-export-uncompressed",
        ],
        check=True,
        cwd=clone_dir,
    )

    version = upstream_version(subprocess.check_output(
        ["dpkg-parsechangelog", "-S", "Version"],
        cwd=clone_dir,
        text=True,
    ).strip())
    orig = find_orig_tarball(clone_dir.parent, version)

    # populate the working tree with the upstream source (the tarball has a
    # single linux-<version>/ top-level directory which we strip)
    log_i(f"Unpacking upstream source {orig.name} into {clone_dir}")
    subprocess.run(
        ["tar", "xf", str(orig), "--strip-components=1", "-C", str(clone_dir)],
        check=True,
    )

    apply_series_patches(clone_dir, kernel_patches)

    # generate debian/control and debian/rules.gen with the Debian scripts.
    # This must happen before dpkg-source (which reads debian/control) and
    # works on the unpatched tree.
    # NB: the "debian/control" target regenerates debian/control and then
    # exits non-zero *on purpose* (to force a re-run in the maintainer
    # workflow); tolerate that and verify the file was produced instead.
    log_i("Generating Debian control")
    subprocess.run(
        ["make", "-f", "debian/rules", "debian/control"],
        check=False,
        cwd=clone_dir,
    )
    if not (clone_dir / "debian" / "control").is_file():
        fatal("Debian scripts did not generate debian/control")

    # apply the full patch series (salsa + any custom patches) with quilt, the
    # way the "orig" target in debian/rules does. Note that "dpkg-source
    # --before-build" cannot be used here: it dry-runs the first patch and
    # silently does nothing (exit 0) if it does not apply, which later shows up
    # as an obscure "test -d .pc" failure in debian/rules.real.
    log_i("Applying the Debian patch series (quilt push -a)")
    subprocess.run(
        ["quilt", "push", "--quiltrc", "-", "-a", "-q", "--fuzz=0"],
        check=True,
        cwd=clone_dir,
        env={
            **subprocess.os.environ,
            "QUILT_PATCHES": str(clone_dir / "debian" / "patches"),
            "QUILT_PC": ".pc",
        },
    )

    # generate the flat arm64 .config using the Debian scripts. rules.gen does
    # not set the host arch (the top-level rules does), so export it here to
    # allow the arm64 target to build on a non-arm64 host.
    log_i(f"Generating Debian config (setup_{DEBIAN_BUILD_ID})")
    subprocess.run(
        ["make", "-f", "debian/rules.gen", f"setup_{DEBIAN_BUILD_ID}"],
        check=True,
        cwd=clone_dir,
        env={"DEB_HOST_ARCH": DEBIAN_ARCH, **subprocess.os.environ},
    )
    gen_config = (clone_dir / "debian" / "build"
                  / f"build_{DEBIAN_BUILD_ID}" / ".config")
    if not gen_config.is_file():
        fatal(f"Expected Debian-generated config at {gen_config}")

    # Debian's config references module-signing and trusted keys that only
    # exist inside Debian's own packaging build (e.g. CONFIG_MODULE_SIG_KEY=
    # "output/signing_key.pem"). Debian itself strips these from its
    # distributed config; do the same so a standalone bindeb-pkg build signs
    # modules with a freshly generated ephemeral key rather than requiring
    # external key files. olddefconfig later restores buildable defaults.
    base_config = clone_dir.parent / f"{clone_dir.name}-arm64.config"
    strip_re = re.compile(
        r"^CONFIG_(MODULE_SIG_(ALL|KEY)|SYSTEM_TRUSTED_KEYS"
        r"|SYSTEM_REVOCATION_KEYS|BUILD_SALT)[ =]"
    )
    with open(gen_config, encoding="utf-8") as f_in, \
            open(base_config, "w", encoding="utf-8") as f_out:
        for line in f_in:
            if not strip_re.match(line):
                f_out.write(line)

    # assemble a clean patched source tree to build in-tree with bindeb-pkg.
    # The Debian build trees carry the debian/ packaging (and hardlink it back
    # into debian/build), so build from a copy that excludes it instead of
    # trying to prune those trees in place.
    linux_dir = clone_dir.parent / f"{clone_dir.name}-src"
    log_i(f"Copying patched source tree to {linux_dir}")
    subprocess.run(
        [
            "rsync", "-a", "--delete",
            "--exclude=/debian", "--exclude=/.pc", "--exclude=/.git",
            f"{clone_dir}/", f"{linux_dir}/",
        ],
        check=True,
    )

    return linux_dir, base_config


def main():
    DEFAULT_REPO = GIT_UPSTREAM["linux"]["repo"]
    DEFAULT_REF = GIT_UPSTREAM["linux"]["ref"]

    parser = argparse.ArgumentParser(description="Build Linux Deb")
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Git repository to clone (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"Git ref (branch/tag) to checkout (default: {DEFAULT_REF})",
    )
    parser.add_argument(
        "--linux-next",
        action="store_true",
        help="Use linux-next repository and ref defaults",
    )
    parser.add_argument(
        "--qcom-next",
        action="store_true",
        help="Use qcom-next repository and ref defaults",
    )
    parser.add_argument(
        "--debian",
        action="store_true",
        help=("Build from the Debian kernel-team salsa repository, using its "
              "scripts to generate the config and apply its patch series. "
              "Use --ref to select the branch (default: "
              f"{GIT_UPSTREAM['debian']['ref']})"),
    )
    parser.add_argument(
        "--kernel-patch",
        action="append",
        default=[],
        metavar="PATCH",
        help=("Custom kernel patch file to apply on top of the tree "
              "(repeatable). In Debian mode these are appended to the salsa "
              "quilt series"),
    )
    parser.add_argument(
        "--local-dir",
        type=str,
        default=None,
        help=("Path to an existing Linux kernel source tree;"
              " if not set, the repo will be cloned into ./linux"),
    )

    parser.add_argument(
        "fragments",
        metavar="FRAGMENT",
        type=str,
        nargs="*",
        help="Config fragments to merge",
    )

    # Use parse_known_args to allow fragments before and after flags
    args, unknown = parser.parse_known_args()
    # Combine positional fragments with unknown args (fragments after flags)
    args.fragments = args.fragments + unknown

    # default settings for next trees
    git_upstream_key = None
    if args.linux_next:
        git_upstream_key = "linux-next"
    elif args.qcom_next:
        git_upstream_key = "qcom-next"
    elif args.debian:
        git_upstream_key = "debian"

    ref_prefix = GIT_UPSTREAM["linux"]["ref_prefix"]
    if git_upstream_key is not None:
        if args.repo == DEFAULT_REPO:
            args.repo = GIT_UPSTREAM[git_upstream_key]["repo"]
        if args.ref == DEFAULT_REF:
            args.ref = GIT_UPSTREAM[git_upstream_key]["ref"]
            ref_prefix = GIT_UPSTREAM[git_upstream_key]["ref_prefix"]

    if ref_prefix:
        found_tag = get_latest_dated_tag(args.repo, ref_prefix)
        if found_tag:
            log_i(f"Found latest tag: {found_tag}")
            args.ref = found_tag
        else:
            log_i("No suitable tag found, falling back to default ref")

    debian_mode = args.debian
    check_dependencies(debian_mode=debian_mode)

    # base .config to seed the build with; in Debian mode this comes from the
    # Debian scripts, otherwise we generate it below with BASE_CONFIG
    base_config = None

    if debian_mode:
        if args.local_dir:
            fatal("--local-dir is not supported in Debian mode")
        linux_dir, base_config = prepare_debian_source(
            Path("linux-debian").resolve(),
            args.repo,
            args.ref,
            args.kernel_patch,
        )
    elif args.local_dir:
        linux_dir = Path(args.local_dir)
        if not linux_dir.exists():
            fatal(f"Provided --local-dir '{linux_dir}' does not exist")
        log_i(f"Using existing kernel source at {linux_dir}")
    else:
        linux_dir = Path("linux")
        log_i(f"Cloning Linux ({args.repo}:{args.ref}) into {linux_dir}")
        subprocess.run(
            [
                "git",
                "clone",
                "--depth=1",
                "--branch",
                args.ref,
                args.repo,
                str(linux_dir),
            ],
            check=True,
        )

    # apply custom patches directly to a plain (non-Debian) tree; in Debian
    # mode they were already added to the quilt series
    if args.kernel_patch and not debian_mode:
        for patch in args.kernel_patch:
            patch = Path(patch)
            if not patch.exists():
                fatal(f"Kernel patch '{patch}' does not exist")
            log_i(f"Applying custom patch {patch}")
            with open(patch, "rb") as f:
                subprocess.run(
                    ["git", "apply", "-p1", "-"],
                    check=True,
                    cwd=linux_dir,
                    stdin=f,
                )

    log_i("Configuring Linux")
    # directory to store local config fragments so they can be picked up by
    # kbuild
    local_conf_dir = linux_dir / "kernel" / "configs"
    local_conf_dir.mkdir(parents=True, exist_ok=True)

    config_targets = []

    for i, fragment in enumerate(args.fragments):
        if Path(fragment).exists():
            # Create a unique name for the local fragment
            local_frag_name = f"local_{i}.config"
            dest_path = local_conf_dir / local_frag_name

            log_i(f"Copying local fragment {fragment} to {dest_path}")
            with open(fragment, "r", encoding="utf-8") as f_in:
                content = f_in.read()
            with open(dest_path, "w", encoding="utf-8") as f_out:
                f_out.write(content)

            config_targets.append(f"kernel/configs/{local_frag_name}")
        elif (linux_dir / "arch" / "arm64" / "configs" / fragment).exists():
            log_i(f"Using config fragment from repo: {fragment}")
            config_targets.append(f"arch/arm64/configs/{fragment}")
        else:
            fatal(
                f"Config fragment '{fragment}' not found locally or in "
                f"repository (arch/arm64/configs/)."
            )

    nproc = subprocess.check_output(["nproc"], text=True).strip()
    make_base_command = [
        "make",
        f"-j{nproc}",
        "ARCH=arm64",
        "CROSS_COMPILE=aarch64-linux-gnu-",
        "DEB_HOST_ARCH=arm64",
    ]

    if base_config is not None:
        # seed with the Debian-generated config
        log_i(f"Using Debian-generated base config: {base_config}")
        shutil.copyfile(base_config, linux_dir / ".config")
    else:
        # Create base defconfig first
        log_i(f"Creating base config: {BASE_CONFIG}")
        subprocess.run(make_base_command + [BASE_CONFIG], check=True,
                       cwd=linux_dir)

    # Merge config fragments using merge_config.sh for proper dependency
    # handling
    if config_targets:
        merge_command = [
            "scripts/kconfig/merge_config.sh", "-m", "-r", ".config"
        ]
        merge_command.extend(config_targets)
        subprocess.run(
            merge_command,
            check=True,
            cwd=linux_dir,
            env={"ARCH": "arm64", **subprocess.os.environ}
        )

        # Finalize config with olddefconfig
        subprocess.run(
            make_base_command + ["olddefconfig"],
            check=True,
            cwd=linux_dir
        )

    log_i("Building Linux deb")
    build_command = make_base_command + [DEB_PKG_SET]
    subprocess.run(build_command, check=True, cwd=linux_dir)


if __name__ == "__main__":
    main()
