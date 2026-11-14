# 0011 — Persisted loopback endpoint for the Grasshopper plugin

**Status:** Accepted, 2026-11-14.

## Context

The plugin has always connected to `ws://127.0.0.1:59130/`. The only override
was the undocumented `STAGELOOM_PORT` environment variable, which is invisible
to users, awkward to set on Windows, and easy to mismatch with the add-on's
**Server port** preference. The obvious alternative — a settings component
whose value lives in the `.gh` document — would make the endpoint per document,
so opening another file, or opening the same file on another machine, would
silently point the stage components somewhere else.

The add-on already listens on `127.0.0.1` only and stores its port in its own
global preference. Remote networking is out of scope: the link is a local
transport for one Blender/Rhino pair.

## Decision

- **One pure endpoint helper.** `grasshopper/src/core/Endpoint.cs` owns the
  default port (59130), the allowed range (1024..65535), host validation,
  canonical `ws://127.0.0.1:<port>/` text and the resolution precedence. It
  accepts `127.0.0.1` and `localhost` (case-insensitive, trimmed) and normalizes
  the latter to `127.0.0.1`; every other host, including `0.0.0.0` and `::1`, is
  rejected.
- **Precedence is saved setting > `STAGELOOM_PORT` > default.** A missing saved
  value is ignored quietly; an invalid saved value or malformed environment
  value is reported as a diagnostic and falls through rather than preventing
  the plugin from loading. `STAGELOOM_PORT` remains a documented development
  fallback, not the normal configuration path.
- **The persisted setting is global to the Grasshopper installation.**
  `EndpointSettings` uses the SDK's `GH_SettingsServer` database `Stageloom`,
  key `endpoint.port`. That writes per-user XML under the Grasshopper app-data
  folder (`%APPDATA%\Grasshopper\Stageloom.xml`), the same store Grasshopper
  uses for its own settings. That global value is authoritative; the plugin
  never reads the endpoint back from `GH_Document` or a `.gh` file.
- **The component is a control, not the storage.** **Stageloom Connection**
  (Stageloom tab, Settings) exposes a `Port` input and `Host`/`Endpoint`/
  `Status` outputs. A valid changed port is saved globally and applied; invalid
  input is a runtime message, never a dialog. Grasshopper saves the input's
  ordinary `PersistentData` with the document, but the component reseeds it
  from the saved global setting in `AddedToDocument`, so a stale document
  value cannot silently override the endpoint.
- **The endpoint is resolved before the first component registers.**
  `GhLink`'s static initializer loads the saved port and applies the
  environment/default fallback. `GhLink.Configure(host, port)` validates the
  pair, persists it, stops the old worker, starts a new one when a link was
  running or a document still has Stageloom components, and the new connection
  republishes `hello` and every schema. Stage In/Stage Out alone are therefore
  enough to connect to the saved port after a Rhino restart.
- **No wire change and no remote option.** The server side keeps its
  `127.0.0.1` bind and **Server port** preference; its panel now prints the
  full endpoint.

## Consequences

- A saved plugin port survives Rhino/Grasshopper restarts and is shared by
  every `.gh` document opened in that installation. Changing it is a single
  component edit, optionally removed afterwards.
- A mismatch with the add-on is still possible (the two preferences are
  independent); both sides now display the endpoint, so it is visible rather
  than guessed.
- `GH_SettingsServer` is a Grasshopper SDK type, so the settings store lives in
  `grasshopper/src/gh/`, not the pure core. The pure tests cover validation and
  precedence; the console suites cover persistence, reconfiguration and the
  component's status path without a live Rhino.
- A malformed saved setting falls back to `STAGELOOM_PORT` or 59130 with a
  startup diagnostic; it never blocks component loading.
