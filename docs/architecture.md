# Architecture

The project separates deterministic archive construction from optional translation.

```text
canonical index
     │
     ├── English WAV source
     ├── Chinese LAB source
     └── existing bilingual index
              │
              ▼
        deterministic builder
              │
      ┌───────┼────────┐
      ▼       ▼        ▼
  manifest   SRT    continuous FLAC
      │                 │
      └──────── validation report

missing Chinese only
      │
      ▼
optional translation helper
```

The manifest is the durable intermediate representation. Presentation formats should be generated from it.
