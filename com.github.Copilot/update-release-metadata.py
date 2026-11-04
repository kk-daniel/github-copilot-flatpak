#!/usr/bin/env python3
"""Refresh the GitHub Copilot .deb pins in the Flatpak manifest.

GitHub publishes the Linux packages only as release assets of github/app. The
.deb and .rpm carry no OpenPGP signature, the release has no artifact
attestation, and the Tauri updater signature in latest.json covers the AppImage
alone. What is available is the asset metadata in the GitHub release API: a
`digest` GitHub computes itself when the asset is uploaded, the byte `size`, and
the account that uploaded it. That is the trust anchor here, fetched over TLS
from api.github.com -- weaker than a publisher signature, and stated as such in
README.md.

Within that, everything that can be pinned is: the release must be published
(not a draft or prerelease) under the exact tag the manifest names, each asset
must be fully uploaded by GitHub's release bot, the digest must be SHA-256, and
the API's download URL must be the manifest's URL byte for byte. Flatpak then
checks the sha256 and size again at install time, so an asset swapped after
the bump fails the installation instead of installing.

The manifest is read and rewritten the way the Claude and ChatGPT updaters do
it: sources located structurally with PyYAML, values replaced by node mark so
comments and formatting survive, and the result re-parsed and required to say
exactly what was resolved before it is moved into place.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

try:
    import yaml
except ModuleNotFoundError:
    sys.exit(
        f"Error: PyYAML is required but is not available to {sys.executable}.\n"
        "The manifest is parsed as YAML rather than scanned, so a structural\n"
        "check is possible at all. Install it (dnf install python3-pyyaml /\n"
        "apt install python3-yaml), or run this script with an interpreter that\n"
        "has it."
    )

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "com.github.Copilot.yaml"

REPOSITORY = "github/app"
# Every Linux asset of every release so far was uploaded by this account. A
# replacement uploaded by anyone else -- a maintainer by hand, a compromised
# token of a different identity -- is refused rather than pinned.
UPLOADER = "github-app-releaser[bot]"
# The manifest module these pins live in, and how --print-version labels them.
MODULE = "copilot"
# The arch as it appears in the asset name, mapped to the value only-arches
# must carry for that source. Checking the pair is what catches a manifest that
# builds one architecture's flatpak around the other's binary.
ARCHES = {"x64": "x86_64", "arm64": "aarch64"}
# apply_extra looks for exactly these two names, so the manifest's filename: is
# load-bearing at install time.
FILENAME = "copilot-{arch}.deb"
ASSET = "GitHub-Copilot-linux-{arch}.deb"

RELEASE_URL = re.compile(
    r"https://github\.com/github/app/releases/download/v(?P<version>[0-9]+(?:\.[0-9]+)*)/"
    r"GitHub-Copilot-linux-(?P<arch>x64|arm64)\.deb\Z"
)
DIGEST = re.compile(r"sha256:(?P<hex>[0-9a-f]{64})\Z")


def die(message: str) -> NoReturn:
    sys.exit(f"Error: {message}")


def field(mapping, key):
    for k, v in mapping.value:
        if getattr(k, "value", None) == key:
            return v
    return None


def manifest_sources(node) -> dict:
    """Map url-arch -> the manifest's extra-data source for that arch.

    The module is found by name and only its own sources are read. Within it,
    an extra-data source that is not one of the two .debs would be fetched by
    the build and pinned by nothing here, so it is refused rather than ignored.
    """
    modules = field(node, "modules")
    if modules is None:
        die(f"{MANIFEST.name} has no modules")

    # Module entries can also be paths to included module files, which are
    # plain scalars rather than mappings and have no name to match.
    found = [
        m
        for m in modules.value
        if isinstance(m, yaml.MappingNode) and getattr(field(m, "name"), "value", None) == MODULE
    ]
    if len(found) != 1:
        die(f"{MANIFEST.name} has {len(found)} modules named {MODULE}, expected 1")

    sources = field(found[0], "sources")
    if sources is None:
        die(f"the {MODULE} module has no sources")

    by_arch = {}
    for source in sources.value:
        # The module also carries local `type: file` sources, which fetch
        # nothing and are not ours to check.
        if getattr(field(source, "type"), "value", None) != "extra-data":
            continue

        url_node = field(source, "url")
        if url_node is None:
            die(f"an extra-data source in the {MODULE} module has no url")
        match = RELEASE_URL.fullmatch(url_node.value)
        if match is None:
            die(
                f"the {MODULE} module fetches {url_node.value}, which is not a "
                f"{REPOSITORY} release .deb this script knows how to pin"
            )

        arch = match.group("arch")
        if arch in by_arch:
            die(f"{MANIFEST.name} has two {arch} extra-data sources")

        only = field(source, "only-arches")
        listed = [getattr(n, "value", n) for n in only.value] if only is not None else None
        want = ARCHES[arch]
        if listed != [want]:
            die(
                f"the {arch} .deb has only-arches {listed or 'missing'}, expected "
                f"[{want}]. A swapped pair builds one architecture's flatpak "
                "around the other's binary."
            )

        filename = field(source, "filename")
        expected = FILENAME.format(arch=arch)
        if filename is None or filename.value != expected:
            die(
                f"the {arch} .deb has filename "
                f"{filename.value if filename is not None else 'missing'}, expected "
                f"{expected}. apply_extra looks the payload up by that name."
            )

        for key in ("sha256", "size"):
            if field(source, key) is None:
                die(f"the {arch} extra-data source has no {key}")

        by_arch[arch] = {
            "url": url_node.value,
            "version": match.group("version"),
            "sha256": field(source, "sha256"),
            "size": field(source, "size"),
        }

    missing = [a for a in ARCHES if a not in by_arch]
    if missing:
        die(f"the {MODULE} module has no extra-data source for {', '.join(missing)}")
    return by_arch


def fetch_release(version: str) -> dict:
    """The release object for v<version>, from the GitHub REST API.

    A token is optional: CI passes GITHUB_TOKEN for the higher rate limit, and
    the endpoint is public either way.
    """
    url = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/v{version}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-copilot-flatpak-updater",
        },
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            # A truncated body fails json.loads, so a short read cannot be
            # mistaken for a release.
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            die(f"{REPOSITORY} has no release tagged v{version}")
        die(f"cannot fetch {url}: HTTP {error.code} {error.reason}")
    except (urllib.error.URLError, TimeoutError) as error:
        die(f"cannot fetch {url}: {error}")


def resolve(release: dict, version: str, sources: dict) -> dict:
    """Pin each arch from the release's asset metadata, or refuse."""
    if release.get("tag_name") != f"v{version}":
        die(f"asked for v{version}, the API returned {release.get('tag_name')!r}")
    # Renovate's github-releases datasource skips both, so a pin to one is a
    # hand edit that should not slip through looking like a normal bump.
    if release.get("draft"):
        die(f"v{version} is a draft release")
    if release.get("prerelease"):
        die(f"v{version} is a prerelease")

    assets = {}
    for asset in release.get("assets", []):
        name = asset.get("name")
        if name in assets:
            die(f"v{version} has two assets named {name}")
        assets[name] = asset

    resolved = {}
    for arch in ARCHES:
        name = ASSET.format(arch=arch)
        asset = assets.get(name)
        if asset is None:
            die(f"v{version} has no {name} asset")
        if asset.get("state") != "uploaded":
            die(f"{name} in v{version} is in state {asset.get('state')!r}, not uploaded")

        uploader = (asset.get("uploader") or {}).get("login")
        if uploader != UPLOADER:
            die(f"{name} in v{version} was uploaded by {uploader!r}, expected {UPLOADER}")

        url = asset.get("browser_download_url")
        if url != sources[arch]["url"]:
            die(f"the API serves {name} from {url}, the manifest points at {sources[arch]['url']}")

        # Assert the algorithm rather than stripping a prefix that may not be
        # there: a different digest written into a sha256 field would only
        # surface as a failed install.
        digest = DIGEST.fullmatch(str(asset.get("digest")))
        if digest is None:
            die(f"{name} in v{version} has digest {asset.get('digest')!r}, expected sha256:<hex>")

        size = asset.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            die(f"{name} in v{version} has size {size!r}")

        resolved[arch] = {"url": url, "sha256": digest.group("hex"), "size": str(size)}
    return resolved


