"""Generate declared dependency contracts. No network requests or runtime attestation."""
from pathlib import Path
import hashlib
import json
import secrets
import tempfile
from gradia_feedback import create_app as feedback_app,Principal as FeedbackPrincipal
from gradia_work_items import create_app as items_app,Principal as ItemPrincipal
from gradia_outbox.api import create_app as outbox_app
from gradia_outbox.contracts import Principal as OutboxPrincipal,Destination
from gradia_event_relay.api import create_app as relay_app
from gradia_event_relay import Relay
from gradia_event_relay.contracts import Principal as RelayPrincipal,Page
from gradia_work_items.contracts import ChangeFeedPage
from gradia_composition import build_lock,diagram

ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).resolve().parent
sha=lambda value:hashlib.sha256(value).hexdigest()
canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()

def implementation(folder,module):
    files=list((folder/module).glob('*.py'))
    files.extend(p for p in folder.iterdir() if p.is_file() and p.suffix in ('.json','.toml','.txt','.md'))
    files.extend(p for p in [folder/'LICENSE',folder/'NOTICE'] if p.exists())
    files.extend((folder/'skills').glob('*.md'))
    manifest=[{'path':p.relative_to(folder).as_posix(),'sha256':sha(p.read_bytes())} for p in sorted(set(files))]
    return {'sha256':sha(canonical(manifest)),'files':manifest,'scope':'Root source/config/license files, implementation Python modules and packaged skill Markdown; excludes tests and generated examples'}

def schemas():
    with tempfile.TemporaryDirectory() as temp:
        base=Path(temp);key=secrets.token_hex(32)
        feedback=feedback_app(base/'feedback.sqlite',{key:FeedbackPrincipal(tenant_id='demo',role='operator')}).openapi()
        items=items_app(base/'items.sqlite',{key:ItemPrincipal(tenant_id='demo',role='operator')}).openapi()
        outbox=outbox_app(base/'outbox.sqlite',{key:OutboxPrincipal(tenant_id='demo',actor_id='schema',role='operator')},{'fixture':Destination(tenant_id='demo',url='http://127.0.0.1:3195/webhook',signing_key=secrets.token_hex(32),receiver_id='fixture')}).openapi()
        with Relay(base/'relay.sqlite',source_origin='http://127.0.0.1:3192',target_origin='http://127.0.0.1:3193',source_token=key,target_token=key,tenant_id='demo',profile_id='schema',destination_id='fixture') as relay:
            facade=relay_app(relay,{key:RelayPrincipal(tenant_id='demo',actor_id='schema')}).openapi()
    return {'feedback':feedback,'work-items':items,'outbox':outbox,'event-relay':facade}

def protocol_schemas():
    # Preserve actual producer and stricter relay consumer contracts separately.
    return {'schema':'gradia.event-relay.protocol-contracts.v1','changefeedProducerPage':ChangeFeedPage.model_json_schema(),'changefeedConsumerPage':Page.model_json_schema(),
        'signedWebhook':{'envelope':{'schema':'gradia.webhook.v1','fields':['schema','delivery_id','tenant_id','event_type','payload_sha256','payload','created_at']},
        'acknowledgment':{'schema':'gradia.webhook.ack.v1','fields':['schema','delivery_id','payload_sha256','receiver_id','accepted'],'accepted':True},
        'postSignature':'HMAC-SHA256(signing_key, timestamp + dot + immutable_envelope)',
        'receiptGetSignature':'HMAC-SHA256(signing_key, timestamp + dot + GET + dot + exact_url + dot + immutable_envelope)',
        'ackSignature':'HMAC-SHA256(signing_key, gradia.webhook.ack.v1. + request_timestamp + dot + attempt_id + dot + response_bytes)'},
        'boundary':'Declared protocol projection matching current source; not a universal runtime compatibility or authenticity proof'}

