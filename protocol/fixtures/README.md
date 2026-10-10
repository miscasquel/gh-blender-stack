# Golden frames

Each fixture is a folder `<name>/` with:

- `frame.bin`: one encoded protocol frame (see `docs/protocol.md` §3).
- `expect.json`: the decoded header and expected section sizes and checksums.

Python and C# codecs must both decode every fixture to the same values, and encode the expected values back to identical bytes (canonical header encoding).

Empty until milestone M2.