def value_span(text: str, node) -> tuple[int, int]:
    """The span of a scalar's own value, excluding any &anchor or !!tag.

    A ScalarNode's start_mark points at its properties, not its value, so
    replacing the whole extent of `sha256: &pin !!str abc` deletes `&pin !!str`.
    Properties are whitespace-delimited tokens starting with ! or &, and a
    value cannot start with either, so skipping them is unambiguous.
    """
    start, end = node.start_mark.index, node.end_mark.index
    while start < end and text[start] in "!&":
        while start < end and not text[start].isspace():
            start += 1
        while start < end and text[start].isspace():
            start += 1
    return start, end


def write_manifest(text: str, sources: dict, resolved: dict) -> str:
    """Replace each sha256/size value in place, by node mark.

    Only the value is replaced, re-emitted in the style it already had, so a
    quoted scalar stays quoted and verify-pins does not report a whole-line
    diff as a pin mismatch. Offsets are absolute rather than (line, column),
    because PyYAML counts NEL, LS and PS as line breaks where str.split does not.
    """
    edits = []
    for arch in ARCHES:
        for key in ("sha256", "size"):
            node = sources[arch][key]
            # The C loader reports a plain scalar's style as '' where the Python
            # one gives None; normalise so this does not depend on which is used.
            style = node.style or None
            value = resolved[arch][key]
            if style in ("'", '"'):
                value = f"{style}{value}{style}"
            elif style is not None:
                die(f"cannot rewrite a block scalar (style {style!r}) for {arch} {key}")
            edits.append((node, value))

    # Descending, so replacing one value cannot shift the offsets of the next.
    for node, value in sorted(edits, key=lambda e: e[0].start_mark.index, reverse=True):
        start, end = value_span(text, node)
        text = text[:start] + value + text[end:]
    return text


