# 0020 — One version source and reproducible release artifacts

**Status:** Accepted, 2026-11-18 (0.8.0 T9a).

## Context

Three facts were true before 0.8.0 T9a: the version was extracted with a
grep/cut copy in every build/install script, the C# `hello` client string and
the protocol example carried their own version literals, and Blender's
extension build wrote entry timestamps and ordering from the filesystem, so a
different checkout could produce different zip bytes. The `.gha` build also had
no deterministic-compiler flag, and the release process deployed it to a
hardcoded wine user.

Those are release-integrity problems, not product behavior. They matter because
a release artifact must be traceable to a tree and a version, and because a
deploy script that assumes one developer's user is wrong on every other prefix.

## Decision

- **One version source.** `blender/stageloom/blender_manifest.toml` holds the
  only release version literal. `scripts/version` reads it (default manifest, a
  path, or `-` for stdin). `scripts/build-extension`,
  `scripts/install-extension`, `scripts/test-extension` and
  `scripts/install-addon` call it instead of carrying their own extraction.
- **Version claims are checked.** `scripts/check-docs` still reads the manifest
  directly (it validates the source) and compares the C# `hello` string in
  `Publication.cs` and the `hello` example in `docs/protocol.md` to it. T11
  bumps the manifest and those checked claims together; a forgotten literal is
  a docs-check failure, not a silent drift.
- **The extension zip is normalized.** `scripts/build-extension` rewrites the
  Blender-built archive before validation: entries sorted by name, timestamp
  `1980-01-01 00:00:00`, fixed Unix file/directory/symlink modes, preserved
  per-entry compression (the declared wheel stays stored), empty comments and
  extra fields. It then validates the rewritten zip with Blender, checks the
  version/manifest and the no-platform-specific rule, prints the SHA-256 and
  writes `dist/stageloom-<version>.zip.sha256`.
  `scripts/test-reproducible` builds twice and asserts the fixed metadata as
  well as comparing bytes.
- **The `.gha` is deterministic.** `grasshopper/build/build-gha.sh` passes Mono
  `csc` `/deterministic`, which fixes the assembly timestamp, module version
  GUID and MVID. No `/pathmap` is used: the build emits no debug information,
  and a build from a copy of the tree at another absolute path was
  byte-identical, so source paths do not leak into the assembly. If a future
  build enables `/debug`, it must add a stable `/pathmap` from `$REPO` before
  claiming reproducibility. `scripts/test-reproducible-gha` builds twice in the
  tree and once in a copied tree, then compares all three.
- **The deploy user is discovered.** `grasshopper/deploy/deploy.sh` uses
  `STAGELOOM_WINE_USER` when set, otherwise the single non-`Public` directory
  under `$WINEPREFIX/drive_c/users`; zero or multiple users fail with the
  candidates. `--dry-run` prints the resolved user and destination without
  copying. `scripts/check-deployed` keeps scanning every user because a stale
  copy under any account is still a live-session hazard.

## Consequences

- Same tree + same toolchain produces the same extension zip and `.gha`. The
  T9a tree (before T9b's generated protocol tables) produced extension zip
  `bfb250609feb188a458541c020be8c96049dab53ffb627e7d56a2f128882eb80` and `.gha`
  `05c904d5bf5cd2012347aa9680edc3d8ade7f3854cc526e19a4cd77879d5efe4` from three
  builds (two in the tree, one from a copy at a different absolute path). Those
  hashes are historical and do **not** describe the T9b tree. On the T9b working
  tree the same checks reproduced extension zip
  `d12b6f504196d24ceaeb8c3896f162f48dbdef36346dd7112100b8e539b79b88` (two builds)
  and `.gha`
  `18c629e3fba03d30b6605ea53b2dd77221533f3f469e9b10a3ff1c57ee8ceafb` (two in-tree
  builds plus a copied-tree build); `scripts/test-extension` also passed. T11
  records the final release-tree hashes. A different zlib build can change
  deflated zip bytes, and a different Mono `csc`/referenced Rhino or Grasshopper
  assembly can change the `.gha`; the claim is same-toolchain/same-tree, not
  universal bit-for-bit reproduction.
- `scripts/test-reproducible-gha` is local/release only: it needs wine with
  Mono's `csc` and Rhino 8's managed assemblies. `scripts/check-all` runs it in
  the full local gate and `--ci` skips it explicitly; CI does not claim it.
  The check restores the original `grasshopper/build/out/Stageloom.gha` on
  exit, so `scripts/check-deployed` still compares the real built/deployed
  state.
- T11 must update `blender_manifest.toml` and the checked C#/protocol literals
  in the same change; `scripts/version` then names the new artifact.
- No production, protocol, fixture or `out_hash` behavior changes. The
  normalization and `/deterministic` flag are release tooling; the extracted
  manifest, wheel and sources remain byte-identical to the tree.