def build():
    specs=schemas();protocols=protocol_schemas()
    pins={name:implementation(ROOT/'packages'/('blocks-'+name),'gradia_'+name.replace('-','_')) for name in ('feedback','work-items','outbox','event-relay')}
    drivers=[{'path':name,'sha256':sha((OUT/name).read_bytes())} for name in ('full_tcp_replay.py','fixture_enqueue_proxy.py')]
    receiver=ROOT/'packages/blocks-outbox/examples/fixture_receiver.py'
    fixture={'path':'packages/blocks-outbox/examples/fixture_receiver.py','sha256':sha(receiver.read_bytes())}
    def node(identity,kind,version,artifact,contract,requires=(),scope='public'):
        return {'id':identity,'kind':kind,'edition':version,'artifactSha256':artifact,'contractSha256':contract,'scope':scope,'requires':[pin(v) for v in requires],'inputs':{},'outputs':{}}
    def pin(n):return {key:n[key] for key in ('id','edition','artifactSha256','contractSha256')}
    f=node('feedback','block','0.2.1',pins['feedback']['sha256'],sha(canonical(specs['feedback'])))
    w=node('work-items','block','0.2.0',pins['work-items']['sha256'],sha(canonical(specs['work-items'])))
    o=node('outbox','block','0.1.0',pins['outbox']['sha256'],sha(canonical(specs['outbox'])))
    profile=json.loads((ROOT/'packages/blocks-work-items/block.manifest.json').read_text())['handoffProfile']
    h=node('feedback-proposal-handoff','bridge','0.2.0',pins['work-items']['sha256'],sha(canonical(profile)),[f,w])
    r=node('event-relay','bridge','0.1.0',pins['event-relay']['sha256'],sha(canonical(specs['event-relay'])),[w,o])
    v=node('signed-receiver-fixture','block','0.1.0',fixture['sha256'],sha(canonical(protocols['signedWebhook'])),[o],scope='demo')
    x=node('feedback-event-delivery','workflow','0.1.0',sha(canonical(drivers)),sha(canonical({'protocols':protocols,'drivers':drivers})),[h,r,v],scope='demo')
    graph={'schema':'gradia.composition.graph.v1','scope':'demo','nodes':[f,w,o,h,r,v,x],'connections':[]}
    def connect(source,output,target,input,schema):
        digest=sha(canonical(schema));source['outputs'][output]=digest;target['inputs'][input]=digest
        graph['connections'].append({'fromNode':source['id'],'fromPort':output,'toNode':target['id'],'toPort':input})
    connect(f,'feedback-record',h,'selected-feedback',specs['feedback']['components']['schemas']['FeedbackRecord'])
    connect(w,'item',h,'target-item-contract',specs['work-items']['components']['schemas']['Item'])
    connect(w,'changefeed-producer-contract',r,'work-item-metadata',specs['work-items']['components']['schemas']['ChangeFeedPage'])
    connect(o,'enqueue-contract',r,'target-enqueue-contract',specs['outbox']['components']['schemas']['Enqueue'])
    connect(o,'acknowledged-item',r,'target-acknowledged-item',specs['outbox']['components']['schemas']['OutboxItem'])
    connect(o,'signed-webhook',v,'signed-webhook',protocols['signedWebhook'])
    connect(h,'item-proposal',x,'item-proposal',specs['work-items']['components']['schemas']['Item'])
    connect(r,'target-acknowledgment',x,'enqueue-acknowledgment',specs['outbox']['components']['schemas']['EnqueueAcknowledgment'])
    connect(v,'signed-receiver-acknowledgment',x,'receiver-acknowledgment',protocols['signedWebhook']['acknowledgment'])
    values={name+'-openapi.json':spec for name,spec in specs.items()}
    values.update({'protocol-contracts.json':protocols,'event-relay.graph.json':graph,'event-relay.lock.json':build_lock(graph,[x['id']],'demo'),
        'recipe-source-pins.json':{'schema':'gradia.recipe.source-pins.v1','implementations':pins,'workflowDrivers':drivers,'receiverFixture':fixture,
        'boundary':'Declared dependency/contract DAG, not HTTP call ordering. Page port pins actual typed producer OpenAPI contract; separate stricter consumer projection limits pages to25. Schema identity is not semantic compatibility proof. Workflow driver pair and receiver source pinned separately. No full-package/runtime attestation or arbitrary workflow qualification.'}})
    for name,value in values.items():(OUT/name).write_text(json.dumps(value,indent=2)+'\n')
    (OUT/'event-relay.mmd').write_text(diagram(graph,'demo'))
    return graph

if __name__=='__main__':
    build();print('Generated declared relay schemas, pins, lock and dependency diagram')
