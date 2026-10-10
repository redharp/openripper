# MakeMKV Linux packages

Investigated on 2026-10-09 against upstream MakeMKV 2.0.0. OpenRipper remains an
MIT-licensed application invoking a separately licensed MakeMKV process.

The [official Linux instructions](https://forum.makemkv.com/forum/viewtopic.php?f=3&t=224)
require **both** archives, at the same version:

- `makemkv-oss`: source for the GUI, `libmakemkv` multiplexer and `libdriveio`
  MMC library. A headless build can disable the GUI. This is not the complete
  ripping engine.
- `makemkv-bin`: architecture-specific `makemkvcon` binaries plus runtime data
  and Java components. The archive's Makefile supports amd64, i386, armhf and
  arm64. These are Linux binaries, not a Windows packaging alternative.

Both upstream archives were downloaded and their SHA-256 checksums matched the
[official hash manifest](https://www.makemkv.com/download/makemkv-sha-2.0.0.txt):

```text
makemkv-bin-2.0.0.tar.gz
f1265e74875a186efdfbbbec7459a64e969033515e53cbc4d805f0a374f0a124
makemkv-oss-2.0.0.tar.gz
435316b2d219eb48c880526557addd076b5f5e6de5171424c7651f9cac95b161
```

Licensing was inspected in `makemkv-bin-2.0.0/src/eula_en_linux.txt` and
`makemkv-oss-2.0.0/License.txt`. The Linux EULA permits unchanged redistribution
and describes an exception for distribution-specific Linux repackaging with
unchanged program files and license. The OSS archive specifies per-file licenses;
binary files and files without an explicit license use the MakeMKV agreement.
The name `oss` therefore does not make all of MakeMKV open source or remove its
activation requirements. Preserve the actual upstream license texts with any
redistributed runtime; this note is not a replacement for those terms.

## Effect on OpenRipper

The existing Docker build already copies a MakeMKV runtime from a pinned
`jlesage/makemkv` image. That project's [build recipe](https://github.com/jlesage/docker-makemkv/blob/master/src/makemkv-bin/build.sh)
builds the OSS libraries without the GUI and installs the BIN component. A direct
build could remove the intermediate image dependency, but would also require us
to maintain system-library compatibility, architecture builds, license/source
distribution and checksum updates. No Docker base or MakeMKV version was changed
as part of this investigation, and the downloaded binaries were not executed.

The Windows companion continues to use a separately installed Windows MakeMKV.
Do not copy Linux binaries into the Windows bundle or describe the proprietary
MakeMKV engine as part of OpenRipper's MIT-licensed source.
