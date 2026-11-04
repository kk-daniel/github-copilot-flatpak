# Vendored Flathub shared modules

Copied unmodified from [flathub/shared-modules](https://github.com/flathub/shared-modules)
at commit `3d87a6632cf1e787b009e234b13a8ccfa6d15df8`, keeping the upstream
directory layout so the relative `../intltool/intltool-0.51.json` reference in
`libayatana-appindicator-gtk3.json` resolves as it does there.

Vendored rather than added as a submodule so the repository stays standalone,
like the Claude and ChatGPT Flatpaks next to it.

GitHub Copilot is a Tauri app. Its tray icon `dlopen`s
`libayatana-appindicator3.so.1` (falling back to `libappindicator3.so.1`), and
neither is in the GNOME runtime. Without one the app still starts — v1.1.34
fixed the startup crash when no tray library is available — but has no tray
icon.

To refresh, copy the same paths from a newer shared-modules commit and update
the hash above.
