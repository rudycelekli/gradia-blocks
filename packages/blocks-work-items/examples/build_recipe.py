"""Rebuild public API schemas and declared implementation locks. No live provider access."""
from pathlib import Path
import hashlib
import json
import tempfile
from gradia_feedback import create_app as feedback_app,Principal as FeedbackPrincipal
from gradia_work_items import create_app,Principal
from gradia_composition import build_lock,diagram

ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).resolve().parent
sha=lambda value:hashlib.sha256(value).hexdigest()
canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def implementation(folder,module):
    files=[p for p in (folder/module).glob('*.py')]
    files.extend(p for p in folder.iterdir() if p.is_file() and p.suffix in ('.json','.toml','.txt','.md'))
    files.extend(p for p in [folder/'LICENSE',folder/'NOTICE'] if p.exists())
    manifest=[{'path':p.relative_to(folder).as_posix(),'sha256':sha(p.read_bytes())} for p in sorted(set(files))]
    return {'sha256':sha(canonical(manifest)),'files':manifest,'scope':'Root source/config/license files and implementation Python modules; excludes tests and generated examples'}


def build():
    with tempfile.TemporaryDirectory() as temp:
        base=Path(temp)
        feedback=feedback_app(base/'feedback.sqlite',{'metadata-schema-key-longer-than-24':FeedbackPrincipal(tenant_id='demo',role='operator')}).openapi()
        items=create_app(base/'items.sqlite',{'metadata-schema-key-longer-than-24':Principal(tenant_id='demo',role='bridge')}).openapi()
    source=ROOT/'packages/blocks-feedback';target=ROOT/'packages/blocks-work-items'
    pins={'feedback':implementation(source,'gradia_feedback'),'work-items':implementation(target,'gradia_work_items')}
    def node(id,kind,version,artifact,contract,scope='public',requires=None):
        return {'id':id,'kind':kind,'edition':version,'artifactSha256':artifact,'contractSha256':contract,'scope':scope,'requires':requires or [],'inputs':{},'outputs':{}}
    def pin(value):return {key:value[key] for key in ('id','edition','artifactSha256','contractSha256')}
    f=node('feedback','block','0.2.1',pins['feedback']['sha256'],sha(canonical(feedback)))
    w=node('work-items','block','0.1.0',pins['work-items']['sha256'],sha(canonical(items)))
    profile=json.loads((target/'block.manifest.json').read_text())['handoffProfile']
    h=node('feedback-proposal-handoff','bridge','0.1.0',pins['work-items']['sha256'],sha(canonical(profile)),requires=[pin(f),pin(w)])
    fixture=node('feedback-to-work-item','workflow','0.1.0',sha((OUT/'tcp_replay.py').read_bytes()),sha(canonical({'profile':profile,'driver':'tcp_replay.py','scope':'synthetic local workflow acceptance fixture'})),scope='demo',requires=[pin(h)])
    feedback_port=sha(canonical(feedback['components']['schemas']['FeedbackRecord']))
    item_port=sha(canonical(items['components']['schemas']['Item']))
    f['outputs']={'feedback':feedback_port};h['inputs']={'feedback':feedback_port};h['outputs']={'item':item_port};fixture['inputs']={'item':item_port}
    graph={'schema':'gradia.composition.graph.v1','scope':'demo','nodes':[f,w,h,fixture],'connections':[{'fromNode':f['id'],'fromPort':'feedback','toNode':h['id'],'toPort':'feedback'},{'fromNode':h['id'],'fromPort':'item','toNode':fixture['id'],'toPort':'item'}]}
    for name,value in [('work-items-openapi.json',items),('feedback-openapi.json',feedback),('feedback-to-work-item.graph.json',graph),('feedback-to-work-item.lock.json',build_lock(graph,[fixture['id']],'demo')),('recipe-source-pins.json',{'schema':'gradia.recipe.source-pins.v1','implementations':pins,'boundary':'Declared local implementation identity and schema lock; TCP fixture success is separate evidence, not full runtime attestation or arbitrary workflow qualification'})]:
        (OUT/name).write_text(json.dumps(value,indent=2)+'\n')
    (OUT/'feedback-to-work-item.mmd').write_text(diagram(graph,'demo'))
    return graph


if __name__=='__main__':
    build();print('Generated schemas, declared source pins, recipe lock and dependency diagram')
