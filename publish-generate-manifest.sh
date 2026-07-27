#!/bin/bash
set -e

# test-manifest-local.sh
# Local test harness for publish-generate-manifest.sh.
# Builds a scratch "releases branch" checkout, drops in your plugin.json,
# runs the real manifest generator against it, and prints/validates the result.
#
# Usage:
#   ./test-manifest-local.sh <plugin_slug> <path-to-plugin.json> [manifest-script-path]
#
# Example:
#   ./test-manifest-local.sh esports-scheduler plugin/esportsarr/plugin.json

plugin_slug="${1:?Usage: $0 <plugin_slug> <path-to-plugin.json> [manifest-script-path]}"
plugin_json_src="${2:?Usage: $0 <plugin_slug> <path-to-plugin.json> [manifest-script-path]}"
manifest_script="${3:-./publish-generate-manifest.sh}"

[[ -f "$plugin_json_src" ]] || { echo "plugin.json not found at $plugin_json_src" >&2; exit 1; }
[[ -f "$manifest_script" ]] || { echo "manifest script not found at $manifest_script" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required (sudo apt install jq)" >&2; exit 1; }
command -v gh >/dev/null || { echo "gh CLI is required (https://cli.github.com)" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run 'gh auth login' first" >&2; exit 1; }

# Infer GITHUB_REPOSITORY from the current git remote unless already set
if [[ -z "${GITHUB_REPOSITORY:-}" ]]; then
  origin_url=$(git config --get remote.origin.url 2>/dev/null || true)
  GITHUB_REPOSITORY=$(echo "$origin_url" | sed -E 's#.*[:/]([^/]+/[^/.]+)(\.git)?$#\1#')
fi
[[ -n "$GITHUB_REPOSITORY" ]] || { echo "Could not infer GITHUB_REPOSITORY; set it manually and re-run." >&2; exit 1; }

export SOURCE_BRANCH="${SOURCE_BRANCH:-main}"
export RELEASES_BRANCH="${RELEASES_BRANCH:-releases}"
export GITHUB_REPOSITORY
export GITHUB_TOKEN="${GITHUB_TOKEN:-$(gh auth token)}"
# GPG_PRIVATE_KEY intentionally left unset for local runs -> script skips signing

echo "Repo:            $GITHUB_REPOSITORY"
echo "Source branch:   $SOURCE_BRANCH"
echo "Releases branch: $RELEASES_BRANCH"
echo "Plugin slug:     $plugin_slug"
echo

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
cd "$work_dir"

mkdir -p "plugins/${plugin_slug}"
cp "$OLDPWD/$plugin_json_src" "plugins/${plugin_slug}/plugin.json"

echo "Running manifest generator in scratch dir: $work_dir"
bash "$OLDPWD/$manifest_script"

echo
echo "=== root manifest.json ==="
jq . manifest.json

echo
echo "=== metadata/${plugin_slug}/manifest.json ==="
jq . "metadata/${plugin_slug}/manifest.json"

echo
echo "--- sanity checks ---"
latest_url=$(jq -r --arg s "$plugin_slug" '.plugins[] | select(.slug==$s) | .latest_url // empty' manifest.json)
manifest_url=$(jq -r --arg s "$plugin_slug" '.plugins[] | select(.slug==$s) | .manifest_url // empty' manifest.json)

if [[ -z "$latest_url" ]]; then
  echo "WARNING: no latest_url found for '$plugin_slug' — likely no matching GitHub Release tags yet"
  echo "         (expected tag format: ${plugin_slug}-<version>)"
else
  echo "latest_url: $latest_url"
fi

if [[ -n "$manifest_url" ]]; then
  echo "manifest_url: $manifest_url"
  if command -v curl >/dev/null; then
    status=$(curl -s -o /dev/null -w "%{http_code}" "$manifest_url" || echo "?")
    echo "  -> HTTP $status (won't resolve until this is pushed to $RELEASES_BRANCH)"
  fi
fi

echo
echo "Done. Scratch dir will be removed on exit; copy manifest.json out now if you want to keep it:"
echo "  cp \"$work_dir/manifest.json\" ."
