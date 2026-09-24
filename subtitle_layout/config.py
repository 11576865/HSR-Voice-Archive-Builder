from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SubtitleRenderConfig:
    """Consolidated configuration for subtitle rendering pipeline and visual styles."""

    enable_karaoke: bool = False
    enable_frosted_glass: bool = False
    enable_multi_layer_outline: bool = False
    enable_kinetic: bool = True
    kinetic_options: dict[str, Any] = field(default_factory=dict)
    fade_in_ms: int = 200
    fade_out_ms: int = 200
    use_audio_aware_fade: bool = True

    @classmethod
    def plain_text_preset(cls) -> SubtitleRenderConfig:
        """Standard plain text rendering without dynamic kinetic motion, frosted glass, or karaoke."""
        return cls(
            enable_karaoke=False,
            enable_frosted_glass=False,
            enable_multi_layer_outline=False,
            enable_kinetic=False,
            kinetic_options={},
            fade_in_ms=0,
            fade_out_ms=0,
            use_audio_aware_fade=False,
        )

    @classmethod
    def pr72_default_preset(cls) -> SubtitleRenderConfig:
        """Default preset with standard kinetic fade and smooth transitions."""
        return cls(
            enable_karaoke=False,
            enable_frosted_glass=False,
            enable_multi_layer_outline=False,
            enable_kinetic=True,
            kinetic_options={},
            fade_in_ms=200,
            fade_out_ms=200,
            use_audio_aware_fade=True,
        )

    @classmethod
    def karaoke_preset(cls) -> SubtitleRenderConfig:
        r"""Preset with word-level karaoke timing tags (\k)."""
        return cls(
            enable_karaoke=True,
            enable_frosted_glass=False,
            enable_multi_layer_outline=False,
            enable_kinetic=True,
            kinetic_options={},
            fade_in_ms=150,
            fade_out_ms=150,
            use_audio_aware_fade=True,
        )

    @classmethod
    def rich_media_preset(cls) -> SubtitleRenderConfig:
        """Preset with frosted glass background card and multi-layer stroboscopic outline."""
        return cls(
            enable_karaoke=False,
            enable_frosted_glass=True,
            enable_multi_layer_outline=True,
            enable_kinetic=True,
            kinetic_options={},
            fade_in_ms=200,
            fade_out_ms=200,
            use_audio_aware_fade=True,
        )
