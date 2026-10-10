from __future__ import annotations

import unittest
from pathlib import Path


class SubtitleStyleWorkbenchUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_style_workbench_uses_adaptive_split_pane_shell(self) -> None:
        html = self.html
        self.assertIn("字幕样式工作台", html)
        self.assertNotIn(">字幕排版工作台</h2>", html)
        self.assertIn("SUBTITLE STYLE WORKBENCH", html)
        self.assertIn("STYLE INSPECTOR", html)
        self.assertIn("Subtitle Style Workbench redesign v14", html)
        self.assertIn(
            "grid-template-columns:minmax(0,1fr) clamp(390px,26vw,460px)!important",
            html,
        )
        self.assertIn("aspect-ratio:16 / 9!important", html)
        self.assertIn("max-height:calc(100dvh - var(--topbar-height) - 58px)!important", html)

    def test_redesign_resets_legacy_grid_placement_and_padding(self) -> None:
        html = self.html
        self.assertNotIn(
            "padding-right:var(--layout-inspector-width)!important",
            html,
        )
        self.assertNotIn(
            "width:var(--layout-inspector-width)!important",
            html,
        )
        self.assertGreaterEqual(html.count("grid-area:auto!important"), 2)
        self.assertIn(
            'body[data-workspace="layout"] .preview-workbench{\n'
            '    width:100%!important;\n'
            '    padding-right:0!important;',
            html,
        )

    def test_primary_actions_stay_above_long_parameter_stack(self) -> None:
        html = self.html
        action_pos = html.index('class="preview-actions"')
        controls_pos = html.index('class="preview-controls"')
        self.assertLess(action_pos, controls_pos)
        self.assertIn('id="previewGeometryBtn"', html)
        self.assertIn('id="previewLibassBtn"', html)
        self.assertIn('id="saveSubtitleLayoutBtn"', html)
        self.assertIn('id="generateSubtitleAssBtn"', html)
        self.assertIn("#generateSubtitleAssBtn{", html)
        self.assertIn("grid-column:1 / -1", html)

    def test_basic_and_advanced_controls_are_grouped_without_id_changes(self) -> None:
        html = self.html
        for title in (
            "文字与字体",
            "位置与安全区",
            "预设",
            "外观与可读性",
            "动效",
            "逐词同步",
            "档案增强",
            "画布与诊断",
        ):
            self.assertIn(title, html)

        for control_id in (
            "prevChsFont",
            "prevPriFont",
            "prevChsSize",
            "prevPriSize",
            "prevCentralGap",
            "prevMarginH",
            "prevMarginV",
            "prevOutlineWidth",
            "prevShadowDepth",
            "prevBlurRadius",
            "prevFadeIn",
            "prevFadeOut",
            "prevEnableKaraoke",
            "prevEnableArchiveHud",
            "previewBackgroundMode",
        ):
            self.assertIn(f'id="{control_id}"', html)

        self.assertIn('class="layout-inspector-section layout-section-advanced"', html)

    def test_tablet_and_mobile_breakpoints_keep_single_column_flow(self) -> None:
        html = self.html
        self.assertIn("@media (min-width:760px) and (max-width:1200px)", html)
        self.assertIn("@media (max-width:759px)", html)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))!important", html)
        self.assertIn("grid-template-columns:1fr!important", html)


    def test_v15_redraw_keeps_preview_provenance_visible(self) -> None:
        html = self.html
        self.assertIn("Subtitle Style Workbench v15", html)
        self.assertIn('class="layout-workflow-strip"', html)
        self.assertIn('class="layout-preview-region"', html)
        self.assertIn('class="layout-preview-evidence"', html)
        for element_id in (
            "previewModeLabel",
            "previewEvidenceTitle",
            "previewEvidenceDetail",
            "previewEvidenceMeta",
        ):
            self.assertEqual(html.count(f'id="{element_id}"'), 1)
        self.assertIn('data-preview-evidence="geometry"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('aspect-ratio:16 / 9!important', html)

    def test_real_render_and_geometry_actions_report_distinct_evidence(self) -> None:
        html = self.html
        self.assertIn('id="previewGeometryBtn" aria-pressed="true"', html)
        self.assertIn('id="previewLibassBtn" aria-pressed="false"', html)
        for state in ("geometry", "rendering", "libass", "stale", "failed"):
            self.assertIn(f"{state}:[", html)
        self.assertIn("setPreviewEvidence('libass')", html)
        self.assertIn("setPreviewEvidence('rendering')", html)
        self.assertIn("setPreviewEvidence('failed'", html)
        self.assertIn("setPreviewEvidence(hadResult?'stale':'geometry')", html)
        self.assertIn("真实渲染不会保存设置", html)

    def test_out_of_order_preview_results_cannot_replace_current_state(self) -> None:
        html = self.html
        self.assertIn("let libassPreviewGeneration = 0;", html)
        self.assertIn("let geometryPreviewGeneration = 0;", html)
        self.assertIn("const generation=++libassPreviewGeneration;", html)
        self.assertIn(
            "generation===libassPreviewGeneration&&identity===currentLibassPreviewIdentity()",
            html,
        )
        self.assertGreaterEqual(html.count("if(!stillCurrent())return;"), 3)
        self.assertIn("geometryPreviewGeneration++; // A queued edit", html)
        self.assertGreaterEqual(
            html.count("if(generation!==geometryPreviewGeneration)return;"), 3
        )
        self.assertIn("libassPreviewGeneration++; // Explicit user choice", html)

    def test_persisted_snapshot_is_the_submitted_snapshot(self) -> None:
        html = self.html
        self.assertIn("const submittedSettings=subtitleSettingsPayload();", html)
        self.assertIn("body:JSON.stringify({...submittedSettings,expected_project_root:currentProject.root||null})", html)
        self.assertIn("savedSubtitleSettings=submittedIdentity", html)
        self.assertIn("const newerDraft=JSON.stringify(subtitleSettingsPayload())!==submittedIdentity;", html)
        self.assertIn("if(newerDraft){", html)
        self.assertIn("return false; // Do not export the older settings", html)
        self.assertIn("markSubtitleSettingsDirty();", html)

    def test_v15_mobile_surface_is_in_flow(self) -> None:
        html = self.html
        self.assertIn(
            ".preview-workbench .layout-preview-region{position:relative;top:auto",
            html,
        )
        self.assertIn(".layout-preview-evidence{flex-direction:column}", html)
        self.assertIn(".preview-inspector .preview-actions button{min-height:42px!important}", html)

    def test_v16_manual_sample_and_render_evidence_boundaries(self) -> None:
        html = self.html
        for control in ("layoutSampleOrigin", "layoutReloadSampleBtn"):
            self.assertEqual(html.count(f'id="{control}"'), 1)
        self.assertIn("layoutTrialTextRevision++;", html)
        self.assertIn("layoutSampleSource='manual';", html)
        self.assertIn("layoutStressSample=null; // Never reuse a sample's word timings", html)
        self.assertIn("request===layoutSampleRequestGeneration", html)
        self.assertIn("loadLayoutStressSample({force:true})", html)
        self.assertIn("if(typeof img.decode==='function')", html)
        self.assertIn("img.addEventListener('error',onError,{once:true})", html)
        self.assertIn("signal:controller?.signal", html)
        self.assertIn("subtitleSettingsSavePending=true;", html)
        self.assertIn("if(subtitleSettingsSavePending)", html)


    def test_v17_subtitle_studio_has_real_corpus_workflow(self) -> None:
        html = self.html
        for element_id in (
            "layoutCorpusList",
            "layoutCorpusSearch",
            "layoutAuditBtn",
            "layoutAuditCancelBtn",
            "layoutAuditStatus",
            "layoutPreviousSampleBtn",
            "layoutNextSampleBtn",
            "layoutCorpusPrevPageBtn",
            "layoutCorpusNextPageBtn",
            "layoutCorpusPageStatus",
            "layoutCorpusMobileToggleBtn",
            "layoutStyleUndoBtn",
            "layoutStyleRedoBtn",
        ):
            self.assertEqual(html.count(f'id="{element_id}"'), 1)
        self.assertIn('class="layout-corpus-rail"', html)
        self.assertIn("layoutCorpusItems=[];", html)
        self.assertIn("layoutCorpusPageSize=80;", html)
        self.assertIn("async function runLayoutBatchAudit()", html)
        self.assertIn("layoutAuditResults.set(String(sub.id)", html)
        self.assertIn("rows=selected.slice(0,60)", html)
        self.assertIn("function moveLayoutCorpusSelection(offset)", html)
        self.assertIn("function travelLayoutStyleHistory(delta)", html)
        self.assertIn("function recordLayoutStyleHistory()", html)
        self.assertIn("resetLayoutStyleHistory();", html)

    def test_mobile_editor_keeps_stage_first_and_navigation_collapsed(self) -> None:
        html = self.html
        self.assertIn(".preview-workbench .layout-preview-region{order:1!important", html)
        self.assertIn(".preview-workbench .layout-corpus-rail{", html)
        self.assertIn("order:2!important;width:100%", html)
        self.assertIn(".preview-workbench .preview-inspector{order:3!important", html)
        self.assertIn('aria-expanded="false" aria-controls="layoutCorpusList"', html)
        self.assertIn("rail.classList.toggle('is-open')", html)



    def test_cue_editor_has_persisted_text_and_source_audio_controls(self) -> None:
        html = self.html
        for element_id in (
            "layoutCueEditorPanel",
            "layoutCueHeading",
            "layoutCueFinalText",
            "layoutCueSaveBtn",
            "layoutCueRevertBtn",
            "layoutCueSourceText",
            "layoutCueStart",
            "layoutCueEnd",
            "layoutCueDuration",
            "layoutCueScrubber",
            "layoutCueScrubberTime",
            "layoutCueAudio",
            "layoutCueAudioLoadBtn",
            "layoutCueWaveform",
            "layoutCueWaveformHelp",
            "layoutCueSaveStatus",
        ):
            self.assertEqual(html.count(f'id="{element_id}"'), 1)
        self.assertIn("function layoutCueUpdateDraft(next)", html)
        self.assertIn("async function saveLayoutCueText()", html)
        self.assertIn("expected_project_root:currentProject.root||null", html)
        self.assertIn("if(Number(data.result?.updated_count||0)!==1)", html)
        self.assertIn("layoutCueDrafts=new Map()", html)
        self.assertIn("layoutCorpusPersistable=data.persistable===true;", html)
        self.assertIn("layoutCueSaveInFlight||layoutCueTimingSaveInFlight||!layoutCorpusPersistable", html)
        self.assertIn("layoutCueSelect(null); // Scratch trial text", html)
        self.assertIn("layoutStressSample.word_alignments=null", html)
        self.assertIn("正在获取原始语音并校验 WAV", html)
        self.assertIn("响应不是合法的 WAV 文件", html)
        self.assertIn("beforeunload", html)
        self.assertIn("function layoutCuePrepareWaveform(blob,identity)", html)
        self.assertIn("function layoutCueSeekAudio(fraction)", html)
        self.assertIn("event.key==='ArrowDown'", html)
        self.assertIn("layoutCorpusPersistable=data.persistable===true;", html)
        self.assertIn("layoutCorpusRefreshBtn", html)


    def test_cue_timing_editor_has_real_backend_contract(self) -> None:
        html = self.html
        for name in (
            "layoutCueTimingStart",
            "layoutCueTimingEnd",
            "layoutCueTimingSaveBtn",
            "layoutCueTimingRevertBtn",
            "layoutCueTimingResetBtn",
            "layoutCueTimingStatus",
            "layoutCueTimingBadge",
        ):
            self.assertEqual(html.count(f'id="{name}"'), 1)
        self.assertIn("async function saveLayoutCueTiming(", html)
        self.assertIn("expected_start:state.savedStart", html)
        self.assertIn("expected_end:state.savedEnd", html)
        self.assertIn("layoutCueTimingSaveInFlight", html)
        self.assertIn("layoutTimingCheck(state)", html)
        self.assertIn("state.conflict", html)
        self.assertIn("data-timing-nudge=", html)
        self.assertIn("声轨和原始时间轴不变", html)



if __name__ == "__main__":
    unittest.main()
