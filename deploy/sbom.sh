#!/bin/sh
# Emit a CycloneDX software bill of materials for a built brain image.
#
# Written rather than reached for, because reaching for one would mean adding a
# scanner to this deployment and its whole trust surface, to describe an image
# whose contents are 127 Debian packages and no third-party Python at all. What
# a generic tool would discover, this reads out of the image directly:
#
#   dpkg-query   the Debian packages, which are the entire dependency set
#   pip list     the Python ones, which must be EMPTY except pip itself --
#                that emptiness is a design invariant ("no PyJWT", and every
#                other wheel), so it is asserted here and not merely reported
#
# The image is inspected through `docker run`, not through the socket and not
# through a scanner mounted at /var/run/docker.sock: this script needs to read
# an image, and a bill of materials is not worth handing anything the ability to
# control the daemon.
#
# Usage:  deploy/sbom.sh brain:0.2.0 > sbom.json
set -eu

IMAGE=${1:?usage: sbom.sh <image:tag>}
DOCKER=${DOCKER:-docker}

digest=$($DOCKER inspect -f '{{.Id}}' "$IMAGE")
created=$($DOCKER inspect -f '{{.Created}}' "$IMAGE")
arch=$($DOCKER inspect -f '{{.Architecture}}' "$IMAGE")
base=$($DOCKER inspect -f '{{index .Config.Labels "org.opencontainers.image.base.name"}}' "$IMAGE")
release=$($DOCKER inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$IMAGE")
# An image built before the label existed reports an empty string. Say
# "unreleased" rather than emitting a bill of materials whose version field is
# blank: blank reads as "not filled in yet", and this one is filled in.
[ -n "$release" ] || release=unreleased
python=$($DOCKER run --rm --entrypoint python3 "$IMAGE" -c 'import platform; print(platform.python_version())')

# The invariant, checked before anything is emitted. A bill of materials that
# quietly listed a wheel somebody added would be a correct document about a
# broken promise; failing here makes it a build failure instead.
wheels=$($DOCKER run --rm --entrypoint python3 "$IMAGE" -m pip list --format=freeze \
         | grep -v '^pip==' || true)
if [ -n "$wheels" ]; then
    echo "REFUSED: the image contains third-party Python packages:" >&2
    echo "$wheels" >&2
    exit 77
fi

$DOCKER run --rm --entrypoint dpkg-query "$IMAGE" -W \
    -f '${Package}\t${Version}\t${Architecture}\n' \
| IMAGE="$IMAGE" DIGEST="$digest" CREATED="$created" ARCH="$arch" \
  BASE="$base" RELEASE="$release" PYVER="$python" python3 -c '
import json, os, sys, urllib.parse

components = [{
    "type": "application",
    "name": "brain",
    "version": os.environ["RELEASE"],
    "description": "the engine; Python standard library only, no third-party package",
    "purl": "pkg:generic/brain@" + os.environ["RELEASE"],
}, {
    "type": "library",
    "name": "python",
    "version": os.environ["PYVER"],
    "purl": "pkg:generic/python@" + os.environ["PYVER"],
}]
for line in sys.stdin:
    if not line.strip():
        continue
    name, version, arch = line.rstrip("\n").split("\t")
    components.append({
        "type": "library",
        "name": name,
        "version": version,
        "purl": "pkg:deb/debian/%s@%s?arch=%s" % (
            urllib.parse.quote(name), urllib.parse.quote(version), arch),
    })

json.dump({
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "version": 1,
    "metadata": {
        "timestamp": os.environ["CREATED"],
        "component": {
            "type": "container",
            "name": os.environ["IMAGE"],
            "version": os.environ["RELEASE"],
            "purl": "pkg:oci/brain@" + os.environ["DIGEST"],   # local digest; see below
        },
        "properties": [
            # A LOCAL content digest, and it is worth naming carefully. Under
            # the containerd image store `docker inspect .Id` is the OCI index
            # digest; under the classic store it is the image config digest
            # [both observed 2026-08-23 — this build exported a manifest, a
            # config and an index, and .Id was the index]. Either way it is not
            # a registry manifest digest, because nothing here is pushed to a
            # registry. Writing it down as one would claim an immutability this
            # deployment does not have.
            {"name": "image.id", "value": os.environ["DIGEST"]},
            {"name": "image.id_kind",
             "value": "local daemon content digest; NOT a registry manifest digest"},
            {"name": "image.architecture", "value": os.environ["ARCH"]},
            {"name": "image.base", "value": os.environ["BASE"]},
            {"name": "python.third_party_packages", "value": "0"},
        ],
    },
    "components": components,
}, sys.stdout, indent=2)
sys.stdout.write("\n")
'
