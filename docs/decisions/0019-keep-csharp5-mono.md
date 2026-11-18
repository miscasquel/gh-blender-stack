# 0019 — Keep C# 5/Mono through 1.0; .NET spike no-go

**Status:** Accepted, 2026-11-18 (0.8.0 T9c). Closes the
`net48`/`net7.0-windows` spike in
[plan-quality-0.7-0.8.md](../plan-quality-0.7-0.8.md) §2.4 D7 and W9.

## Context

The Grasshopper plugin is compiled C# 5 with Mono's `csc.exe` under wine and
loaded by Rhino 8/GH1. C# 5 has no `ClientWebSocket` or `System.Text.Json`, so
the plugin carries its own WebSocket (`MiniWsClient`) and JSON (`Json.cs`)
implementations. Those are not untested stopgaps: `MiniWsTests` covers the
handshake, frame caps, fragmentation, control/close frames and hostile input,
and the codec/JSON path is checked against the shared golden fixtures and the
number/parameter suites.

The 0.8.0 plan allowed a time-boxed spike for `net48` (with Roslyn) or
`net7.0-windows`, with migration limited to the transport/JSON layers if it
proved reliable on the target and in the development loop. That evidence is not
available here:

- the C# 5 + `csc.exe`/wine loop (with the headless Blender and C# suites) is
  the only verified development and test loop;
- no `dotnet` SDK is installed and no native Windows/Rhino trial target is
  available, so a `net48`/`net7.0-windows` build could not be loaded, measured
  or kept green (T10b later measured pure-core C# line coverage with Linux
  Mono's built-in profiler, but that is a local measurement of the existing
  C# 5 code, not a trial target and not a framework decision);
- native Windows/Rhino acceptance is deferred until after 0.8.0, so loading in
  Rhino 8 on Windows cannot be demonstrated inside this release;
- `net7.0-windows` would add a second runtime/platform to build and test;
  `net48` would replace the working `csc.exe`/wine path before a native
  replacement is verified;
- the value of the migration is maintainability and testability, not release
  speed, and the hand-written transport already has direct conformance
  coverage.

The owner decision on 2026-11-18 is therefore a no-go for 0.8.0.

## Decision

- **No-go for 0.8.0.** Do not add a `net48` or `net7.0-windows` target and do
  not migrate the transport or JSON layers.
- Keep C# 5/Mono as the production build, and keep the hand-written
  `MiniWsClient` and `Json.cs`.
- Revisit only after native Windows/Rhino acceptance, post-0.8 (1.0 or later),
  when the target load, CI and development-loop questions can be answered with
  evidence. If a later spike says go, migrate transport/JSON only; do not
  rewrite `Conversion`, `Instances` or the components.
- No production, build-script, protocol or fixture change is part of this
  decision.

## Consequences

- `MiniWsClient` and `Json.cs` remain product code; the T10 fuzz and transport
  tests continue to harden them rather than treating them as temporary.
- Development and CI stay on one runtime: C# 5 via wine, with no new Windows
  runner, SDK or framework dependency.
- The .NET spike is closed with this record. 0.8.0 does not claim a framework
  migration, and the W9/T9c item is a recorded no-go rather than pending.
- The shipped constraint stays C# 5/Mono. The hand-written transport and JSON
  remain a known maintainability trade, not an open migration.
