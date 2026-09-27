from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SubtitleRenderConfig:
    """Consolidated configuration for subtitle rendering pipeline and visual styles."""

    enable_karaoke: bool = False
    karaoke_mode: str = "k"
    enable_frosted_glass: bool = False
    enable_multi_layer_outline: bool = False
    enable_kinetic: bool = True
    kinetic_options: dict[str, Any] = field(default_factory=dict)
    fade_in_ms: int = 200
    fade_out_ms: int = 200
    use_audio_aware_fade: bool = True

    # Readability / appearance settings shared by preview and ASS export.
    outline_width: float = 3.0
    shadow_depth: float = 2.0
    blur_radius: float = 0.0
    card_opacity: float = 0.62

    # Deliberately restrained transform-based entrance animation.
    enable_soft_entry: bool = False
    soft_entry_scale_percent: float = 98.0
    soft_entry_blur: float = 1.5
    soft_entry_ms: int = 160

    # Optional low-priority archive metadata overlay.
    enable_archive_hud: bool = False
    archive_character: str = ""
    archive_hud_font_size: int = 22
    archive_hud_opacity: float = 0.72

    # Layout settings are shared by preview and ASS export.
    chs_font: str = "汉仪旗黑"
    primary_font: str = "Noto Sans"
    base_chs_size: int = 48
    base_primary_size: int = 42
    margin_horizontal_percent: float = 0.03
    margin_vertical_percent: float = 0.05
    min_central_gap: float = 20.0

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
