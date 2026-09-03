#!/usr/bin/env bash
# Build the canonical Agents Remember CPython runtime from verified source.

set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=python-runtime-contract.env
. "$script_dir/python-runtime-contract.env"

data_root="${XDG_DATA_HOME:-$HOME/.local/share}/agents-remember"
cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/agents-remember/python-build"
prefix="$data_root/python/cpython-$AR_PYTHON_VERSION"
tooling_root="$data_root/tooling"

usage() {
  printf '%s\n' \
    "usage: scripts/install-python-runtime.sh [--prefix PATH] [--cache-root PATH] [--tooling-root PATH]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix)
      [ "$#" -ge 2 ] || usage
      prefix="$2"
      shift 2
      ;;
    --cache-root)
      [ "$#" -ge 2 ] || usage
      cache_root="$2"
      shift 2
      ;;
    --tooling-root)
      [ "$#" -ge 2 ] || usage
      tooling_root="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done

case "$prefix" in
  /*/cpython-"$AR_PYTHON_VERSION") ;;
  *)
    echo "[python-runtime] --prefix must be absolute and end in cpython-$AR_PYTHON_VERSION" >&2
    exit 2
    ;;
esac
case "$cache_root:$tooling_root" in
  /*:/*) ;;
  *)
    echo "[python-runtime] cache and tooling roots must be absolute" >&2
    exit 2
    ;;
esac

python="$prefix/bin/python3.13"
if [ -x "$python" ]; then
  "$python" "$script_dir/check-python-runtime.py" \
    --expected-version "$AR_PYTHON_VERSION" \
    --expected-base-prefix "$prefix" \
    --require-linux-pidfd \
    --source-url "$AR_PYTHON_SOURCE_URL" \
    --source-sha256 "$AR_PYTHON_SOURCE_SHA256" \
    --builder-commit "$AR_PYTHON_BUILD_COMMIT"
  echo "[python-runtime] canonical runtime already valid at $prefix"
  exit 0
fi
if [ -e "$prefix" ]; then
  echo "[python-runtime] refusing incomplete or foreign prefix: $prefix" >&2
  echo "[python-runtime] inspect and remove that exact prefix before retrying" >&2
  exit 1
fi

for command in curl gcc git make sha256sum tar; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "[python-runtime] missing build command: $command" >&2
    echo "[python-runtime] Ubuntu build dependencies: $AR_PYTHON_APT_BUILD_DEPS" >&2
    exit 1
  }
done

mkdir -p "$cache_root/downloads" "$tooling_root" "$(dirname -- "$prefix")"
archive="$cache_root/downloads/Python-$AR_PYTHON_VERSION.tar.xz"
if [ ! -f "$archive" ]; then
  partial="$archive.partial.$$"
  trap 'rm -f -- "$partial"' EXIT
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "$partial" "$AR_PYTHON_SOURCE_URL"
  observed="$(sha256sum "$partial" | awk '{print $1}')"
  [ "$observed" = "$AR_PYTHON_SOURCE_SHA256" ] || {
    echo "[python-runtime] source digest mismatch: expected $AR_PYTHON_SOURCE_SHA256, observed $observed" >&2
    exit 1
  }
  mv -- "$partial" "$archive"
  trap - EXIT
fi
observed="$(sha256sum "$archive" | awk '{print $1}')"
[ "$observed" = "$AR_PYTHON_SOURCE_SHA256" ] || {
  echo "[python-runtime] cached source digest mismatch: expected $AR_PYTHON_SOURCE_SHA256, observed $observed" >&2
  exit 1
}

short_commit="$(printf '%s' "$AR_PYTHON_BUILD_COMMIT" | cut -c1-12)"
builder_root="$tooling_root/pyenv-$short_commit"

validate_builder() {
  candidate_root="$1"
  observed_commit="$(git -C "$candidate_root" rev-parse HEAD)"
  [ "$observed_commit" = "$AR_PYTHON_BUILD_COMMIT" ] || {
    echo "[python-runtime] builder commit mismatch: expected $AR_PYTHON_BUILD_COMMIT, observed $observed_commit" >&2
    return 1
  }
  definition="$candidate_root/plugins/python-build/share/python-build/$AR_PYTHON_VERSION"
  [ -f "$definition" ] || {
    echo "[python-runtime] pinned builder lacks the $AR_PYTHON_VERSION definition" >&2
    return 1
  }
  grep -F "$AR_PYTHON_SOURCE_URL#$AR_PYTHON_SOURCE_SHA256" "$definition" >/dev/null || {
    echo "[python-runtime] builder definition does not bind the approved source and digest" >&2
    return 1
  }
}

require_reusable_builder() {
  candidate_root="$1"
  if [ -L "$candidate_root" ] || [ ! -d "$candidate_root/.git" ]; then
    echo "[python-runtime] refusing foreign builder path: $builder_root" >&2
    return 1
  fi
  validate_builder "$candidate_root"
}

if [ -e "$builder_root" ] || [ -L "$builder_root" ]; then
  require_reusable_builder "$builder_root"
else
  builder_staging="$(mktemp -d "$tooling_root/.pyenv-$short_commit.staging.XXXXXX")"
  cleanup_builder_staging() {
    [ -z "${builder_staging:-}" ] || rm -rf -- "$builder_staging"
  }
  trap cleanup_builder_staging EXIT
  git clone --no-checkout "$AR_PYTHON_BUILD_REPOSITORY" "$builder_staging"
  git -C "$builder_staging" checkout --detach "$AR_PYTHON_BUILD_COMMIT"
  validate_builder "$builder_staging"
  if ! mv -T --no-clobber -- "$builder_staging" "$builder_root"; then
    if [ ! -e "$builder_root" ] && [ ! -L "$builder_root" ]; then
      echo "[python-runtime] failed to publish builder at $builder_root" >&2
      exit 1
    fi
  fi
  require_reusable_builder "$builder_root"
  if [ -e "$builder_staging" ]; then
    cleanup_builder_staging
  fi
  builder_staging=""
  trap - EXIT
fi

echo "[python-runtime] source=$AR_PYTHON_SOURCE_URL"
echo "[python-runtime] sha256=$AR_PYTHON_SOURCE_SHA256"
echo "[python-runtime] builder=$AR_PYTHON_BUILD_REPOSITORY@$AR_PYTHON_BUILD_COMMIT"
echo "[python-runtime] prefix=$prefix"
PYTHON_BUILD_CACHE_PATH="$cache_root/downloads" \
  MAKE_OPTS="${MAKE_OPTS:--j4}" \
  "$builder_root/plugins/python-build/bin/python-build" -v "$AR_PYTHON_VERSION" "$prefix"

"$python" "$script_dir/check-python-runtime.py" \
  --expected-version "$AR_PYTHON_VERSION" \
  --expected-base-prefix "$prefix" \
  --require-linux-pidfd \
  --source-url "$AR_PYTHON_SOURCE_URL" \
  --source-sha256 "$AR_PYTHON_SOURCE_SHA256" \
  --builder-commit "$AR_PYTHON_BUILD_COMMIT"
