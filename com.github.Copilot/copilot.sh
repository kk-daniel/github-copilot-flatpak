#!/usr/bin/env bash
set -euo pipefail
# The extension mount points below are empty unless the user installed
# something, and a loop over a literal `*` is never what is wanted here.
shopt -s nullglob

log_dir="${XDG_CACHE_HOME:-$HOME/.cache}/github-copilot-flatpak"
mkdir -p "$log_dir"
log_file="$log_dir/launcher.log"

log() {
  printf '%s\n' "$*" >> "$log_file"
}

# Extension diagnostics go to stderr rather than the launcher log, because a
# misspelled extension name has to be visible to whoever typed it -- the log is
# for the post-mortem, not for the one line that says why nothing happened.
msg() {
  printf '%s\n' "$*" >&2
}

# ~/.copilot is the persisted directory (--persist=.copilot). Creating it up
# front means nothing the app or its CLI writes there races a missing parent.
ensure_copilot_dir() {
  if [ ! -d "$HOME/.copilot" ]; then
    mkdir -p "$HOME/.copilot"
    log "Created $HOME/.copilot"
  fi
}

# Everything in $HOME outside ~/.copilot is discarded when the sandbox exits,
# so a `git config --global` written to ~/.gitconfig would be lost. Git only
# falls back to $XDG_CONFIG_HOME/git/config for --global writes when that file
# already exists and ~/.gitconfig does not; XDG_CONFIG_HOME is the app's own
# persisted config directory under Flatpak, so creating the file once keeps the
# user's name, email and other global settings across restarts.
ensure_git_config() {
  local config="${XDG_CONFIG_HOME:-$HOME/.config}/git/config"
  if [ ! -e "$config" ]; then
    mkdir -p "$(dirname "$config")"
    : > "$config"
    log "Created $config"
  fi
}

# Tool extensions land at /app/tools/<name> via the com.visualstudio.code.tool
# extension point declared in the manifest, so the Flathub extensions built for
# VS Code work here unmodified:
#   flatpak install flathub com.visualstudio.code.tool.podman//25.08
# Nothing gates this -- as in VS Code's launcher, whatever is mounted is wired up.
enable_tool_extensions() {
  local python_sitedir tool_dir tool_bindir tool_pythondir
  python_sitedir="$(python3 - <<'PYTHON'
import os
import site

print(os.path.relpath(site.getusersitepackages(), site.getuserbase()))
PYTHON
)"

  for tool_dir in /app/tools/*; do
    tool_bindir="$tool_dir/bin"
    if [ -d "$tool_bindir" ]; then
      export PATH="$PATH:$tool_bindir"
      log "Added $tool_bindir to PATH"
    fi

    tool_pythondir="$tool_dir/$python_sitedir"
    if [ -d "$tool_pythondir" ]; then
      if [ -z "${PYTHONPATH:-}" ]; then
        export PYTHONPATH="$tool_pythondir"
      else
        export PYTHONPATH="$PYTHONPATH:$tool_pythondir"
      fi
      log "Added $tool_pythondir to PYTHONPATH"
    fi
  done
}

# Opt-in SDK extensions. Nothing is declared in the manifest for these: the
# runtime is org.gnome.Sdk, whose org.freedesktop.Sdk.Extension point already
# mounts any installed extension of the matching branch at /usr/lib/sdk/<name>.
# FLATPAK_ENABLE_SDK_EXT takes a comma-separated list of short names -- golang,
# not org.freedesktop.Sdk.Extension.golang -- or "*" for everything installed:
#
#   flatpak install flathub org.freedesktop.Sdk.Extension.golang//25.08
#   flatpak run --env=FLATPAK_ENABLE_SDK_EXT=golang com.github.Copilot
#
# Unset enables nothing, which is the right default: these are toolchains the
# agent can run, and they should be there because someone asked for them.
enable_sdk_extensions() {
  local spec="${FLATPAK_ENABLE_SDK_EXT:-}"
  local sdk=() dir ext
  [ -n "$spec" ] || return 0

  if [ "$spec" = "*" ]; then
    for dir in /usr/lib/sdk/*; do
      sdk+=("${dir##*/}")
    done
  else
    IFS=',' read -ra sdk <<< "$spec"
  fi

  for ext in "${sdk[@]}"; do
    [ -n "$ext" ] || continue
    if [ ! -d "/usr/lib/sdk/$ext" ]; then
      msg "Requested SDK extension \"$ext\" is not installed"
      log "Requested SDK extension \"$ext\" is not installed"
      continue
    fi
    log "Enabling SDK extension \"$ext\""
    if [ -f "/usr/lib/sdk/$ext/enable.sh" ]; then
      # Third-party script we do not control: drop errexit and nounset so a
      # stray unset variable or failing command in it cannot take the launcher
      # down before the app ever starts. pipefail is left alone.
      set +eu
      # shellcheck source=/dev/null
      . "/usr/lib/sdk/$ext/enable.sh"
      set -eu
    else
      export PATH="$PATH:/usr/lib/sdk/$ext/bin"
    fi
  done
}

ensure_copilot_dir
ensure_git_config
enable_tool_extensions
enable_sdk_extensions
log "PATH=$PATH"

# No GPU or windowing flags: this is WebKitGTK, not Chromium, and GTK picks
# Wayland or X11 from the sockets the sandbox has. The keyring is likewise left
# alone -- with no Secret Service reachable the app keeps its OAuth token in
# its own database under ~/.var/app/com.github.Copilot (README has the opt-in).
exec /app/extra/copilot/bin/github "$@"
