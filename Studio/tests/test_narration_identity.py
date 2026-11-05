import json
import tempfile
import threading
import tkinter as tk
import unittest
from queue import Queue
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

from docurandom.narration_identity import narration_digest, package_narration, validate_narration_snapshot, validate_project_narration
from docurandom.ui import DocuRandomApp, SmartVoiceRequest
from docurandom.models import RenderSettings
from docurandom.voicevox import VoicevoxError, VoicevoxSynthesisSettings
from docurandom.voicevox_project import VoicevoxStyleOption, build_voicevox_project, write_project_checkpoint
from docurandom.youtube_upload import YouTubeUploadError, resolve_workspace_upload_sources
from docurandom.publication_assets import publication_sources, ensure_publication_assets
from tests.test_publication_assets import setup_video, image_bytes
from tests.test_voicevox_project import _engine_query
from tests.test_youtube_upload import _workspace


class NarrationIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tk_root = tk.Tk()
        cls.tk_root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.tk_root.destroy()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name) / 'video.txt'
        self.script = '現在の動画です。\n新しい工場です。'
        self.package.write_text('TIÊU ĐỀ\n\nnew\n\nMÔ TẢ\n\nnew description\n\nSEO TAGS\n\nnew, factory\n\nTHOẠI VOICEVOX\n\nA: 現在の動画です。\nA: 新しい工場です。\n', encoding='utf-8-sig')

    def app_with_editor(self, text):
        widget = tk.Text(self.tk_root)
        self.addCleanup(widget.destroy)
        widget.insert('1.0', text)
        widget.configure(state='disabled')
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.voicevox_script_text = widget
        app.last_content_package = self.package
        app.content_last_result = app.content_runner = None
        app.voicevox_styles = {}
        app.voicevox_preferred_style_id = 53
        for key, value in {'voicevox_style_var':'normal', 'voicevox_endpoint_var':'http://127.0.0.1:50021', 'voicevox_engine_path_var':'', 'voicevox_use_gpu_var':False, 'output_name_var':'video'}.items():
            setattr(app, key, Mock(get=Mock(return_value=value)))
        app._collect_voicevox_settings = Mock(return_value=VoicevoxSynthesisSettings())
        app._effective_voicevox_backup_dir = Mock(return_value=None)
        app._resolve_video_chat_context = Mock(return_value=('current-thread', ''))
        app._append_log = Mock()
        return app

    def test_generated_script_updates_real_disabled_tk_text_and_restores_lock(self):
        app = self.app_with_editor('以前の動画です。')
        # Reproduce the previous failure: Tk silently ignores these writes.
        app.voicevox_script_text.delete('1.0', 'end')
        app.voicevox_script_text.insert('1.0', self.script)
        self.assertEqual(app.voicevox_script_text.get('1.0', 'end-1c'), '以前の動画です。')
        app._set_voicevox_script(self.script)
        self.assertEqual(app.voicevox_script_text.get('1.0', 'end-1c'), self.script)
        self.assertEqual(app.voicevox_script_text.cget('state'), 'disabled')

    def test_automatic_request_replaces_nonempty_previous_script(self):
        app = self.app_with_editor('以前の動画です。')
        request = app._collect_smart_voice_request(require_package=True)
        self.assertEqual(request.script, self.script)
        self.assertEqual(request.package_path, self.package.resolve())
        self.assertEqual(request.package_script_sha256, narration_digest(self.script))
        self.assertEqual(app.voicevox_script_text.get('1.0', 'end-1c'), self.script)
        self.assertEqual(request.chatgpt_thread_id, 'current-thread')

    def test_standalone_manual_request_keeps_user_script(self):
        app = self.app_with_editor('手動で編集した文章です。')
        request = app._collect_smart_voice_request()
        self.assertEqual(request.script, '手動で編集した文章です。')
        self.assertIsNone(request.package_path)

    def test_missing_automatic_package_cannot_fall_back_to_old_editor(self):
        app = self.app_with_editor('以前の動画です。')
        self.package.unlink()
        with self.assertRaises(VoicevoxError):
            app._collect_smart_voice_request(require_package=True)

    def test_package_missing_voice_cannot_fall_back_to_old_editor(self):
        app = self.app_with_editor('以前の動画です。')
        self.package.write_text('TIÊU ĐỀ\nnew', encoding='utf-8')
        with self.assertRaises(VoicevoxError):
            app._collect_smart_voice_request(require_package=True)

    def test_same_narration_snapshot_survives_metadata_edit(self):
        digest = narration_digest(self.script)
        self.package.write_text(self.package.read_text(encoding='utf-8-sig').replace('new', 'edited title'), encoding='utf-8')
        validate_narration_snapshot(self.package, self.script, digest)

    def test_changed_package_rejects_queued_voice_before_engine_or_cached_result(self):
        request = SmartVoiceRequest('batch', self.script, VoicevoxSynthesisSettings(), 53, '', '', False, package_path=self.package, package_script_sha256=narration_digest(self.script))
        self.package.write_text('THOẠI VOICEVOX\nA: 別の動画です。', encoding='utf-8')
        app = DocuRandomApp.__new__(DocuRandomApp)
        app._record_pipeline_stage = Mock()
        with self.assertRaises(VoicevoxError):
            app._create_smart_voice_for_job('job', 'label', None, request, threading.Event())
        app._record_pipeline_stage.assert_not_called()

    def test_script_snapshot_cannot_claim_digest_of_another_script(self):
        with self.assertRaises(VoicevoxError):
            validate_narration_snapshot(self.package, '以前の動画です。', narration_digest(self.script))

    def test_unreadable_project_blocks_publication(self):
        project = self.package.with_suffix('.vvproj')
        project.write_text('{}', encoding='utf-8')
        with self.assertRaises(VoicevoxError):
            validate_project_narration(self.package, project)

    def test_upload_accepts_matching_voice_and_blocks_previous_video_project(self):
        workspace = _workspace(Path(self.temp.name) / 'workspace')
        package = workspace.output_dir / 'video.txt'
        package.write_text(self.package.read_text(encoding='utf-8-sig'), encoding='utf-8')
        video = workspace.output_dir / 'video.mp4'
        video.write_bytes(b'completed-video-fixture')
        project = workspace.voicevox_dir / 'video.vvproj'
        style = VoicevoxStyleOption('test', 'uuid', 'normal', 53)
        lines = self.script.splitlines()
        data = build_voicevox_project(app_version='0.25.2', lines=lines, queries=[_engine_query('テスト') for _ in lines], style=style)
        write_project_checkpoint(project, data)
        checkpoint = {'stages': {'package': {'status':'complete','artifacts':{'path':str(package)}}, 'render':{'status':'complete','artifacts':{'video':str(video)}}, 'voicevox':{'status':'complete','artifacts':{'audited_project':str(project),'script_sha256':narration_digest(self.script)}}}}
        workspace.pipeline_checkpoint_path.write_text(json.dumps(checkpoint), encoding='utf-8')
        self.assertEqual(resolve_workspace_upload_sources(workspace).video_path, video.resolve())
        old = build_voicevox_project(app_version='0.25.2', lines=['以前の動画です。'], queries=[_engine_query('テスト')], style=style)
        write_project_checkpoint(project, old)
        with self.assertRaisesRegex(YouTubeUploadError, 'không khớp'):
            resolve_workspace_upload_sources(workspace)

    def test_archived_mismatched_voice_cannot_be_public_or_repaired_with_an_image(self):
        from unittest.mock import patch
        _w, item, status, pool = setup_video(Path(self.temp.name) / 'queue')
        archive = Path(item['archive_dir'])
        package = Path(item['package_path'])
        package.write_text(self.package.read_text(encoding='utf-8-sig'), encoding='utf-8')
        voice_dir = archive / 'VOICEVOX'
        voice_dir.mkdir()
        project = voice_dir / 'B_VOICEVOX_FINAL_audited.vvproj'
        data = build_voicevox_project(app_version='0.25.2', lines=['以前の動画です。'], queries=[_engine_query('テスト')], style=VoicevoxStyleOption('test','uuid','normal',53))
        write_project_checkpoint(project,data)
        thumbnail = archive / 'thumbnail.png'
        thumbnail.write_bytes(image_bytes())
        item['thumbnail_path'] = str(thumbnail)
        with self.assertRaisesRegex(YouTubeUploadError, 'không khớp'):
            publication_sources(item)
        with patch('docurandom.publication_assets.ChatGPTWebChat') as chat:
            with self.assertRaisesRegex(YouTubeUploadError, 'không khớp'):
                ensure_publication_assets(_w,item,status,pool,threading.Event(),Mock())
            chat.assert_not_called()
        pool.acquire.assert_not_called()

    def test_real_render_method_reaches_renderer_without_a_voice_request_local(self):
        from unittest.mock import patch
        class ReachedRenderIdentity(Exception):
            pass
        app = DocuRandomApp.__new__(DocuRandomApp)
        profile = SimpleNamespace(encoder='libx264', hardware_accelerated=False, threads_per_job=1)
        app._ensure_runtime = Mock(return_value=(None,profile))
        app._profile_for_jobs = Mock(return_value=profile)
        app._audio_duration_for_job = Mock(return_value=0.0)
        app._update_job_disk_reservation = Mock()
        app._runtime_summary = Mock(return_value='CPU')
        app._record_pipeline_stage = Mock()
        app.events = Queue()
        settings = RenderSettings(source_dir=Path(self.temp.name),output_dir=Path(self.temp.name),remove_gemini_watermarks=False,flow_right_crop=False)
        # Exercise the actual method used after the smart-voice wrapper;
        # mocking only _render_one_job would miss an out-of-scope variable.
        with patch('docurandom.ui.render_request_identity', side_effect=ReachedRenderIdentity):
            with self.assertRaises(ReachedRenderIdentity):
                app._render_one_job('job','label',(),settings,threading.Event())
        app._record_pipeline_stage.assert_any_call('render','running','Đang dựng timeline, subtitle, audio và kiểm tra video.')

    def test_timestamped_archived_package_uses_its_archived_voice_filename(self):
        _w, item, *_ = setup_video(Path(self.temp.name) / 'timestamped-queue')
        archive = Path(item['archive_dir'])
        package = Path(item['package_path'])
        package.write_text(self.package.read_text(encoding='utf-8-sig'), encoding='utf-8')
        timestamped = package.with_name('2026-10-11_08-38-56_B.txt')
        package.rename(timestamped)
        item['package_path'] = str(timestamped)
        project = archive / 'VOICEVOX/B_VOICEVOX_FINAL_audited.vvproj'
        project.parent.mkdir()
        data = build_voicevox_project(app_version='0.25.2', lines=self.script.splitlines(),
            queries=[_engine_query('テスト') for _ in self.script.splitlines()],
            style=VoicevoxStyleOption('test','uuid','normal',53))
        write_project_checkpoint(project, data)
        # The recorded live filename now exists for a different current video.
        # Only B's archive can establish the narration of queued B.
        live = Path(self.temp.name) / project.name
        live.write_text('{}', encoding='utf-8')
        cp = archive / 'State/pipeline_checkpoint.json'
        cp.parent.mkdir()
        cp.write_text(json.dumps({'stages': {'voicevox': {'artifacts': {
            'audited_project': str(live), 'audit_status': 'applied',
            'script_sha256': narration_digest(self.script)}}}}), encoding='utf-8')
        thumb = archive/'thumbnail.png'
        thumb.write_bytes(image_bytes())
        item['thumbnail_path'] = str(thumb)
        self.assertEqual(publication_sources(item).package_path, timestamped.resolve())
        project.unlink()
        # A live project must not be substituted for a missing archived one.
        write_project_checkpoint(live, data)
        with self.assertRaisesRegex(YouTubeUploadError, 'Thiếu đúng project'):
            publication_sources(item)
