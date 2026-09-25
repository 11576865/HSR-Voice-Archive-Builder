# Reference Audio Workbench

The subtitle review workspace can also be used to build a human-curated GPT-SoVITS reference library.

## Why this is separate from training export

Training data and inference references are different assets:

- **Training dataset export** aims to keep as much valid, correctly transcribed character speech as possible.
- **Reference Pack export** contains only clips a human explicitly selected after listening to the original WAV while reading the official source transcript.

The reference workflow never edits the official source transcript. Human work adds metadata beside the archived entry.

## Data flow

```text
manifest entry
  + source_member_id
  + original WAV package
        |
        v
Subtitle Review Editor
  - play original WAV
  - mark selected/not selected
  - add free-form emotion tag
  - set 0..1 intensity
  - mark reference quality
        |
        v
reference_annotations.json
        |
        v
<Character>_ReferencePack/
  audio/*.wav
  reference_catalog.json
  rejected.csv
  README_REFERENCE_PACK.txt
```

## Audio identity

The workbench resolves audio by `source_member_id` first. This is important because voice packages may contain identical WAV basenames in different folders. A basename is only used when it is unique.

When the configured WAV source is a ZIP or 7z archive, the preview endpoint extracts it into the project state directory under `.state/reference-preview/` and reuses that extraction while the source fingerprint is unchanged.

## Annotation file

Human reference metadata is stored at the project root as:

```text
reference_annotations.json
```

Example entry:

```json
{
  "schema_version": 1,
  "entries": {
    "member:chapter5/vo_001.wav": {
      "subtitle_id": "42",
      "source_member_id": "chapter5/vo_001.wav",
      "logical_id": "chapter5-001",
      "filename": "vo_001.wav",
      "selected": true,
      "emotion": "surprised",
      "intensity": 0.8,
      "quality": "good"
    }
  }
}
```

Emotion is intentionally free-form. The UI offers common suggestions but does not force every character into the same emotion vocabulary.

## Reference Pack export

The dashboard's GPT-SoVITS asset section provides two independent actions:

- **Export training dataset**
- **Export reference audio pack**

The Reference Pack contains only entries with `selected=true`.

The catalog preserves:

- official source transcript;
- source language;
- source member identity;
- duration;
- human emotion tag;
- human intensity;
- human quality label.

GPT-SoVITS commonly uses clear reference clips around 3–10 seconds. The exporter does not discard a human-selected clip solely because it falls outside that range; it records `recommended_duration=false` and reports the count.

## API

FastAPI and the lightweight server expose equivalent local endpoints:

```text
GET  /api/project/{project_id}/subtitles/{subtitle_id}/audio
POST /api/project/{project_id}/reference-annotations
POST /api/project/{project_id}/export/reference-pack?speaker=<name>
```

All remain behind the existing local control-token protection.

## Scope

This workbench does **not** automatically infer emotion. It creates the reliable human-labelled reference library needed by a later Character Voice Service emotion router and long-form continuity planner.
