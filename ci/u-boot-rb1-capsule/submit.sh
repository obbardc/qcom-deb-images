#!/bin/sh
# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

# HACK: submit the RB1 U-Boot capsule update test for a build-on-pr run
#
# usage: submit.sh <build-url> [<artifact-prefix>]
#
# <build-url> is the content of the build_url artifact of the debos job, e.g.
# https://.../qcom-deb-images-<run-id>-<attempt>/; <artifact-prefix> defaults
# to "trixie". Set LAVA_IDENTITY to pick another lavacli identity.

set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "usage: $0 <build-url> [<artifact-prefix>]" >&2
    exit 1
fi

build_url="${1%/}/"
artifact_prefix="${2:-trixie}"

sed -e "s|{{BUILD_DOWNLOAD_URL}}|${build_url}|g" \
    -e "s|{{ARTIFACT_PREFIX}}|${artifact_prefix}|g" \
    "$(dirname "$0")/job.yaml" >"${TMPDIR:-/tmp}/u-boot-rb1-capsule.yaml"
lavacli -i "${LAVA_IDENTITY:-foundries}" jobs submit \
    "${TMPDIR:-/tmp}/u-boot-rb1-capsule.yaml"
