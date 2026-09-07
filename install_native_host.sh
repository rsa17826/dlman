#!/usr/bin/env bash
# Installs the native messaging host manifest into every Chromium-family
# and Firefox native-messaging directory that actually exists on this machine.
#
# Usage:
#   ./install_native_host.sh /absolute/path/to/native_host_executable \
#       CHROME_EXTENSION_ID
#
# CHROME_EXTENSION_ID must come from a manifest.json "key" field so it's
# stable across reloads (see chrome://extensions -> Details, or compute it
# from the key at build time).

set -euo pipefail

HOST_PATH="$(which dlman)"
CHROME_EXT_ID="jgoldecddaakookanlkpdiomnkmoifnb"
HOST_NAME="com.nyix.dlman"

if [[ ! -x "$HOST_PATH" ]]; then
  echo "ERROR: $HOST_PATH does not exist or is not executable" >&2
  exit 1
fi

os="$(uname -s)"

# Explicitly known Chromium-family config dir names (relative to base).
# This list is necessarily incomplete -- new forks appear constantly -- so
# it's supplemented by a fallback scan below rather than relied on alone.
if [[ "$os" == "Darwin" ]]; then
  base="$HOME/Library/Application Support"
  KNOWN_RELDIRS=(
    "Google/Chrome"
    "Chromium"
    "Microsoft Edge"
    "BraveSoftware/Brave-Browser"
    "Vivaldi"
    "Helium"
    "net.imput.helium"
    "Iridium"
    "Ungoogled-Chromium"
    "Thorium"
    "Opera Software/Opera Stable"
  )
elif [[ "$os" == "Linux" ]]; then
  base="$HOME/.config"
  KNOWN_RELDIRS=(
    "google-chrome"
    "chromium"
    "microsoft-edge"
    "BraveSoftware/Brave-Browser"
    "vivaldi"
    "helium"
    "net.imput.helium"
    "iridium"
    "ungoogled-chromium"
    "thorium"
    "opera"
  )
else
  echo "ERROR: unsupported OS '$os'. Windows needs registry keys, not this script." >&2
  exit 1
fi

# Build the actual install-target list: known dirs that exist, plus anything
# else under $base that already has extensions installed (Default/Extensions
# or similar profile marker), so unlisted forks get caught too.
declare -A CHROMIUM_DIRS

for rel in "${KNOWN_RELDIRS[@]}"; do
  parent="$base/$rel"
  if [[ -d "$parent" ]]; then
    CHROMIUM_DIRS["$rel"]="$parent/NativeMessagingHosts"
  fi
done

# Fallback scan: any top-level dir under $base containing a "Default" or
# profile-like subdir with an "Extensions" folder is almost certainly a
# Chromium-based browser profile root, known or not.
if [[ -d "$base" ]]; then
  while IFS= read -r -d '' candidate; do
    rel="$(basename "$candidate")"
    # Skip if already captured via the known list above.
    already_known=false
    for known in "${!CHROMIUM_DIRS[@]}"; do
      if [[ "$base/$known" == "$candidate" ]]; then
        already_known=true
        break
      fi
    done
    $already_known && continue

    if find "$candidate" -mindepth 2 -maxdepth 3 -type d -iname "Extensions" -print -quit 2>/dev/null | grep -q .; then
      CHROMIUM_DIRS["$rel (detected)"]="$candidate/NativeMessagingHosts"
    fi
  done < <(find "$base" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)
fi

if [[ ${#CHROMIUM_DIRS[@]} -eq 0 ]]; then
  echo "ERROR: no Chromium-based browser config directories found under $base" >&2
  exit 1
fi

installed_any=false

for label in "${!CHROMIUM_DIRS[@]}"; do
  dir="${CHROMIUM_DIRS[$label]}"
  parent="$(dirname "$dir")"
  # Only install if the browser's own config dir exists — i.e. the browser
  # has been run at least once on this machine. Creating NativeMessagingHosts/
  # itself is fine even if it's the first native host for that browser.
  if [[ -d "$parent" ]]; then
    mkdir -p "$dir"
    manifest_path="$dir/$HOST_NAME.json"
    cat > "$manifest_path" <<EOF
{
  "name": "$HOST_NAME",
  "description": "Multithreaded download manager",
  "path": "$HOST_PATH",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$CHROME_EXT_ID/"]
}
EOF
    echo "Installed for $label: $manifest_path"
    installed_any=true
  fi
done

if [[ "$installed_any" == false ]]; then
  echo "ERROR: no known browser config directories found on this machine." >&2
  exit 1
fi
