#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if (( $# > 1 )); then
    echo "Usage: $0 [path-to-srs-trunk]" >&2
    exit 2
fi

trunk="${1:-"$here/trunk"}"
version_file="$trunk/src/core/srs_core_version6.hpp"
if [[ ! -f "$version_file" ]]; then
    echo "SRS source missing at $trunk. Supply the SRS v6.0-d6 trunk before building." >&2
    exit 1
fi
version="$(awk '$1 == "#define" { v[$2] = $3 } END {
    printf "%s.%s.%s", v["VERSION_MAJOR"], v["VERSION_MINOR"], v["VERSION_REVISION"]
}' "$version_file")"
if [[ "$version" != 6.0.134 ]]; then
    echo "Media patches target SRS 6.0.134, found $version. Review the patches and run the regressions for the new source." >&2
    exit 1
fi

cd -- "$trunk"
options=(-p2)
# Git apply treats paths as repository-relative when trunk is inside a checkout.
if repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    prefix="$(git rev-parse --show-prefix)"
    cd -- "$repo_root"
    if [[ -n "$prefix" ]]; then
        options+=(--directory="${prefix%/}")
    fi
fi

patches=(
    "$here/patches/0001-http-flv-dotted-stream-names.patch"
    "$here/patches/0002-fix-shutdown-with-active-publishers.patch"
    "$here/patches/0003-interrupt-http-flv-on-client-delete.patch"
)
pending=()
for patch in "${patches[@]}"; do
    if git apply "${options[@]}" --check "$patch" 2>/dev/null; then
        pending+=("$patch")
    elif git apply "${options[@]}" --reverse --check "$patch" 2>/dev/null; then
        echo "Already applied: ${patch##*/}"
    else
        echo "Cannot apply ${patch##*/} to $trunk." >&2
        git apply "${options[@]}" --check "$patch"
        exit 1
    fi
done

if [[ ${pending[0]+yes} ]]; then
    git apply "${options[@]}" "${pending[@]}"
    echo "Applied ${#pending[@]} SRS media patch(es)."
fi
