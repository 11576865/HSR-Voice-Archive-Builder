from __future__ import annotations

import csv
import json
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

from app.jobs import create_job, get_job
from app.launch import (
    _build_mobile_progress_bar_template,
    _build_progress_card_template,
    _extract_progress_card_markup,
)
from app.pipeline import build_project_v02
from app.server import _INDEX_HTML_TEMPLATE, _progress_event_sse


def write_wav(path: Path, frames: int = 80) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * frames)


def write_index(path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["index", "group", "filename", "english", "sha256"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "index": "1",
                "group": "scene",
                "filename": "a.wav",
                "english": "Hello.",
                "sha256": "",
            }
        )


class QuickBuildUiTests(unittest.TestCase):
    def test_control_surface_has_svg_favicon(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        svg = (root / "app" / "static" / "favicon.svg").read_text(encoding="utf-8")
        self.assertIn('rel="icon" type="image/svg+xml" href="/static/favicon.svg?v=1"', html)
        self.assertIn('viewBox="0 0 32 32"', svg)
        self.assertNotIn("<text", svg)

    def test_common_timing_settings_are_visible_before_first_build(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="quickIntroGap"', html)
        self.assertIn('id="quickSameGroupGap"', html)
        self.assertIn('id="quickGroupGap"', html)
        self.assertIn('id="quickAiBudget" class="hidden"', html)
        self.assertIn('id="quickTranslateMissing"', html)
        self.assertIn('id="quickReviewOfficial"', html)
        self.assertIn('id="quickChapterFlac"', html)
        self.assertIn('name="generate_chapter_flac" type="checkbox"', html)
        self.assertIn("d.set('generate_chapter_flac'", html)
        self.assertIn('name="translate_missing" type="checkbox" checked', html)
        self.assertNotIn('name="review_official_target" type="checkbox" checked', html)
        self.assertIn('id="quickReviewOfficial" type="checkbox"', html)
        self.assertIn('name="review_official_target" type="checkbox"', html)
        self.assertIn("data.set('translate_missing'", html)
        self.assertIn("data.set('review_official_target'", html)
        self.assertIn("d.set('intro_gap'", html)
        self.assertIn("d.set('same_group_gap'", html)
        self.assertIn("d.set('group_gap'", html)
        self.assertIn("d.set('review_official_target'", html)

    def test_language_package_roles_and_chinese_translation_visibility_are_clear(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("官方中文语音包（可选）", html)
        self.assertIn("额外语言参考包（可选，不写入成品音频）", html)
        self.assertIn("主音频对应文本语言", html)
        self.assertIn('id="advancedTranslateRow"', html)
        self.assertIn("syncAdvancedTranslationVisibility", html)
        self.assertIn("不进行中文到中文翻译", html)

    def test_finished_files_have_user_facing_descriptions(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("const outputDescriptions=", html)
        self.assertIn("'continuous.flac':'连续播放的无损语音音频'", html)
        self.assertIn("'manifest.json':'完整的机器可读档案", html)
        self.assertIn("'build_report.json':'本次构建的统计", html)
        self.assertIn("'HSR_Voice_Archive_Black.mkv':'黑色视频轨", html)
        self.assertIn("file-subtitle", html)

    def test_workflow_actions_and_incremental_progress_are_visible(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn(':root{color-scheme:dark', html)
        self.assertIn('class="success" id="newProjectBtn"', html)
        self.assertIn('id="applyRemoteBtn" class="success"', html)
        self.assertIn('id="sourceHealthDetails">', html)
        self.assertIn("const active=all.find(j=>['queued','running'].includes(j.state));", html)
        self.assertIn("job.kind==='remote-update-apply'", html)
        self.assertIn("already_applied_count", (
            Path(__file__).resolve().parents[1] / "app" / "remote_index.py"
        ).read_text(encoding="utf-8"))
        self.assertIn('id="reviewPanel"', html)
        self.assertIn('id="submitReviewBtn"', html)
        self.assertIn("x.job.state==='awaiting_input'", html)
        self.assertIn('id="runReportDetails"', html)
        self.assertNotIn('id="blackVideoBtn"', html)
        self.assertIn("label:'生成黑屏 MKV',primary:true,action:startBlackVideo", html)
        self.assertIn("'/api/output/black-video'", html)
        self.assertNotIn('id="openOutputBtn"', html)
        self.assertIn("自行或交给智能体编辑", html)
        self.assertIn("重新构建不会预先清空 output", html)
        self.assertIn('id="artifactBrowser" class="artifact-browser"', html)
        self.assertIn('id="artifactCount" class="badge"', html)
        self.assertIn("Archive / Build Records（档案与构建记录）", html)
        self.assertIn("Project Resources（项目资源）", html)
        self.assertIn("Runtime State（运行状态，高级）", html)
        self.assertIn("p.output_groups||{}", html)
        self.assertIn("p.project_resources||{}", html)
        self.assertIn("p.recovery?.notice", html)
        self.assertIn('id="exportRecoveryBtn"', html)
        self.assertIn('id="importRecoveryFile"', html)
        self.assertIn('id="importRecoveryBtn"', html)
        self.assertIn("'/api/recovery/export'", html)
        self.assertIn("'/api/recovery/import'", html)
        self.assertIn("不包含 WAV、FLAC、MKV 或 API 密钥", html)
        self.assertIn('id="autoRecoveryStatus"', html)
        self.assertIn("自动恢复保护：已校验", html)
        self.assertIn("'/api/recovery/status'", html)
        self.assertIn("删除项目不会删除该恢复包", html)
        self.assertIn("上一代恢复包", html)
        self.assertIn("后端可能拒绝删除", html)
        self.assertNotIn('id="assOutputBtn"', html)
        self.assertIn("label:'生成 ASS',primary:true,action:startAssExport", html)
        self.assertIn("'/api/output/ass'", html)
        # The duplicate build-result stat block is gone. The project overview
        # may still use official/API translation metrics once, as the canonical summary.
        self.assertNotIn('id="archiveStats"', html)
        self.assertNotIn('id="statTotal"', html)
        self.assertNotIn('id="statOfficial"', html)
        self.assertNotIn('id="statIncrementalOfficial"', html)
        self.assertNotIn('id="statApiOnly"', html)
        self.assertNotIn("增量官方中文</span>", html)
        self.assertNotIn("缺失目标文本</span>", html)
        self.assertIn('<span>API 补译</span><b id="overviewApi">—</b>', html)



    def test_core_flac_ass_and_subtitle_editor_ui_contracts(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('type="checkbox" checked disabled> 生成连续 FLAC', html)
        self.assertNotIn('name="generate_ass" type="checkbox"', html)
        self.assertNotIn('name="make_flac" type="checkbox"', html)
        self.assertIn("max-width:1440px", html)
        self.assertIn("@media(max-width:1100px)", html)
        self.assertIn(".sub-workspace>*,.sub-main{min-width:0}", html)
        self.assertNotIn("(Ctrl+Enter)", html)
        self.assertNotIn("(Alt+↑)", html)
        self.assertNotIn("(Alt+↓)", html)
        self.assertIn("document.addEventListener('keydown',event=>{if(event.key==='Escape')setUtilityDrawer(false)});", html)
        self.assertIn('interactive-widget=resizes-content', html)
        self.assertNotIn('@media(pointer:coarse)', html)
        self.assertIn('@media(prefers-reduced-motion:reduce)', html)
        self.assertIn('id="progressTrack" class="progress-track" role="progressbar"', html)
        self.assertIn('role="status" aria-live="polite"', html)
        self.assertIn('id="mobileProgressSlot"', html)
        self.assertIn("syncProgressPlacement", html)
        self.assertIn('type="search" autocomplete="off"', html)
        self.assertNotIn('id="loadSubtitlesBtn"', html)
        self.assertNotIn('id="saveSubtitlesBtn"', html)
        self.assertIn('type="button" class="sub-item ', html)
        self.assertIn("scheduleSubtitleFetch", html)
        self.assertIn("scheduleSubtitleAutosave", html)
        self.assertIn("compositionstart", html)
        self.assertIn("compositionend", html)
        self.assertNotIn("保存并下一条", html)
        self.assertIn("停止输入约 1 秒后自动保存", html)
        self.assertIn("源文件：", html)
        self.assertIn('<details class="sub-ref-card reference-annotation" id="subReferenceCard">', html)
        self.assertNotIn('<details class="sub-ref-card reference-annotation" id="subReferenceCard" open', html)
        self.assertIn('参考语音标注', html)
        self.assertIn('选为 GPT-SoVITS 参考语音', html)
        self.assertIn('id="subOriginalAudio"', html)
        self.assertIn('id="subOriginalAudio" controls preload="metadata"', html)
        self.assertIn("originalAudio.addEventListener('loadedmetadata'", html)
        self.assertIn("originalAudio.addEventListener('canplay'", html)
        self.assertIn("originalAudio.addEventListener('error'", html)
        self.assertIn("if(key&&key===referenceAudioKey)return", html)
        self.assertIn('id="subReferenceSelected"', html)
        self.assertIn('id="subReferenceEmotion"', html)
        self.assertIn('<option value="unmarked">Unmarked</option>', html)
        self.assertIn('<option value="neutral">Neutral</option>', html)
        self.assertIn('<option value="happy">Happy</option>', html)
        self.assertIn('<option value="sad">Sad</option>', html)
        self.assertIn('<option value="angry">Angry</option>', html)
        self.assertIn('<option value="fear">Fear</option>', html)
        self.assertIn('<option value="surprised">Surprised</option>', html)
        self.assertIn('<option value="other">Other</option>', html)
        self.assertNotIn('referenceEmotionSuggestions', html)
        self.assertIn('id="subReferenceIntensity"', html)
        self.assertIn('id="subReferenceIntensityValue" class="small muted">0.50</span>', html)
        self.assertIn('id="subReferenceIntensity" type="range" min="0" max="1" step="0.05" value="0.5"', html)
        self.assertIn('id="subReferenceQuality"', html)
        self.assertIn('A: Recommended reference', html)
        self.assertIn('B: Usable', html)
        self.assertIn('C: Not recommended', html)
        self.assertIn('id="referencePackExportBtn"', html)
        self.assertIn("/export/reference-pack", html)
        self.assertIn("/reference-annotations", html)
        self.assertIn("/subtitles/'+encodeURIComponent(sub.id)+'/audio", html)
        self.assertIn("URL.createObjectURL(blob)", html)
        self.assertIn("if(!blob.size)throw new Error('后端返回了空音频文件')", html)
        server_py = (Path(__file__).resolve().parents[1] / "app" / "server.py").read_text(encoding="utf-8")
        lite_server_py = (Path(__file__).resolve().parents[1] / "app" / "lite_server.py").read_text(encoding="utf-8")
        self.assertIn("media-src 'self' blob:", server_py)
        self.assertIn("media-src 'self' blob:", lite_server_py)
        self.assertIn("body:JSON.stringify({subtitles:[{id:sentId,final_chs:sentText}]})", html)
        self.assertNotIn('id="subUseOfficialBtn"', html)
        self.assertNotIn('id="subUseApiBtn"', html)
        self.assertIn('id="subResetBtn"', html)
        self.assertNotIn('id="subConfirmBtn"', html)
        self.assertNotIn('id="subOpenLayoutBtn"', html)
        self.assertNotIn('id="subNextAttentionBtn"', html)
        self.assertNotIn('<option value="unreviewed">未人工确认</option>', html)
        self.assertNotIn('<option value="confirmed">已人工确认</option>', html)
        self.assertNotIn('<option value="overflow">ASS 排版失败</option>', html)
        self.assertNotIn("openCurrentSubtitleInLayout", html)
        self.assertIn('id="quickIntroGap" type="number" min="0" step="0.01" value="9.00"', html)
        self.assertIn('id="quickSameGroupGap" type="number" min="0" step="0.01" value="1.50"', html)
        self.assertIn('id="quickGroupGap" type="number" min="0" step="0.01" value="3.00"', html)
        self.assertNotIn('id="subWarningBox"', html)
        self.assertNotIn('updateCharCounterAndWarnings', html)
        self.assertIn('.sub-item-tags{display:flex;align-items:center;gap:4px;min-height:18px;overflow:hidden}', html)
        self.assertIn('class="actions project-actions"', html)
        self.assertIn('.project-actions>button{flex:0 0 auto;font-size:13px}', html)
        desktop_pos = html.index('.sub-workspace{display:grid;grid-template-columns:minmax(220px,300px) minmax(0,1fr)')
        mobile_pos = html.index('.sub-workspace{grid-template-columns:minmax(0,1fr);min-height:0}')
        self.assertGreater(mobile_pos, desktop_pos)
        self.assertNotIn('.actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}', html)

    def test_proofreading_productivity_shortcuts_and_diff_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        # The later dense proofreading redesign removed the character diff panel
        # and the separate confirmation workflow; text edits autosave directly.
        self.assertNotIn('id="subDiffPanel"', html)
        self.assertNotIn("function renderSubtitleDiff()", html)
        self.assertIn('id="subShortcutHelp" class="sub-shortcuts"', html)
        self.assertIn("function toggleCurrentSubtitleAudio()", html)
        self.assertNotIn("function confirmCurrentSubtitleFromShortcut()", html)
        self.assertIn("subtitleEditor.addEventListener('keydown'", html)

        self.assertNotIn("e.altKey&&e.key.toLowerCase()==='a'", html)
        self.assertNotIn("e.altKey&&e.key.toLowerCase()==='t'", html)
        self.assertNotIn("e.altKey&&e.key.toLowerCase()==='n'", html)
        self.assertIn("e.altKey&&e.code==='Space'", html)
        self.assertNotIn("(e.ctrlKey||e.metaKey)&&e.key==='Enter'", html)
        self.assertNotIn("setSubtitleConfirmation", html)
        self.assertNotIn("goToNextAttentionSubtitle", html)
        self.assertNotIn("confirmedCount=loadedSubtitles.filter(sub=>sub.confirmed).length", html)
        self.assertNotIn("已确认</span>", html)

    def test_application_shell_v3_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("/* Application shell v3", html)
        self.assertIn("--rail-width:132px", html)
        self.assertIn("--topbar-height:56px", html)
        self.assertIn("position:fixed;left:0;top:var(--topbar-height);bottom:0", html)
        self.assertIn("margin:0 0 0 var(--rail-width)", html)
        self.assertIn("grid-template-columns:minmax(0,1fr) 292px", html)
        self.assertIn('@media(max-width:960px)', html)
        self.assertIn(':root{--rail-width:0px}', html)
        self.assertIn('class="workspace-index">01</span><span>档案</span>', html)
        self.assertIn('class="workspace-index">02</span><span>校对</span>', html)
        self.assertIn('class="workspace-index">03</span><span>排版</span>', html)
        self.assertIn('class="workspace-index">04</span><span>导出</span>', html)
        self.assertIn('class="workspace-index">05</span><span>更新</span>', html)
        self.assertIn("LOCAL ARCHIVE WORKBENCH · AUDIO / SUBTITLE / UPDATE", html)
        self.assertIn(".card{", html)
        self.assertIn("border-radius:4px", html)
        self.assertIn(".job-center-copy{flex-direction:row", html)

    def test_project_overview_job_center_and_artifact_browser_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="jobCenterBar" class="job-center-bar"', html)
        self.assertIn('id="projectOverview" class="project-overview hidden"', html)
        self.assertIn('id="projectSettingsPanel" class="project-settings hidden"', html)
        self.assertIn('id="overviewBuildBtn"', html)
        self.assertIn('id="overviewReviewBtn"', html)
        self.assertIn('id="overviewUpdateBtn"', html)
        self.assertIn('id="overviewArtifactsBtn"', html)
        self.assertIn('id="artifactBrowser" class="artifact-browser"', html)
        self.assertIn("renderProjectOverview(p)", html)
        self.assertIn("renderArtifactBrowser(p,finalProducts,outputMap)", html)
        self.assertIn("renderJobCenterBar(active,projectJobs)", html)
        self.assertIn("copyTextValue(path)", html)
        self.assertIn("data.set('generate_chapter_flac',e.target.elements.generate_chapter_flac?.checked?'true':'false')", html)

        create_start = html.index('<form id="createForm">')
        create_end = html.index('</form>', create_start)
        create_form = html[create_start:create_end]
        self.assertEqual(create_form.count('name="generate_chapter_flac"'), 1)

        self.assertNotIn('background:#fafafa', html)
        self.assertNotIn('border-bottom:1px solid #eee', html)

    def test_font_browser_and_preview_background_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        server_py = (
            Path(__file__).resolve().parents[1] / "app" / "server.py"
        ).read_text(encoding="utf-8")
        lite_server_py = (
            Path(__file__).resolve().parents[1] / "app" / "lite_server.py"
        ).read_text(encoding="utf-8")

        self.assertIn('id="fontBrowserDialog" class="font-browser-dialog"', html)
        self.assertIn('data-font-target="prevChsFont"', html)
        self.assertIn('data-font-target="prevPriFont"', html)
        self.assertIn("'/api/subtitle-layout/fonts?q='", html)
        self.assertIn("'/api/subtitle-layout/font-file?family='", html)
        self.assertIn("new FontFace(alias,bytes)", html)
        self.assertIn("document.fonts.add(face)", html)
        self.assertIn("browserPreviewFontName(", html)
        self.assertIn('id="previewBackgroundMode"', html)
        self.assertIn('<option value="image">本地图片</option>', html)
        self.assertIn('id="previewBackgroundFile" type="file"', html)
        self.assertIn("URL.createObjectURL(file)", html)
        self.assertIn("URL.revokeObjectURL(previewBackgroundObjectUrl)", html)
        self.assertIn("背景仅存在于当前浏览器会话", html)
        self.assertNotIn("bgGradient", html)
        self.assertIn("img-src 'self' data: blob:", server_py)
        self.assertIn("img-src 'self' data: blob:", lite_server_py)
        self.assertIn("font-src 'self' data: blob:", server_py)
        self.assertIn("font-src 'self' data: blob:", lite_server_py)
        self.assertIn('@app.get("/api/subtitle-layout/font-file")', server_py)
        self.assertIn('path == "/api/subtitle-layout/font-file"', lite_server_py)
        self.assertIn('path == "/api/subtitle-layout/fonts"', lite_server_py)

    def test_workbench_visual_structure_uses_canvas_and_inspector_hierarchy(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('class="row preview-copy-fields"', html)
        self.assertIn('class="preview-controls"', html)
        self.assertEqual(html.count('class="inspector-group"'), 7)
        self.assertIn('id="prevOutlineWidth"', html)
        self.assertIn('id="prevShadowDepth"', html)
        self.assertIn('id="prevBlurRadius"', html)
        self.assertIn('id="prevCardOpacity"', html)
        self.assertIn('id="prevFadeIn"', html)
        self.assertIn('id="prevFadeOut"', html)
        self.assertIn('id="prevAudioAwareFade"', html)
        self.assertIn('id="prevEnableSoftEntry"', html)
        self.assertIn('id="prevSoftEntryScale"', html)
        self.assertIn('id="prevSoftEntryBlur"', html)
        self.assertIn('id="prevSoftEntryMs"', html)
        self.assertIn('id="prevKaraokeMode"', html)
        self.assertIn('<option value="kf">平滑扫光（\\kf）</option>', html)
        self.assertIn('<option value="clip">动态裁剪扫光（\\clip + \\t）</option>', html)
        self.assertIn('id="wordAlignmentStatus"', html)
        self.assertIn('id="refreshWordAlignmentBtn"', html)
        self.assertIn('id="generateWordAlignmentBtn"', html)
        self.assertIn('id="importWordAlignmentBtn"', html)
        self.assertIn('id="wordAlignmentFile"', html)
        self.assertIn('id="wordAlignmentProviderStatus"', html)
        self.assertIn("async function generateLocalWordAlignments()", html)
        self.assertIn("'/word-alignments/generate'", html)
        self.assertIn("python -m pip install whisperx", html)
        self.assertIn('id="prevKaraokePreviewTime"', html)
        self.assertIn('id="prevKaraokePreviewTimeVal"', html)
        self.assertIn("async function loadWordAlignmentDiagnostics()", html)
        self.assertIn("async function importWordAlignmentFile(file)", html)
        self.assertIn("async function loadWordAlignmentDiagnostics()", html)
        self.assertIn("local_provider", (
            Path(__file__).resolve().parents[1] / "app" / "server.py"
        ).read_text(encoding="utf-8"))
        self.assertIn("generate_local_word_alignments", (
            Path(__file__).resolve().parents[1] / "app" / "local_word_alignment.py"
        ).read_text(encoding="utf-8"))
        self.assertIn("'/word-alignments'", html)
        self.assertIn("word_alignments:Array.isArray(layoutStressSample?.word_alignments)", html)
        self.assertIn("preview_duration_seconds:previewDuration", html)
        self.assertIn("preview_timestamp:Math.max(0.01,previewDuration*previewPercent)", html)
        self.assertIn('id="prevEnableArchiveHud"', html)
        self.assertIn('id="prevArchiveHudSize"', html)
        self.assertIn('id="prevArchiveHudOpacity"', html)
        self.assertIn("Archive HUD", html)
        self.assertIn("平滑淡入淡出", html)
        self.assertNotIn("淡入淡出 / 动态进入", html)
        self.assertIn('class="preview-stage" id="previewCanvasContainer"', html)
        self.assertIn('class="preview-workbench"', html)
        self.assertIn('class="preview-inspector" aria-label="字幕排版参数与操作"', html)
        self.assertIn(
            '.preview-workbench{\n  position:relative;\n  min-width:0;\n  padding-right:350px;',
            html,
        )
        self.assertIn(
            '.preview-inspector{\n  position:absolute;\n  top:0;\n  right:0;\n  bottom:0;\n  width:350px;',
            html,
        )
        self.assertIn('overflow-y:auto;', html)
        self.assertIn(
            'body[data-workspace="layout"] #subtitleLayoutPreviewCard{\n  display:block!important;\n  background:var(--layout-shell)!important;',
            html,
        )
        self.assertIn(
            '.preview-workbench .preview-stage{\n  width:100%!important;\n  margin:0!important;\n  aspect-ratio:16/9;\n  background:var(--layout-canvas)!important;',
            html,
        )
        self.assertIn('class="sub-workspace proofreading-workspace"', html)
        self.assertIn('class="sub-ref-card sub-audio-card" id="subAudioCard"', html)
        self.assertIn('class="actions proofreading-actions"', html)
        self.assertNotIn('class="actions proofreading-secondary-actions"', html)
        self.assertIn('class="sub-nav-bar proofreading-nav-actions"', html)
        self.assertIn('@media(max-width:1000px)', html)

    def test_layout_light_theme_has_authoritative_tokens(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("/* Subtitle layout workbench theme + geometry v9", html)
        self.assertIn("--layout-shell:var(--surface-primary)", html)
        self.assertIn("--layout-inspector:var(--surface-utility)", html)
        self.assertIn("--layout-input:var(--surface-input)", html)
        self.assertIn("--layout-canvas:#05070b", html)
        self.assertIn(':root[data-theme="light"]{\n  --layout-shell:#ffffff;', html)
        self.assertIn("--layout-inspector:#f5f8fb", html)
        self.assertIn("--layout-input:#ffffff", html)
        self.assertIn("background:var(--layout-inspector)!important", html)
        self.assertIn("background:var(--layout-input)!important", html)
        self.assertIn("color:var(--layout-text)!important", html)
        self.assertIn("border-color:var(--layout-border)!important", html)

        # The video canvas is intentionally dark in both themes.
        self.assertIn("background:var(--layout-canvas)!important", html)

        # Legacy dark workbench surfaces must no longer be authoritative.
        self.assertNotIn(
            'body[data-workspace="review"] .card,body[data-workspace="layout"] .card{background:#15191c',
            html,
        )
        self.assertNotIn(
            '.preview-copy-fields{grid-area:copy;grid-template-columns:1fr!important;padding:14px 16px 4px;margin:0!important;background:#121619}',
            html,
        )
        self.assertNotIn(
            '.preview-controls{grid-area:controls;display:block!important;margin:0!important;padding:0 16px;background:#121619}',
            html,
        )
        self.assertNotIn(
            '.preview-actions{grid-area:actions;display:block!important;margin:0!important;padding:8px 16px 14px;background:#121619}',
            html,
        )

    def test_theme_coherence_and_single_layer_utility_drawer_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        # Theme chrome and light-mode overrides must stay coherent.
        self.assertIn('<meta name="theme-color" content="#0d1725">', html)
        self.assertIn("themeMeta.setAttribute('content',theme==='light'?'#eef4f8':'#0d1725')", html)
        self.assertIn("--surface-terminal:#f1f5f9", html)
        self.assertIn(
            "button:disabled{\n  background:var(--surface-subtle)!important;",
            html,
        )
        self.assertIn(
            "pre,\n.log{\n  background:var(--surface-terminal)!important;",
            html,
        )

        # Drawer close control must remain visible and reopening starts at top.
        self.assertIn(
            ".utility-drawer-head{\n  position:sticky!important;",
            html,
        )
        self.assertIn("drawer.scrollTop=0;", html)
        self.assertIn("close?.focus({preventScroll:true})", html)

        # Diagnostic and LAN control use one outer collapsible card only.
        self.assertIn(
            '<div class="utility-panel-body" id="diagnosticLog">',
            html,
        )
        self.assertNotIn('<details id="diagnosticLog">', html)
        self.assertIn(
            'class="card utility-card utility-network" id="localControlCard"',
            html,
        )
        self.assertNotIn(
            '<summary>本地优先 / 局域网控制</summary>',
            html,
        )
        self.assertIn(
            "const panel=document.getElementById('diagnosticCard');",
            html,
        )

        # Utility panels retain separate semantic accents.
        self.assertIn("--task:#60a5fa", html)
        self.assertIn("--diagnostic:#a78bfa", html)
        self.assertIn("--network:#2dd4bf", html)

    def test_desktop_workspace_width_authority(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("/* Workspace width authority v10", html)
        self.assertIn('@media (min-width:1201px)', html)
        self.assertIn(
            'body[data-workspace="project"] .grid{\n    width:min(100%,1640px)!important;',
            html,
        )
        self.assertIn(
            'body[data-workspace="review"] .grid{\n    width:min(100%,1760px)!important;',
            html,
        )
        self.assertIn(
            'body[data-workspace="layout"] .grid{\n    width:100%!important;\n    max-width:none!important;',
            html,
        )
        self.assertIn(
            '--layout-inspector-width:clamp(340px,20vw,430px)',
            html,
        )
        self.assertIn(
            'body[data-workspace="export"] .grid{\n    width:min(100%,1760px)!important;',
            html,
        )
        self.assertIn(
            'body[data-workspace="update"] .grid{\n    width:min(100%,1900px)!important;',
            html,
        )

    def test_mobile_single_column_workbench_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("Mobile layout authority v6", html)
        self.assertIn(
            "@media (max-width:960px), (hover:none) and (pointer:coarse) and (max-width:1200px)",
            html,
        )
        self.assertIn(
            "#sideStack.utility-drawer{\n  flex-direction:column!important;",
            html,
        )
        self.assertIn(
            "#sideStack.utility-drawer > .utility-drawer-head,\n  #sideStack.utility-drawer > .card",
            html,
        )
        self.assertIn(
            ".sub-workspace,\n  .proofreading-workspace{\n    display:flex!important;\n    flex-direction:column!important;",
            html,
        )
        self.assertIn("max-height:none!important;\n    height:auto!important;\n    overflow:visible!important;", html)
        self.assertIn("-webkit-line-clamp:2;", html)
        self.assertIn(
            ".export-context,\n  .export-lanes,\n  .update-toolbar,\n  .update-review-tools,",
            html,
        )
        self.assertIn(
            "#subtitleLayoutPreviewCard .preview-controls,",
            html,
        )
        self.assertIn(
            ".preview-inspector{\n    position:static;\n    order:2;\n    width:100%;",
            html,
        )
        self.assertIn("body.utility-drawer-open{\n    overflow:hidden!important;", html)

    def test_workflow_density_review_update_export_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        # Proofreading stays focused on text/audio review. Layout QA belongs to
        # the dedicated workbench and runs against a real project stress sample.
        positions = [
            html.index('id="subSourceText"'),
            html.index('id="subOriginalAudio"'),
            html.index('id="subFinalEditor"'),
        ]
        self.assertTrue(all(position >= 0 for position in positions))
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('id="subDiffPanel"', html)
        self.assertNotIn("function renderSubtitleDiff()", html)
        self.assertNotIn("reviewReferences.before(reviewEditor)", html)
        self.assertIn('id="subInitialSourceBadge"', html)
        self.assertIn("function subtitleInitialText(", html)
        self.assertNotIn('id="subLayoutDiagnostic"', html)
        self.assertNotIn('class="proof-step proof-step-layout"', html)
        self.assertIn('class="sub-nav-bar proofreading-nav-actions"', html)
        self.assertIn('id="layoutCorpusCheckStatus"', html)
        self.assertIn("function loadLayoutStressSample()", html)
        self.assertIn("function subtitleLayoutStressScore(", html)
        self.assertNotIn('id="prevPresetLongText"', html)
        self.assertNotIn('id="prevPresetCollision"', html)
        self.assertIn('id="prevChsSize" min="38" max="64" value="48"', html)
        self.assertIn('id="prevMarginH" min="0" max="30" value="3"', html)
        self.assertIn('id="prevMarginV" min="0" max="20" value="5"', html)

        # Utility information is an on-demand drawer, not a permanent column.
        self.assertIn('class="stack utility-drawer" id="sideStack"', html)
        self.assertIn("function setUtilityDrawer(", html)
        self.assertIn('id="utilityCloseBtn"', html)
        self.assertIn("utility-drawer-open", html)

        # Incremental review can be searched, classified, grouped and folded.
        for element_id in (
            "updateSearchQuery",
            "updateCategorySelect",
            "updateGroupBy",
            "updateExpandGroupsBtn",
            "updateCollapseGroupsBtn",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("function updateRowSearchText(", html)
        self.assertIn("function updateGroupInfo(", html)
        self.assertIn("className='update-group'", html)
        self.assertIn("className='update-change'", html)

        # Export workspace contains only export-specific controls; archive products stay in Archive.
        self.assertIn('class="card hidden export-workbench" id="gptSovitsCard"', html)
        self.assertNotIn('id="exportArtifactSummary"', html)
        self.assertNotIn('id="exportGoArchiveBtn"', html)
        self.assertNotIn('id="exportGoReviewBtn"', html)
        self.assertNotIn('id="exportGoLayoutBtn"', html)
        self.assertIn("function renderExportWorkspace(", html)

    def test_theme_switcher_and_collapse_regression_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        for choice in ("light", "dark", "system"):
            self.assertIn(f'data-theme-choice="{choice}"', html)
        self.assertIn("hsr-ui-theme-v1", html)
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn(':root[data-theme="light"]', html)
        self.assertIn("function applyTheme(", html)
        self.assertIn("systemThemeQuery.addEventListener('change'", html)

        # Regression guards for the first collapsible-shell implementation.
        self.assertIn("savedPanelState=state;", html)
        self.assertIn("let autoPinnedPanelKey=null", html)
        self.assertIn("autoPinnedPanelKey='progressCard'", html)
        self.assertIn("preservePanelPin=false", html)
        self.assertIn("lastTaskAttentionState=taskNeedsAttention()", html)
        self.assertIn("nextAttention!==lastTaskAttentionState", html)

    def test_high_contrast_shell_and_panel_collapse_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        for element_id in (
            "panelModeControl",
            "panelAutoModeBtn",
            "panelManualModeBtn",
            "panelExpandAllBtn",
            "panelCollapseSecondaryBtn",
        ):
            self.assertIn(f'id="{element_id}"', html)

        self.assertIn("PANEL_MODE_KEY='hsr-ui-panel-mode-v1'", html)
        self.assertIn("PANEL_STATE_KEY='hsr-ui-panel-state-v1'", html)
        self.assertIn("function enhanceCollapsiblePanels()", html)
        self.assertIn("function applyAutoPanelState(", html)
        self.assertIn("function applyManualPanelState()", html)
        self.assertIn("function taskNeedsAttention()", html)
        self.assertIn("panel.dataset.uiTier=tier", html)
        self.assertIn("panel.classList.add('ui-collapsible')", html)
        self.assertIn(".card[data-ui-tier=\"primary\"]", html)
        self.assertIn(".card[data-ui-tier=\"utility\"]", html)
        self.assertIn(".card.ui-collapsible.ui-collapsed", html)
        self.assertIn('class="app-brand-mark"', html)
        self.assertIn("LOCAL ARCHIVE WORKBENCH", html)

    def test_incremental_update_review_workbench_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("<h2>增量更新审查</h2>", html)
        self.assertIn('id="updatePlanState" class="badge"', html)
        self.assertIn('id="updateReviewList" class="update-review-list"', html)
        self.assertIn('id="updateOnlyActionableBtn"', html)
        self.assertIn('id="updateCopySummaryBtn"', html)
        for category in (
            "exact_existing",
            "variant_of_existing",
            "new_logical",
            "changed_existing",
            "ambiguous",
            "all",
        ):
            self.assertIn(f'data-update-filter="{category}"', html)

        self.assertIn("let currentUpdatePlan=null", html)
        self.assertIn("function renderUpdateReview()", html)
        self.assertIn("function updatePlanSummaryText(plan)", html)
        self.assertIn("currentUpdatePlan=plan", html)
        self.assertIn("renderUpdateReview();", html)
        self.assertIn("CURRENT", html)
        self.assertIn("REMOTE", html)
        self.assertIn("audio_sha256", html)
        self.assertIn("不会被“应用新增”静默覆盖", html)
        self.assertIn("不会自动下载或归入现有条目", html)
        self.assertIn("只有明确的新增条目会自动下载", html)

        # Existing update actions and counters remain the integration contract.
        for element_id in (
            "remoteCharacter",
            "scanRemoteBtn",
            "applyRemoteBtn",
            "uExisting",
            "uVariant",
            "uNew",
            "uChanged",
            "uAmbiguous",
            "uRows",
            "candidatePath",
            "scanLocalBtn",
        ):
            self.assertIn(f'id="{element_id}"', html)

    def test_job_polling_retries_and_bypasses_get_cache(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("opts.cache='no-store'", html)
        self.assertIn("pollTimer=setTimeout(poll,delay)", html)
        self.assertIn("进度连接暂时中断，正在自动重试", html)
        self.assertNotIn("catch(e){clearInterval(pollTimer);setLog(String(e))}", html)

class BuildProgressTests(unittest.TestCase):
    def test_build_jobs_report_progress_and_reject_concurrent_build(self) -> None:
        release = threading.Event()

        def run(report_progress):
            report_progress("translation", "3/6 AI 翻译：批次 1/2", 3, 6)
            release.wait(2)
            return {"ok": True}

        job = create_job("build", run, with_progress=True)
        deadline = time.time() + 2
        state = None
        while time.time() < deadline:
            state = get_job(job.id)
            if state and state["phase"] == "translation":
                break
            time.sleep(0.01)

        self.assertIsNotNone(state)
        self.assertEqual(state["progress_current"], 3)
        self.assertEqual(state["progress_total"], 6)
        self.assertIn("批次 1/2", state["message"])

        with self.assertRaisesRegex(RuntimeError, "已有构建任务正在运行"):
            create_job("quick-build", lambda: None)

        release.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            state = get_job(job.id)
            if state and state["state"] == "succeeded":
                break
            time.sleep(0.01)
        self.assertEqual(state["state"], "succeeded")

    def test_pipeline_reports_all_major_stages(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            write_wav(wavs / "a.wav")
            index = root / "index.csv"
            write_index(index)
            events: list[tuple[str, str, int, int]] = []

            report = build_project_v02(
                index,
                wavs,
                root / "output",
                make_flac=False,
                progress_callback=lambda phase, message, current, total: events.append(
                    (phase, message, current, total)
                ),
            )

            phases = [event[0] for event in events]
            self.assertEqual(
                phases,
                ["prepare", "metadata", "translation", "manifest", "audio", "final"],
            )
            self.assertEqual(events[-1][2:], (6, 6))
            self.assertEqual(report["count_total"], 1)


class TestProgressUI(unittest.TestCase):
    def test_progress_card_markup_extraction(self) -> None:
        extracted = _extract_progress_card_markup(_INDEX_HTML_TEMPLATE)

        self.assertIn('id="progressCard"', extracted)
        self.assertIn('id="progressTrack"', extracted)
        self.assertIn('id="mobileProgressSlot"', extracted)

    def test_progress_card_template_uses_canonical_ids(self) -> None:
        card = _build_progress_card_template()

        self.assertIn('id="progressCard"', card)
        self.assertIn('id="progressTrack"', card)
        self.assertIn('id="progressTitle"', card)
        self.assertIn('id="progressDesc"', card)
        self.assertIn('id="progressMeta"', card)
        self.assertIn('id="progressLog"', card)
        self.assertIn('id="mobileProgressSlot"', card)

    def test_mobile_progress_bar_template_uses_canonical_ids(self) -> None:
        bar = _build_mobile_progress_bar_template()

        self.assertIn('id="mobileProgressBar"', bar)
        self.assertIn('id="mobileProgressTrack"', bar)
        self.assertIn('id="mobileProgressTitle"', bar)
        self.assertIn('id="mobileProgressDesc"', bar)
        self.assertIn('id="mobileProgressMeta"', bar)

    def test_progress_event_sse_formatting(self) -> None:
        job = {
            "title": "测试任务",
            "progress_percent": 42.5,
            "status_text": "处理中",
            "message": "正在处理第 3/10 条语音",
            "log": "2026-03-31 00:00:00 [INFO] 测试日志",
        }

        normal_sse = _progress_event_sse(job, full_payload=True)
        self.assertTrue(normal_sse.startswith("event: progress\ndata: "))
        normal_data = json.loads(normal_sse.split("data: ", 1)[1].strip())
        self.assertEqual(normal_data["progress_percent"], 42.5)
        self.assertEqual(normal_data["log"], job["log"])

        heartbeat_sse = _progress_event_sse(job, heartbeat_log=True)
        self.assertTrue(heartbeat_sse.startswith("event: progress\ndata: "))
        heartbeat_data = json.loads(heartbeat_sse.split("data: ", 1)[1].strip())
        self.assertEqual(heartbeat_data["progress_percent"], 42.5)
        self.assertNotIn("log", heartbeat_data)

    def test_index_html_contains_progress_ui(self) -> None:
        html = _INDEX_HTML_TEMPLATE

        self.assertIn('id="progressCard" class="card utility-card" role="region" aria-label="处理进度"', html)
        self.assertIn('@media(prefers-reduced-motion:reduce)', html)
        self.assertIn('id="progressTrack" class="progress-track" role="progressbar"', html)
        self.assertIn('role="status" aria-live="polite"', html)
        self.assertIn('id="mobileProgressSlot"', html)
        self.assertIn("syncProgressPlacement", html)
        self.assertIn('type="search" autocomplete="off"', html)
        self.assertNotIn('id="loadSubtitlesBtn"', html)
        self.assertNotIn('id="saveSubtitlesBtn"', html)
        self.assertIn('type="button" class="sub-item ', html)
        self.assertIn("activeSubId", html)
        self.assertIn("highlightText", html)
        self.assertIn("scheduleSubtitleFetch", html)
        self.assertIn("scheduleSubtitleAutosave", html)
        self.assertIn("compositionstart", html)
        self.assertIn("compositionend", html)
        self.assertNotIn("保存并下一条", html)
        self.assertIn("停止输入约 1 秒后自动保存", html)
        self.assertIn("源文件：", html)
        self.assertIn(
            "body:JSON.stringify({subtitles:[{id:sentId,final_chs:sentText}]})",
            html,
        )
        self.assertNotIn("confirmed:!!targetSub.confirmed", html)
        self.assertIn('id="quickIntroGap" type="number" min="0" step="0.01" value="9.00"', html)
        self.assertIn('id="quickSameGroupGap" type="number" min="0" step="0.01" value="1.50"', html)
        self.assertIn('id="quickGroupGap" type="number" min="0" step="0.01" value="3.00"', html)
        self.assertNotIn('id="subWarningBox"', html)
        self.assertNotIn('updateCharCounterAndWarnings', html)
        self.assertIn(
            '.sub-item-tags{display:flex;align-items:center;gap:4px;min-height:18px;overflow:hidden}',
            html,
        )
        self.assertIn('class="actions project-actions"', html)
        self.assertIn('.project-actions>button{flex:0 0 auto;font-size:13px}', html)
        desktop_pos = html.index(
            '.sub-workspace{display:grid;grid-template-columns:minmax(220px,300px) minmax(0,1fr)'
        )
        mobile_pos = html.index(
            '.sub-workspace{grid-template-columns:minmax(0,1fr);min-height:0}'
        )
        self.assertGreater(mobile_pos, desktop_pos)
        self.assertNotIn(
            '.actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}',
            html,
        )

    def test_job_polling_retries_and_bypasses_get_cache(self) -> None:
        html = _INDEX_HTML_TEMPLATE

        self.assertIn("cache:'no-store'", html)
        self.assertIn("let attempts=0;while(attempts<5)", html)
        self.assertIn("await new Promise(r=>setTimeout(r,1000*(attempts+1)));", html)

    def test_subtitle_layout_default_preset_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        project_py = (root / "app" / "project.py").read_text(encoding="utf-8")
        config_py = (root / "subtitle_layout" / "config.py").read_text(encoding="utf-8")
        for source in (project_py, config_py):
            self.assertIn("48", source)
            self.assertIn("0.03", source)
            self.assertIn("0.05", source)

    def test_index_html_contains_a11y_and_ux_improvements(self) -> None:
        index_path = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        html = index_path.read_text(encoding="utf-8")

        self.assertIn('class="skip-link"', html)
        self.assertIn('<a href="#mainContent" class="skip-link">跳过导航进入主内容</a>', html)
        self.assertIn('<main id="mainContent">', html)
        self.assertIn('id="subPrevBtn" aria-label="上一条字幕 (Alt+Up)"', html)
        self.assertIn('id="subNextBtn" aria-label="下一条字幕 (Alt+Down)"', html)
        self.assertIn("subtitleEditor.addEventListener('keydown'", html)


if __name__ == "__main__":
    unittest.main()
