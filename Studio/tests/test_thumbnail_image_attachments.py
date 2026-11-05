import base64,json,subprocess
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
from docurandom.content_ai import ChatGPTWebChat,ChatAttachment,ContentAIError
from docurandom.thumbnail_ai import thumbnail_canvas_bytes


@pytest.mark.parametrize('mode',['generated_image','thumbnail_confirmation'])
def test_real_send_payload_and_native_parser_accept_the_same_png_bytes(tmp_path,mode):
    data=thumbnail_canvas_bytes();client=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48)
    connection=Mock();response=connection.getresponse.return_value;response.status=200
    response.read.return_value=json.dumps({'id':'resp_fixture','status':'completed','output_text':'ĐÃ TẠO'}).encode()
    response.getheader.side_effect=lambda name,default=None: str(len(response.read.return_value)) if name=='Content-Length' else 'application/json' if name=='Content-Type' else default
    with patch('docurandom.content_ai.http.client.HTTPConnection',return_value=connection):
        client._send_response('Exact image request',output_mode=mode,
            attachments=(ChatAttachment('canvas.png',data,'image/png'),))
    payload=json.loads(connection.request.call_args.kwargs['body'])
    assert base64.b64decode(payload['attachments'][0]['content_base64'])==data
    # Exercise the native server validator on the actual Python payload,
    # without starting a browser, submitting a prompt or using the network.
    module=Path(__file__).parents[1]/'chat_bridge/src/attachment-request.cjs'
    script="const fs=require('node:fs'),{requestAttachments}=require(process.argv[1]);const b=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(requestAttachments(b.attachments,b.docurandom_output)));"
    completed=subprocess.run([r'C:\Program Files\nodejs\node.exe','-e',script,str(module)],
        input=json.dumps(payload).encode(),capture_output=True,check=True)
    received=json.loads(completed.stdout)
    assert received==payload['attachments']


@pytest.mark.parametrize('mode,name,mime,data',[('text','canvas.png','image/png',thumbnail_canvas_bytes()),
    ('generated_image','canvas.png','image/jpeg',thumbnail_canvas_bytes()),
    ('generated_image','canvas.png','image/png',b'not-an-image')])
def test_invalid_attachment_is_rejected_before_any_request(tmp_path,mode,name,mime,data):
    client=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48)
    with patch('docurandom.content_ai.http.client.HTTPConnection')as connection:
        with pytest.raises(ContentAIError):client._send_response('request',output_mode=mode,attachments=(ChatAttachment(name,data,mime),))
        connection.assert_not_called()
