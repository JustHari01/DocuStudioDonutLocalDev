import json
from pathlib import Path
from docurandom.workspace import Workspace
from docurandom.thumbnail_ai import ThumbnailAIClaim,reserve_thumbnail_readonly_probe

def test_readonly_image_collection_spends_exact_durable_budget(tmp_path: Path):
 w=Workspace('workspace','Test',str(tmp_path),'now','now');w.state_dir.mkdir()
 path=w.state_dir/'thumbnail-ai.json';path.write_text(json.dumps({'package_sha256':'package','prompt_sha256':'prompt','thread_id':'thread','readonly_probe_count':0}))
 claim=ThumbnailAIClaim('recover','text','prompt','package',read_only_collection=True)
 assert not reserve_thumbnail_readonly_probe(w,claim,'foreign')
 for count in range(1,5):
  assert reserve_thumbnail_readonly_probe(w,claim,'thread')
  assert json.loads(path.read_text())['readonly_probe_count']==count
 assert not reserve_thumbnail_readonly_probe(w,claim,'thread')
