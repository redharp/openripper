# Third-party software

OpenRipper's source is licensed under [MIT](LICENSE). Its license does not
relicense bundled or separately installed dependencies.

- **MakeMKV** is separate software with its own license and key requirements.
  The native Windows launcher uses an existing installation. The Docker build
  copies its runtime from the pinned `jlesage/makemkv` image. MakeMKV is not
  licensed under OpenRipper's MIT license.
- **Python dependencies** are listed in `pyproject.toml`. Their licenses and
  notices remain with their upstream distributions.
- **Windows desktop packaging** uses pywebview (BSD-3-Clause) and PyInstaller
  (GPL with a bootloader exception permitting application distribution).
  Microsoft Edge WebView2 Runtime is a separately installed system dependency.
  The Windows package bundles Python and its dependencies; see its `licenses`
  folder for dependency metadata and available license texts.
- **Container components**, including Python, Alpine packages, Java, and
  PostgreSQL, remain governed by their respective upstream licenses.
- **FFmpeg** is invoked as a separate executable for output checks and
  thumbnails. The Docker image installs [Alpine's FFmpeg package](https://pkgs.alpinelinux.org/package/v3.22/community/x86_64/ffmpeg).
  Windows users can supply their own installation.
- **Impeccable** is optional developer tooling. Local skill installations and
  binaries are excluded from this repository and release packages.

OpenRipper does not include movies, disc images, decryption keys, or drive
firmware. Redistributors must review the terms of every component they package.
