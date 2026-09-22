from __future__ import annotations

import csv
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

from app.jobs import create_job, get_job
from app.pipeline import build_project_v02


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
        self.assertIn('id="blackVideoBtn"', html)
        self.assertIn("'/api/output/black-video'", html)
        self.assertNotIn('id="openOutputBtn"', html)
        self.assertIn("自行或交给智能体编辑", html)
        self.assertIn("重新构建不会预先清空 output", html)
        self.assertIn("Final Products（最终成品）", html)
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
        self.assertIn("自动恢复保护：已保存", html)
        self.assertIn("'/api/recovery/status'", html)
        self.assertIn("删除项目不会删除该恢复包", html)
        self.assertIn('id="assOutputBtn"', html)
        self.assertIn("'/api/output/ass'", html)
        self.assertIn("增量官方中文</span>", html)
        self.assertIn("API 补译</span>", html)
        self.assertNotIn("缺失目标文本</span>", html)



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
        self.assertNotIn("document.addEventListener('keydown'", html)
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


if __name__ == "__main__":
    unittest.main()
