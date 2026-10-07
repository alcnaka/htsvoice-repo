# Schemas

`voice.schema.json` documents the TOML-to-object shape of `voices/<id>/voice.toml`.
`registry.schema.json` documents `registry.toml`.

The runtime validator in `tools/registry.py` intentionally uses only the Python standard library, so CI does not depend on a JSON Schema package. The JSON Schemas are the public contract for tooling and editors; the Python validator additionally enforces repository-specific invariants such as directory/ID equality, release uniqueness, and lock completeness.
