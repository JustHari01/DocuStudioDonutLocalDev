import copy
import unittest
import tempfile
import json
import threading
from pathlib import Path
from unittest import mock

from docurandom.youtube_recovery import exact_interrupted_file_input, exact_content_row, ArchivedUploadRecovery
from docurandom.youtube_upload import YouTubeUploadUncertain
from docurandom.youtube_upload import _exact_server_upload_feedback


def row(title="Old title", name="Video 4.mp4", hidden=False):
    controls = ["class", "resume-upload-button", "aria-disabled", "false"]
    if hidden:
        controls += ["hidden", ""]
    return {"nodeName": "YTCP-VIDEO-ROW", "children": [
        {"nodeName": "A", "attributes": ["id", "video-title", "aria-label", title]},
        {"nodeName": "#text", "nodeValue": 'Quá trình tải lên bị gián đoạn. Chọn ' + name},
        {"nodeName": "YTCP-BUTTON", "backendNodeId": 5, "attributes": controls},
        {"nodeName": "INPUT", "backendNodeId": 6, "attributes": ["type", "file"]},
    ]}


class InterruptedRowRecoveryTests(unittest.TestCase):
    def test_backend_success_proves_completion_after_percentage_disappears(self):
        response={'continuationContents':[{'uploadFeedbackItemContinuation':{'id':{'videoId':'AuUuN86k9vI'},'contents':[{'uploadStatus':{'uploadStatus':'SUCCESS','watchEndpoint':{'watchEndpoint':{'videoId':'AuUuN86k9vI'}}}}]}}]}
        self.assertEqual(_exact_server_upload_feedback(response,'AuUuN86k9vI'),(100.0,''))
        self.assertEqual(_exact_server_upload_feedback(response,'Another1234'),(None,''))

    def test_partial_bytes_override_success_and_processing_cannot_prove_transfer(self):
        contents=[{'uploadStatus':{'uploadStatus':'SUCCESS'}},{'transferProgressBar':{'fractionCompleted':.55}},{'processingProgressBar':{'fractionCompleted':1}}]
        response={'continuationContents':[{'uploadFeedbackItemContinuation':{'id':{'videoId':'AuUuN86k9vI'},'contents':contents}}]}
        self.assertEqual(_exact_server_upload_feedback(response,'AuUuN86k9vI'),(55.00000000000001,''))
        contents[:]=[{'processingProgressBar':{'fractionCompleted':1}}]
        self.assertEqual(_exact_server_upload_feedback(response,'AuUuN86k9vI'),(None,''))

    def test_success_with_mismatched_watch_id_fails_closed(self):
        response={'continuationContents':[{'uploadFeedbackItemContinuation':{'id':{'videoId':'AuUuN86k9vI'},'contents':[{'uploadStatus':{'uploadStatus':'SUCCESS','watchEndpoint':{'watchEndpoint':{'videoId':'Different123'}}}}]}}]}
        self.assertEqual(_exact_server_upload_feedback(response,'AuUuN86k9vI'),(None,''))
    def test_uses_only_input_inside_matching_old_row(self):
        document = {"children": [row("New title"), row()]}
        selected = exact_interrupted_file_input(document, "Old title", "Video 4.mp4")
        self.assertIs(selected[0], document["children"][1]["children"][-1])

    def test_same_filename_with_different_title_is_not_enough(self):
        self.assertIsNone(exact_interrupted_file_input(row("New title"), "Old title", "Video 4.mp4"))

    def test_title_with_different_requested_file_is_not_enough(self):
        self.assertIsNone(exact_interrupted_file_input(row(), "Old title", "Video 4.mp4.backup"))

    def test_duplicate_rows_fail_closed(self):
        with self.assertRaises(YouTubeUploadUncertain):
            exact_interrupted_file_input({"children": [row(), copy.deepcopy(row())]}, "Old title", "Video 4.mp4")

    def test_hidden_resume_control_cannot_authorize_attachment(self):
        self.assertIsNone(exact_interrupted_file_input(row(hidden=True), "Old title", "Video 4.mp4"))

    def test_completed_row_cannot_authorize_attachment(self):
        document = row()
        document["children"][1]["nodeValue"] = "Video đã tải lên xong Video 4.mp4"
        self.assertIsNone(exact_interrupted_file_input(document, "Old title", "Video 4.mp4"))

    def test_ambiguous_inputs_cannot_authorize_attachment(self):
        document = row()
        document["children"].append(copy.deepcopy(document["children"][-1]))
        self.assertIsNone(exact_interrupted_file_input(document, "Old title", "Video 4.mp4"))

    def test_watch_link_identifies_exact_row_without_edit_link(self):
        document = row()
        document['children'].append({'nodeName':'A','attributes':['href','https://www.youtube.com/watch?v=AuUuN86k9vI']})
        self.assertIs(exact_content_row(document,'AuUuN86k9vI'),document)
        self.assertIsNone(exact_content_row(document,'Another1234'))

    def test_duplicate_video_ids_fail_closed(self):
        document = row()
        document['children'].append({'nodeName':'A','attributes':['href','https://www.youtube.com/watch?v=AuUuN86k9vI']})
        with self.assertRaises(YouTubeUploadUncertain):
            exact_content_row({'children':[document,copy.deepcopy(document)]},'AuUuN86k9vI')

    def test_archived_recovery_uses_separate_checkpoint_from_successor(self):
        from pathlib import Path
        from docurandom.workspace import Workspace
        w=Workspace('id','name','D:/Youtube/example','','')
        self.assertNotEqual(ArchivedUploadRecovery._checkpoint_path(w), w.state_dir/'youtube_upload.json')

    def test_changed_archived_bytes_stop_before_connecting_browser(self):
        from docurandom.workspace import Workspace
        from docurandom.youtube_upload import WorkspaceUploadSources, YouTubeMetadata, YouTubeUploadRequest
        from docurandom.donut_mcp import DonutMcpEndpoint
        with tempfile.TemporaryDirectory() as temp:
            w=Workspace('id','name',temp,'','')
            w.ensure_layout()
            video=w.output_dir/'old.mp4'; video.write_bytes(b'old immutable video')
            package=w.output_dir/'old.txt'; package.write_text('metadata')
            uploader=ArchivedUploadRecovery(registry_path=Path(temp)/'registry.json')
            sources=WorkspaceUploadSources(video,package,None,YouTubeMetadata('old title','old description'))
            request=YouTubeUploadRequest(w,DonutMcpEndpoint.parse('http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv'),'profile')
            identity=uploader._request_identity(request,sources)
            cp={'identity':identity,'attempt_id':'attempt','profile_id':'profile','status':'uploading','video_path':str(video),'package_path':str(package),'title':'old title','description':'old description'}
            uploader._checkpoint_path(w).write_text(json.dumps(cp),encoding='utf-8')
            successor=w.state_dir/'youtube_upload.json'; successor.write_text('{"status":"queued","title":"new"}',encoding='utf-8')
            before=successor.read_bytes()
            uploader._registry_set(identity,{'attempt_id':'attempt','profile_id':'profile','archived_upload_recovery':True,'youtube_url':'https://youtu.be/AuUuN86k9vI'})
            video.write_bytes(b'a different video with the same filename')
            with mock.patch('docurandom.youtube_recovery.resolve_donut_mcp_endpoint',return_value=request.endpoint), mock.patch('docurandom.youtube_recovery.DonutMcpClient') as client:
                with self.assertRaises(YouTubeUploadUncertain): uploader.recover(w,threading.Event())
                client.assert_not_called()
            self.assertEqual(successor.read_bytes(),before)

    def test_transferred_archive_retries_only_metadata_without_browser_attachment(self):
        from docurandom.workspace import Workspace
        from docurandom.youtube_upload import WorkspaceUploadSources, YouTubeMetadata, YouTubeUploadRequest
        from docurandom.donut_mcp import DonutMcpEndpoint
        with tempfile.TemporaryDirectory() as temp:
            w=Workspace('id','name',temp,'',''); w.ensure_layout()
            video=w.output_dir/'old.mp4'; video.write_bytes(b'old immutable video')
            package=w.output_dir/'old.txt'; package.write_text('metadata')
            uploader=ArchivedUploadRecovery(registry_path=Path(temp)/'registry.json')
            sources=WorkspaceUploadSources(video,package,None,YouTubeMetadata('old title','old description'))
            endpoint=DonutMcpEndpoint.parse('http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv')
            request=YouTubeUploadRequest(w,endpoint,'profile')
            identity=uploader._request_identity(request,sources)
            cp={'identity':identity,'attempt_id':'attempt','profile_id':'profile','status':'submitting','video_path':str(video),'package_path':str(package),'title':'old title','description':'old description'}
            uploader._checkpoint_path(w).write_text(json.dumps(cp),encoding='utf-8')
            uploader._registry_set(identity,{'attempt_id':'attempt','profile_id':'profile','archived_upload_recovery':True,'youtube_url':'https://youtu.be/AuUuN86k9vI','transfer_complete':True})
            successor=w.state_dir/'youtube_upload.json'; successor.write_text('{"status":"queued","title":"new"}',encoding='utf-8')
            before=successor.read_bytes()
            with mock.patch('docurandom.youtube_recovery.resolve_donut_mcp_endpoint',return_value=endpoint), mock.patch('docurandom.youtube_recovery.DonutMcpClient') as client, mock.patch.object(uploader,'_reconcile_submitted_locked_inner',side_effect=YouTubeUploadUncertain('network timeout')), mock.patch.object(uploader,'_recover_locked_upload') as retry:
                uploader.recover(w,threading.Event())
                client.assert_not_called()
                retry.assert_called_once()
                self.assertTrue(retry.call_args.kwargs['metadata_only'])
                self.assertEqual(retry.call_args.args[2],identity)
            self.assertEqual(successor.read_bytes(),before)
