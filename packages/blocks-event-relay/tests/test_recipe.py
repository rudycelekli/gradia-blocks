from pathlib import Path
import importlib.util
import hashlib
import json
from gradia_composition import build_lock,diagram,verify_lock

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]
def sha(value):return hashlib.sha256(value).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
def read(name):return json.loads((ROOT/'examples'/name).read_text())
def builder():
    spec=importlib.util.spec_from_file_location('event_relay_recipe_builder',ROOT/'examples/build_recipe.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_recipe_reproduces_exact_lock_declared_connections_and_diagram():
    graph=read('event-relay.graph.json');lock=read('event-relay.lock.json')
    assert build_lock(graph,['feedback-event-delivery'],'demo')==lock
    assert verify_lock(lock,graph,['feedback-event-delivery'],'demo')['status']=='REPRODUCED'
    assert lock['compatibility']=='UNKNOWN' and lock['promotionAuthorized'] is False
    assert lock['unresolvedInputs']==[]
    assert diagram(graph,'demo')==(ROOT/'examples/event-relay.mmd').read_text()
    nodes={n['id']:n for n in graph['nodes']}
    assert {k:nodes[k]['edition'] for k in ('feedback','work-items','outbox','event-relay')}=={'feedback':'0.2.1','work-items':'0.2.0','outbox':'0.1.0','event-relay':'0.1.0'}


def test_source_hashes_cover_implementations_packaged_skill_and_both_driver_files():
    pins=read('recipe-source-pins.json');nodes={n['id']:n for n in read('event-relay.graph.json')['nodes']}
    for name,manifest in pins['implementations'].items():
        folder=REPO/'packages'/('blocks-'+name)
        assert builder().implementation(folder,'gradia_'+name.replace('-','_'))==manifest
        for entry in manifest['files']:assert sha((folder/entry['path']).read_bytes())==entry['sha256']
        assert sha(canonical(manifest['files']))==manifest['sha256']
        assert nodes[name]['artifactSha256']==manifest['sha256']
    assert any(p['path']=='skills/relay-work-item-events.md' for p in pins['implementations']['event-relay']['files'])
    assert {e['path'] for e in pins['workflowDrivers']}=={'full_tcp_replay.py','fixture_enqueue_proxy.py'}
    for entry in pins['workflowDrivers']:assert sha((ROOT/'examples'/entry['path']).read_bytes())==entry['sha256']
    assert nodes['feedback-event-delivery']['artifactSha256']==sha(canonical(pins['workflowDrivers']))
    fixture=pins['receiverFixture'];assert sha((REPO/fixture['path']).read_bytes())==fixture['sha256']
    assert nodes['signed-receiver-fixture']['artifactSha256']==fixture['sha256']


def test_generated_openapi_and_protocols_match_current_actual_implementations():
    module=builder();specs=module.schemas();nodes={n['id']:n for n in read('event-relay.graph.json')['nodes']}
    for name,spec in specs.items():
        assert read(name+'-openapi.json')==spec
        assert nodes[name]['contractSha256']==sha(canonical(spec))
    assert read('protocol-contracts.json')==module.protocol_schemas()
    producer=specs['work-items']['components']['schemas']['ChangeFeedPage']
    path=specs['work-items']['paths']['/v1/work-items/changes']['get']
    assert path['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/ChangeFeedPage')
    assert nodes['work-items']['outputs']['changefeed-producer-contract']==sha(canonical(producer))
    assert nodes['event-relay']['inputs']['work-item-metadata']==sha(canonical(producer))
