# 0007 — Published on GitHub

**Status:** Accepted, 2026-10-12

## Context

The project reached a working demo (v0.1.0): the mesh loop runs end to end,
all three test layers pass, and the docs state what is and is not implemented.
Until now the repository lived on local disk with no remote (ADR 0005).
Publication gives the work a home, a backup remote, and an issue tracker.

## Decision

- The repository is published publicly at
  `github.com/miscasquel/gh-blender-stack`, under GPL-3.0 (ADR 0006).
- The published v0.1.0 is the demo, not the full v1 scope. `docs/plan.md`
  stays the authoritative roadmap; the README states the current limits
  explicitly.
- Development history is preserved, with personal machine names and private
  paths rewritten out of every commit.
- Development continues on `main` after publication.

## Consequences

- The repository is public: anything committed is public. Private paths and
  secrets must stay out; the history was scrubbed once before the first push.
- CI can only run the pure-core layer (layer A). Layer B needs Blender and
  layer C needs wine with Rhino installed, so both stay local.
- Issues and future release notes live on GitHub from here on.
