# Flatpak for GitHub Copilot

Unofficial Flatpak packaging for the [GitHub Copilot app](https://github.com/github/app)
on Linux — GitHub's agent-native desktop app for running and steering Copilot
sessions across repositories.

The Flatpak uses GitHub's official `.deb` builds for **x86_64 and aarch64**. The
app is built with Tauri, so its UI is WebKitGTK rather than a bundled Chromium:
the package runs on the GNOME runtime (`org.gnome.Sdk//50`) instead of the
Electron BaseApp the Claude and ChatGPT Flatpaks use, and needs no Zypak.

## Building and installing

```sh
flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo
```

With a host `flatpak-builder`:

```sh
flatpak-builder --force-clean --user --install-deps-from=flathub --repo=repo --install builddir com.github.Copilot/com.github.Copilot.yaml
```

With `org.flatpak.Builder`, build only, and install with the host's
`flatpak`. Installing runs `apply_extra` in a bubblewrap sandbox, which cannot
be created from inside the Builder's own sandbox. `FLATPAK_USER_DIR` points
the Builder at your real user installation rather than its private one:

```sh
flatpak run --env=FLATPAK_USER_DIR="$HOME/.local/share/flatpak" --command=flatpak-builder org.flatpak.Builder --force-clean --user --install-deps-from=flathub --repo=repo builddir com.github.Copilot/com.github.Copilot.yaml
```

```sh
flatpak remote-add --user --no-gpg-verify --if-not-exists copilot-local "$PWD/repo"
```

```sh
flatpak install --user copilot-local com.github.Copilot
```

```sh
flatpak run com.github.Copilot
```

The source repository and the published Flatpak remote contain no GitHub
application code. Installing from either, Flatpak downloads the architecture's
`.deb` straight from the `github/app` release as `extra-data`, verifies its
pinned SHA-256 and size, and runs `apply_extra` locally.

The `.flatpak` bundles attached to releases are the exception. Flatpak never
downloads extra-data while installing a bundle, so a working bundle has to
carry the `.deb` inside it, and these do.

`apply_extra` streams the `.deb`'s `data.tar` member through `bsdtar`, so the
650 MB tarball never lands on disk, and moves the package's `usr/` tree to
`/app/extra/copilot`. Tauri finds its resources at `<exe_dir>/../lib/GitHub Copilot`,
so that layout is kept as is. It also checks the ELF machine of the binaries
against the `.deb`'s architecture, so a mixed-up pair of sources fails at
install time instead of at launch. The GNOME SDK supplies `ar` and `bsdtar`, so
unlike the sibling Flatpaks no 7-Zip module is bundled.

The tray icon needs `libayatana-appindicator3`, which the GNOME runtime lacks.
It is built from vendored Flathub shared modules; see
[`com.github.Copilot/shared-modules`](com.github.Copilot/shared-modules/README.md).

## Upstream verification

GitHub publishes the Linux packages only as release assets. Neither the `.deb`
nor the `.rpm` carries an OpenPGP signature, the releases have no artifact
attestation, and the Tauri updater signature in `latest.json` covers the
AppImage only. So there is no publisher signature to check, and this repository
does not pretend otherwise.

What `update-release-metadata.py` uses instead is the release API over TLS:

1. The release must exist under exactly `v<version>`, published — not a draft
   or a prerelease.
2. Each `.deb` asset must be fully uploaded, by `github-app-releaser[bot]`, the
   account that uploaded every Linux asset so far.
3. Its `digest` — computed by GitHub when the asset was uploaded — must be
   SHA-256, and the API's download URL must equal the manifest URL exactly.
4. Both architectures must name the same version, and each source's
   `only-arches` and `filename` must match the architecture in its URL.

The sha256 and size are then written into the manifest. Flatpak checks both
again on every install, so an asset replaced after the pin fails installation
instead of being installed, and CI's `verify-pins` job re-resolves the pins on
every build so such a change shows up as a red build.

## Sandboxing

The app has no access to the real home directory. `--persist=.copilot` gives it
a private `~/.copilot` — CLI config, session state, plugins, extensions and, by
default, the per-session git worktrees — stored on the host at
`~/.var/app/com.github.Copilot/.copilot`. It is not shared with a
host-installed Copilot CLI. The app's own database and settings live under
`~/.var/app/com.github.Copilot/data` and `config`.

### Working with existing checkouts

Cloned repositories and worktrees created by the app stay inside the sandbox.
To let it work on checkouts you already have, grant the directory that holds
them:

```sh
flatpak override --user --filesystem=~/src com.github.Copilot
```

Grant the narrowest directory that works; `--filesystem=home` hands the agent
your whole home directory. Undo with `--nofilesystem=~/src`.

### Git identity

Only `~/.copilot` survives a restart, so a `git config --global` written to
`~/.gitconfig` would be lost. The launcher creates
`$XDG_CONFIG_HOME/git/config` on first start; Git then writes `--global`
settings there, inside the app's persisted config directory:

```sh
flatpak run --command=git com.github.Copilot config --global user.email you@example.com
```

### Login keyring

The app is granted no keyring at all — neither
`--talk-name=org.freedesktop.secrets` nor the KWallet names the Claude Flatpak
grants.

Secret Service is the one the app would use: its keyring code is the
`dbus-secret-service` crate and nothing else, so it never speaks KWallet's own
`org.kde.kwalletd5`/`6` interface and a KWallet grant would go unused. And
Secret Service has no per-application isolation: a client allowed onto it can
enumerate and read every other unlocked secret too — browser passwords, Wi-Fi
keys, other apps' tokens. Here that would extend to every command the agent
runs, since those run inside the same sandbox. KDE is no exception: since
KWallet 5.97, kwalletd also serves `org.freedesktop.secrets`, and the grant
exposes the wallet through that API rather than through KWallet's own one.

Without the grant the app logs "System keychain unavailable" and keeps the
token in its own database under the Flatpak's private data directory. Treat it
like any other credential file in your home directory. To opt into the login
keyring instead:

```sh
flatpak override --user --talk-name=org.freedesktop.secrets com.github.Copilot
```

Undo it with `--no-talk-name=org.freedesktop.secrets`. Expect to sign in again
after switching.

### Updates

Updates come through Flatpak. The app's built-in updater only knows how to
replace an AppImage or reinstall a `.deb`/`.rpm` through `pkexec`, and neither
is possible inside the sandbox, so an in-app "update available" prompt cannot
complete; update the Flatpak instead.

## Adding tools

The sandbox ships Git (from the GNOME SDK) and Node (through the auto-installed
`org.freedesktop.Sdk.Extension.node24`, so `npx`-based MCP servers work). VS
Code tool extensions such as `podman`, `fish` and `git-lfs` are detected
automatically under `/app/tools` — note the `//25.08` branch, which has to
match:

```sh
flatpak install flathub com.visualstudio.code.tool.podman//25.08
```

SDK extensions are opt-in through `FLATPAK_ENABLE_SDK_EXT`, as a
comma-separated list of short names or `*` for everything installed:

```sh
flatpak install flathub org.freedesktop.Sdk.Extension.golang//25.08
flatpak run --env=FLATPAK_ENABLE_SDK_EXT=golang com.github.Copilot
```

To make it stick for the desktop entry and URL handler too:

```sh
flatpak override --user --env=FLATPAK_ENABLE_SDK_EXT=golang com.github.Copilot
```

The launcher records what it wired up:

```sh
tail ~/.var/app/com.github.Copilot/cache/github-copilot-flatpak/launcher.log
```

## Updating GitHub Copilot

Renovate's `github-releases` datasource watches `github/app` and bumps both
architecture URLs in one `copilot` group. The post-Renovate workflow then pins
the new assets and adds the metainfo release:

```sh
./com.github.Copilot/update-release-metadata.py
./com.github.Copilot/update-metainfo.sh
```

The updater needs a `python3` with PyYAML. `GITHUB_TOKEN` or `GH_TOKEN` is used
when set, for the higher API rate limit. `--print-version` prints
`copilot=<version>` without touching the network; CI uses it to check that
refreshing the pins did not move the version.

The GNOME runtime is tracked too, but each bump waits for approval on the
Dependency Dashboard: GNOME 51 moves to freedesktop 26.08, and the `25.08`
extension versions in the manifest have to move with it by hand — while the VS
Code tool extensions are only published up to 25.08.

## CI

`.github/workflows/flatpak-build.yaml` builds natively on `ubuntu-24.04` and
`ubuntu-24.04-arm`, installs each result — which runs `apply_extra` against that
architecture's `.deb` — and checks the payload and its library dependencies
inside the sandbox. A `publish` job merges both architectures into one signed
OSTree repository for GitHub Pages, and release bundles are attached per
architecture (`com.github.Copilot-x86_64.flatpak`,
`com.github.Copilot-aarch64.flatpak`).

It expects:

- variables `GPG_KEY_ID`, `GPG_KEY_GREP`, and `APP_CLIENT_ID`
- secrets `GPG_PRIVATE_KEY`, `GPG_PASSPHRASE`, and `APP_PRIVATE_KEY`
- GitHub Pages enabled with GitHub Actions as its source
- arm64 hosted runners, which are free for public repositories

Once published:

```sh
flatpak remote-add --if-not-exists --user github-copilot-flatpak https://kk-daniel.github.io/github-copilot-flatpak/github-copilot.flatpakrepo
```

```sh
flatpak install --user github-copilot-flatpak com.github.Copilot
```

## Licence

MIT for the packaging in this repository. The GitHub Copilot app itself is
proprietary, © GitHub, Inc., and covered by
[GitHub's terms](https://docs.github.com/site-policy/github-terms/github-terms-for-additional-products-and-features).
It is downloaded directly from GitHub's release at Flatpak installation time and
is not redistributed by this source repository or its Flatpak remote; the
release bundles do embed it, as described above.
