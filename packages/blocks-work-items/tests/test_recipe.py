from pathlib import Path
import hashlib
import json
from gradia_composition import build_lock,diagram,read_json,verify_lock

ROOT=Path(__file__).resolve().parents[1]


def test_checked_recipe_has_exact_lock_schema_connections_and_fresh_source_pins():
    graph=read_json(ROOT/'examples/feedback-to-work-item.graph.json')
    lock=read_json(ROOT/'examples/feedback-to-work-item.lock.json')
    assert build_lock(graph,['feedback-to-work-item'],'demo')==lock
    assert verify_lock(lock,graph,['feedback-to-work-item'],'demo')['status']=='REPRODUCED'
    assert lock['unresolvedInputs']==[]
    assert lock['compatibility']=='UNKNOWN' and not lock['promotionAuthorized']
    assert diagram(graph,'demo')==(ROOT/'examples/feedback-to-work-item.mmd').read_text()
    pins=json.loads((ROOT/'examples/recipe-source-pins.json').read_text())['implementations']
    for name,manifest in pins.items():
        folder=ROOT.parent/('blocks-'+name)
        for file in manifest['files']:
            assert hashlib.sha256((folder/file['path']).read_bytes()).hexdigest()==file['sha256'],file['path']
        assert hashlib.sha256(json.dumps(manifest['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest()==manifest['sha256']
    nodes={node['id']:node for node in graph['nodes']}
    assert nodes['feedback']['artifactSha256']==pins['feedback']['sha256']
    assert nodes['work-items']['artifactSha256']==nodes['feedback-proposal-handoff']['artifactSha256']==pins['work-items']['sha256']
    assert nodes['feedback-to-work-item']['artifactSha256']==hashlib.sha256((ROOT/'examples/tcp_replay.py').read_bytes()).hexdigest()
    for name,node in [('feedback-openapi.json','feedback'),('work-items-openapi.json','work-items')]:
        spec=json.loads((ROOT/'examples'/name).read_text())
        assert nodes[node]['contractSha256']==hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':')).encode()).hexdigest()
