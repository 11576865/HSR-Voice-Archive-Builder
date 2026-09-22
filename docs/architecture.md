# Architecture

The project keeps archive construction deterministic and uses translation only for unresolved target text.

```text
primary voice package + canonical index
                │
                ▼
        resolve ordered entries
                │
        ├── official target text
        ├── confirmed incremental target text
        ├── existing target text
        └── missing target text
                    │
                    ▼
            optional translation
                │
                ▼
        resolved Timeline
                │
        ┌───────┼───────────────┐
        ▼       ▼               ▼
    manifest   SRT        continuous FLAC
        │
        ├── human subtitle overrides
        ├── optional ASS
        └── optional black MKV
```

The manifest and resolved Timeline are the durable machine-readable archive state. Human proofreading is stored as a separate override layer rather than overwriting official/API provenance. Presentation formats are regenerated from the effective final text view.
