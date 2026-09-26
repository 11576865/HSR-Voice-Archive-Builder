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
  - expand the advanced GPT-SoVITS reference tool
  - play original WAV
  - mark selected/not selected
  - choose a controlled emotion label
  - set 0..1 emotion intensity
  - rate reference quality
        |
        v
reference_annotations.json
        |
        v
<Character>_ReferencePack/
  audio/*.wav
  reference_catalog.json
  REFERENCE_INDEX.txt
  REFERENCE_INDEX.md
  rejected.csv
  README_REFERENCE_PACK.txt
```

## Audio identity and playback

The workbench resolves audio by `source_member_id` first. This is important because voice packages may contain identical WAV basenames in different folders. A basename is only used when it is unique.

The browser cannot attach the control token directly to an `<audio>` element request, so the existing frontend intentionally fetches the protected audio endpoint with the control header, converts the response to a Blob URL, and binds that Blob URL to the player. The dashboard Content Security Policy therefore explicitly allows `media-src 'self' blob:`. The reference tool loads audio only when the advanced section is expanded and releases the Blob URL again when collapsed.

The player preloads WAV metadata and shows “ready” only after the browser can play a clip with a positive duration. An empty/non-WAV response, a 0-second duration, or a decoder failure is shown explicitly instead of being reported as ready. Re-rendering the same subtitle does not interrupt an already loaded clip.

When the configured WAV source is a ZIP or 7z archive, the preview endpoint extracts it into the project state directory under `.state/reference-preview/` and reuses that extraction while the source fingerprint is unchanged.

## Annotation file

Human reference metadata is stored at the project root as:

```text
reference_annotations.json
```

Example entry:

```json
{
  "schema_version": 2,
  "entries": {
    "member:chapter5/vo_001.wav": {
      "subtitle_id": "42",
      "source_member_id": "chapter5/vo_001.wav",
      "logical_id": "chapter5-001",
      "filename": "vo_001.wav",
      "selected": true,
      "emotion": "surprised",
      "intensity": 0.8,
      "quality": "A"
    }
  }
}
```

Emotion is a controlled value:

- `unmarked`
- `neutral`
- `happy`
- `sad`
- `angry`
- `fear`
- `surprised`
- `other`

`other` is the compatibility bucket for performances that do not fit the current taxonomy.

Reference quality is also controlled:

- `unrated` — not reviewed yet
- `A` — recommended reference
- `B` — usable
- `C` — not recommended

Older annotations remain readable: blank/good/ok/poor are normalized to unrated/A/B/C when loaded.

Emotion intensity remains numeric from 0.0 to 1.0. It describes how strongly the reference performance expresses the selected emotion, not the emotion category itself. The UI explains the approximate scale from neutral/minimal expression to strong expression.

## Reference Pack export

The dashboard keeps two independent asset flows. They share source WAV identity where appropriate, but their metadata remains separate.

The GPT-SoVITS asset section provides two independent actions:

- **Export training dataset**
- **Export reference audio pack**

The Reference Pack contains only entries with `selected=true`.

The English training export requires an English source-text language and does not accept an explicitly non-English primary audio language. `auto` audio language remains possible, but the export report flags it as unconfirmed. Each copied sample is listed in `sample_provenance.csv` with its package member identity, transcript, and SHA-256. A WAV that differs from the completed archive's hash is recorded in `rejected.csv` instead of being exported with stale text.

Reference emotion, intensity, and quality are never written into the GPT-SoVITS training dataset export. Training metadata and reference-annotation metadata remain separate by design.

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
