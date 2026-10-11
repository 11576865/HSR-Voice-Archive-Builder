"""Fault-injected incremental publication and conservative rollback tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.project import create_project, load_project, PROJECT_FILENAME, update_project
from app.remote_update_publish import (
    capture_update_boundary, list_remote_revisions, publish_remote_revision,
    revision_status, rollback_remote_revision,
)
from app.security import api_token
from app.server import app, _set_active, _clear_active


class RevisionPublicationTests(unittest.TestCase):
    def setUp(self):
        _clear_active()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch_state = patch("app.project.STATE_FILE", self.root / "state.json")
        self.patch_state.start()
        self.addCleanup(self.patch_state.stop)

        (self.root / "wavs").mkdir()
        (self.root / "wavs" / "original.wav").write_bytes(b"original voice")
        (self.root / "index.csv").write_text(
            "filename,source_member_id,english\noriginal.wav,original.wav,Hello\n",
            encoding="utf-8",
        )
        self.out = self.root / "output"
        self.out.mkdir()
        self.manifest = self.out / "manifest.json"
        self.manifest.write_text(
            json.dumps({"entries": [{"index": 1, "filename": "original.wav",
                                     "source_member_id": "original.wav"}]}),
            encoding="utf-8",
        )
        self.config = create_project(
            self.root, name="revisions", index_csv="index.csv",
            wav_source="wavs", output_dir="output",
        )
        self.before = (self.root / PROJECT_FILENAME).read_bytes()
        self.boundary = capture_update_boundary(self.config, self.manifest)
        self.revision = "a" * 32
        self.audio = (self.root / ".generated" / "combined_audio_versions"
                      / f"combined-{self.revision}")
        self.audio.mkdir(parents=True)
        (self.audio / "original.wav").write_bytes(b"original voice")
        (self.audio / "new.wav").write_bytes(b"new voice")
        self.index = self.root / ".generated" / f"quick_index_incremental_{self.revision}.csv"
        self.index.write_text(
            "filename,source_member_id,english\n"
            "original.wav,original.wav,Hello\nnew.wav,new.wav,New\n",
            encoding="utf-8",
        )

    def publish(self):
        return publish_remote_revision(
            self.config, manifest_path=self.manifest,
            expected_boundary=self.boundary,
            combined=self.audio, updated_index=self.index,
            revision_id=self.revision, selected_filenames=["new.wav"],
        )

    def test_atomic_config_commit_journal_and_controlled_rollback(self):
        active = self.publish()
        self.assertEqual(active["state"], "active")
        self.assertTrue(active["can_rollback"])
        self.assertEqual(load_project(self.root).index_csv,
                         str(self.index.relative_to(self.root)))
        self.assertEqual(load_project(self.root).wav_source,
                         str(self.audio.relative_to(self.root)))
        listed = list_remote_revisions(self.config)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["revision_id"], self.revision)
        restored = rollback_remote_revision(
            self.config, self.revision,
            expected_project_sha256=active["current_project_sha256"],
        )
        self.assertEqual(restored["state"], "rolled_back")
        self.assertFalse(restored["can_rollback"])
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)
        self.assertEqual(self.config.wav_source, "wavs")
        self.assertTrue((self.audio / "new.wav").is_file())
        self.assertTrue(self.index.is_file())
        with self.assertRaisesRegex(ValueError, "not the current"):
            rollback_remote_revision(
                self.config, self.revision,
                expected_project_sha256=active["current_project_sha256"],
            )

    def test_refuses_index_manifest_and_project_changes_before_commit(self):
        for change in ("index", "manifest", "project"):
            with self.subTest(change=change):
                with tempfile.TemporaryDirectory() as td:
                    # Keep each branch's independent initial state in this test.
                    index_old = (self.root / "index.csv").read_bytes()
                    manifest_old = self.manifest.read_bytes()
                    config_old = (self.root / PROJECT_FILENAME).read_bytes()
                    if change == "index":
                        (self.root / "index.csv").write_bytes(index_old + b"\n")
                    elif change == "manifest":
                        self.manifest.write_bytes(manifest_old + b" ")
                    else:
                        update_project(self.config, remote_character="changed")
                    try:
                        with self.assertRaisesRegex(ValueError, "changed during"):
                            self.publish()
                        self.assertFalse(
                            (self.root / ".generated" / "incremental_revisions"
                             / f"{self.revision}.json").exists()
                        )
                    finally:
                        (self.root / "index.csv").write_bytes(index_old)
                        self.manifest.write_bytes(manifest_old)
                        (self.root / PROJECT_FILENAME).write_bytes(config_old)
                        self.config = load_project(self.root)

    def test_publish_rejects_wrong_selected_index_member_or_missing_audio(self):
        old_index = self.index.read_bytes()
        cases = [
            ("missing_row",
             "filename,source_member_id,english\n"
             "original.wav,original.wav,Hello\n",
             "does not contain exactly"),
            ("duplicate_row",
             old_index.decode() + "new.wav,new.wav,Duplicate\n",
             "Duplicate published source-member"),
            ("wrong_member",
             old_index.decode().replace("new.wav,new.wav,New",
                                        "new.wav,chapter/new.wav,New"),
             "source-member mismatch"),
            ("missing_filename_column",
             "id,text\n1,hello\n", "missing filename column"),
        ]
        for label, content, reason in cases:
            with self.subTest(label=label):
                self.index.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, reason):
                    self.publish()
                self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)
                self.assertFalse(
                    (self.root / ".generated" / "incremental_revisions"
                     / f"{self.revision}.json").exists()
                )
        self.index.write_bytes(old_index)
        (self.audio / "new.wav").unlink()
        with self.assertRaisesRegex(ValueError, "Published indexed WAV missing"):
            self.publish()
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)

    def test_entire_index_must_resolve_before_configuration_switch(self):
        original_csv = self.index.read_bytes()
        original_wav = self.audio / "original.wav"
        original_bytes = original_wav.read_bytes()
        try:
            original_wav.unlink()
            with self.assertRaisesRegex(ValueError, "Published indexed WAV missing"):
                self.publish()
            self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)
            original_wav.write_bytes(original_bytes)
            for suffix, csv_rows, error in [
                ("wrong_basename",
                 "filename,source_member_id,english\n"
                 "other.wav,original.wav,Hello\n"
                 "new.wav,new.wav,New\n", "filename/member conflict"),
                ("traversal",
                 "filename,source_member_id,english\n"
                 "original.wav,../original.wav,Hello\n"
                 "new.wav,new.wav,New\n", "Unsafe published source-member"),
                ("duplicate_original",
                 original_csv.decode("utf-8") +
                 "original.wav,original.wav,Repeated\n", "Duplicate published source-member"),
            ]:
                with self.subTest(suffix=suffix):
                    self.index.write_text(csv_rows, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, error):
                        self.publish()
                    self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)
        finally:
            self.index.write_bytes(original_csv)
            original_wav.write_bytes(original_bytes)
        self.assertFalse((self.root / ".generated" / "incremental_revisions"
                         / f"{self.revision}.json").exists())

    def test_nested_same_basename_original_members_are_accepted(self):
        for chapter in ("chapterA", "chapterB"):
            folder = self.audio / chapter
            folder.mkdir()
            (folder / "dup.wav").write_bytes(chapter.encode("utf-8"))
        with self.index.open("a", encoding="utf-8") as stream:
            stream.write("dup.wav,chapterA/dup.wav,First\n")
            stream.write("dup.wav,chapterB/dup.wav,Second\n")
        published = self.publish()
        self.assertEqual(published["state"], "active")
        self.assertTrue((self.audio / "chapterA" / "dup.wav").exists())
        self.assertTrue((self.audio / "chapterB" / "dup.wav").exists())

    def test_symlink_as_indexed_original_wav_is_refused(self):
        original = self.audio / "original.wav"
        original.unlink()
        try:
            original.symlink_to(self.root / "wavs" / "original.wav")
        except (NotImplementedError, OSError):
            self.skipTest("Symbolic links are not supported in this test environment")
        with self.assertRaisesRegex(ValueError, "Unsafe published WAV symlink"):
            self.publish()
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)

    def test_save_failure_keeps_prior_config_and_prepared_evidence(self):
        with patch("app.remote_update_publish.save_project",
                   side_effect=OSError("injected disk failure")):
            with self.assertRaisesRegex(OSError, "injected"):
                self.publish()
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)
        self.assertEqual(self.config.wav_source, "wavs")
        status = revision_status(self.config, self.revision)
        self.assertEqual(status["state"], "not_applied")
        self.assertFalse(status["can_rollback"])
        self.assertTrue(self.index.is_file())
        self.assertTrue(self.audio.is_dir())

    def test_crash_after_config_replacement_is_detectable_and_rollback_safe(self):
        from app.remote_update_publish import atomic_write_json
        calls = []
        def fail_second_write(path, data):
            calls.append(str(path))
            if len(calls) == 2:
                raise OSError("simulated journal finalize crash")
            return atomic_write_json(path, data)
        with patch("app.remote_update_publish.atomic_write_json",
                   side_effect=fail_second_write):
            with self.assertRaisesRegex(OSError, "finalize crash"):
                self.publish()
        status = revision_status(self.config, self.revision)
        self.assertEqual(status["state"], "applied_unfinalized")
        self.assertTrue(status["can_rollback"])
        restored = rollback_remote_revision(
            self.config, self.revision,
            expected_project_sha256=status["current_project_sha256"],
        )
        self.assertEqual(restored["state"], "rolled_back")
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)

    def test_completed_build_or_manual_config_edit_blocks_rollback(self):
        active = self.publish()
        self.manifest.write_text(self.manifest.read_text() + " ", encoding="utf-8")
        denied = revision_status(self.config, self.revision)
        self.assertFalse(denied["can_rollback"])
        with self.assertRaisesRegex(ValueError, "rollback denied"):
            rollback_remote_revision(
                self.config, self.revision,
                expected_project_sha256=active["current_project_sha256"],
            )
        self.manifest.write_text(self.manifest.read_text().rstrip(), encoding="utf-8")
        update_project(self.config, remote_character="new-setting")
        self.assertEqual(revision_status(self.config, self.revision)["state"],
                         "superseded_or_modified")
        self.assertFalse(revision_status(self.config, self.revision)["can_rollback"])

    def test_published_index_edit_during_prepare_blocks_commit(self):
        from app.remote_update_publish import atomic_write_json
        def edit_new_index(path, data):
            atomic_write_json(path, data)
            self.index.write_bytes(self.index.read_bytes() + b"late index edit\n")
        with patch("app.remote_update_publish.atomic_write_json",
                   side_effect=edit_new_index):
            with self.assertRaisesRegex(ValueError, "index changed after revision preparation"):
                self.publish()
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before)
        self.assertEqual(
            revision_status(self.config, self.revision)["state"], "not_applied"
        )

    def test_old_index_edit_during_rollback_prepare_blocks_restoration(self):
        from app.remote_update_publish import atomic_write_json
        active = self.publish()
        previous_index = self.root / "index.csv"
        def edit_previous_index(path, data):
            atomic_write_json(path, data)
            if data.get("state") == "rollback_pending":
                previous_index.write_bytes(previous_index.read_bytes() +
                                           b"late concurrent mutation\n")
        with patch("app.remote_update_publish.atomic_write_json",
                   side_effect=edit_previous_index):
            with self.assertRaisesRegex(ValueError, "inputs changed during preparation"):
                rollback_remote_revision(
                    self.config, self.revision,
                    expected_project_sha256=active["current_project_sha256"],
                )
        self.assertEqual(load_project(self.root).wav_source,
                         str(self.audio.relative_to(self.root)))
        self.assertEqual(revision_status(self.config, self.revision)["state"],
                         "applied_unfinalized")
        self.assertFalse(revision_status(self.config, self.revision)["can_rollback"])

    def test_rollback_refuses_tampered_old_or_new_index(self):
        active = self.publish()
        original_index = self.root / "index.csv"
        for edited in (original_index, self.index):
            with self.subTest(index=str(edited)):
                original = edited.read_bytes()
                edited.write_bytes(original + b"changed after publishing\n")
                try:
                    status = revision_status(self.config, self.revision)
                    self.assertFalse(status["can_rollback"])
                    self.assertEqual(
                        status["prior_index_unchanged"], edited != original_index,
                    )
                    self.assertEqual(
                        status["new_index_unchanged"], edited != self.index,
                    )
                    with self.assertRaisesRegex(ValueError, "rollback denied"):
                        rollback_remote_revision(
                            self.config, self.revision,
                            expected_project_sha256=active["current_project_sha256"],
                        )
                    self.assertEqual(load_project(self.root).wav_source,
                                     str(self.audio.relative_to(self.root)))
                finally:
                    edited.write_bytes(original)
        self.assertTrue(revision_status(self.config, self.revision)["can_rollback"])

    def test_rollback_refuses_tampered_prior_config_snapshot(self):
        status = self.publish()
        journal_path = (self.root / ".generated" / "incremental_revisions"
                        / f"{self.revision}.json")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["previous_config"]["remote_character"] = "injected wrong project"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        current = (self.root / PROJECT_FILENAME).read_bytes()
        with self.assertRaisesRegex(ValueError, "previous configuration digest mismatch"):
            rollback_remote_revision(
                self.config, self.revision,
                expected_project_sha256=status["current_project_sha256"],
            )
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(), current)
        self.assertEqual(json.loads(journal_path.read_text())["state"], "committed")

    def test_wrong_preview_sha_or_resource_path_fails_closed(self):
        active = self.publish()
        with self.assertRaisesRegex(ValueError, "changed after rollback preview"):
            rollback_remote_revision(
                self.config, self.revision,
                expected_project_sha256="0" * 64,
            )
        self.assertEqual(revision_status(self.config, self.revision)["state"], "active")

    def test_revision_api_requires_explicit_preview_and_enqueues_rollback(self):
        active = self.publish()
        _set_active(self.config)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = {"X-HSR-Token": api_token()}
            response = client.get("/api/update/revisions", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["revisions"][0]["state"], "active")
            with patch("app.server.create_job",
                       return_value=SimpleNamespace(id="guarded-rollback")) as enqueue:
                invalid = client.post("/api/update/rollback",
                    data={"revision_id": self.revision, "expected_project_sha256": "0" * 64},
                    headers=headers)
                self.assertEqual(invalid.status_code, 400)
                enqueue.assert_not_called()
                response = client.post("/api/update/rollback",
                    data={"revision_id": self.revision,
                          "expected_project_sha256": active["current_project_sha256"]},
                    headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["job"], "guarded-rollback")
                self.assertEqual(enqueue.call_args.args[0], "remote-update-rollback")
        lite = (Path(__file__).resolve().parents[1] / "app/lite_server.py").read_text(
            encoding="utf-8",
        )
        self.assertIn('path == "/api/update/revisions"', lite)
        self.assertIn('path == "/api/update/rollback"', lite)


if __name__ == "__main__":
    unittest.main()