def check_written(text: str, resolved: dict) -> None:
    """Re-parse the result and assert it says what we meant it to say.

    It does not care how the edit was made -- it asks a parser what the build
    will now fetch, and requires that to be exactly the resolved assets.
    """
    document = yaml.safe_load(text)
    modules = [
        m
        for m in document.get("modules", [])
        if isinstance(m, dict) and m.get("name") == MODULE
    ]
    if len(modules) != 1:
        die(f"after rewriting, {MANIFEST.name} has {len(modules)} modules named {MODULE}")

    seen = {}
    for source in modules[0].get("sources", []) or []:
        if not isinstance(source, dict):
            die(f"after rewriting, the {MODULE} module has a source that is not a mapping")
        if source.get("type") != "extra-data":
            continue
        match = RELEASE_URL.fullmatch(str(source.get("url", "")))
        if match is None:
            die(f"after rewriting, the {MODULE} module fetches {source.get('url')}")
        seen[match.group("arch")] = source

    for arch in ARCHES:
        source, want = seen.get(arch), resolved[arch]
        if source is None:
            die(f"after rewriting, {MANIFEST.name} has no {arch} extra-data source")
        if source.get("url") != want["url"]:
            die(f"after rewriting, the {arch} url is {source.get('url')}, expected {want['url']}")
        if str(source.get("sha256")) != want["sha256"]:
            die(f"after rewriting, the {arch} sha256 is {source.get('sha256')}, expected {want['sha256']}")
        if str(source.get("size")) != want["size"]:
            die(f"after rewriting, the {arch} size is {source.get('size')}, expected {want['size']}")


def main() -> None:
    # Read and validate the manifest before the network is touched. A manifest
    # this script cannot make sense of is a local problem, and spending an API
    # call to discover it reports the remote as the culprit.
    text = MANIFEST.read_text(encoding="utf-8")
    sources = manifest_sources(yaml.compose(text))

    # Renovate bumps each URL separately, so the two can drift apart. One
    # release carries both assets, so a skew is always a manifest error.
    versions = {sources[arch]["version"] for arch in ARCHES}
    if len(versions) != 1:
        die(
            "manifest arch versions disagree: "
            + ", ".join(f"{a}={sources[a]['version']}" for a in ARCHES)
        )
    version = versions.pop()

    if sys.argv[1:]:
        # The CI version check runs this instead of grepping the manifest, so it
        # reads the pins through the same structural code the update path uses.
        if sys.argv[1:] != ["--print-version"]:
            die(f"usage: {Path(sys.argv[0]).name} [--print-version]")
        print(f"{MODULE}={version}")
        return

    resolved = resolve(fetch_release(version), version, sources)

    updated = write_manifest(text, sources, resolved)
    check_written(updated, resolved)

    # Write beside the manifest so the rename stays on one filesystem, and
    # carry the manifest's own mode rather than the temp file's 0600.
    mode = MANIFEST.stat().st_mode
    handle = tempfile.NamedTemporaryFile(
        "w", dir=MANIFEST.parent, prefix=MANIFEST.name + ".", delete=False, encoding="utf-8"
    )
    try:
        handle.write(updated)
        handle.close()
        os.chmod(handle.name, mode)
        os.replace(handle.name, MANIFEST)
    except BaseException:
        os.unlink(handle.name)
        raise

    print(f"Pinned GitHub Copilot {version} from the {REPOSITORY} release API")
    for arch in ARCHES:
        print(f"  {arch} sha256={resolved[arch]['sha256']} size={resolved[arch]['size']}")


if __name__ == "__main__":
    main()
