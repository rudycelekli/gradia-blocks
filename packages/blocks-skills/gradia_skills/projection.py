"""Build selectively retrievable text projections from a fresh static screen."""
from pathlib import Path
import hashlib
import json
import re
from .intake import digest, frontmatter, screen_catalog, _safe, SUSPICIOUS


def build_projection(root: Path):
    root = Path(root)
    manifest_path = root / 'sources/sources.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'schemaVersion':1,'sources':[],'candidates':[]}
    report = screen_catalog(root, manifest)
    if report['errors']:
        raise ValueError('Skill catalog screen failed: ' + ','.join(report['errors']))
    rows, resources = [], []
    sources = {s['id']:s for s in manifest['sources']}
    candidates = {s['id']:s for s in manifest['candidates']}
    skills_root = _safe(root,'skills')
    for path in sorted(skills_root.glob('*.md')):
        if path.is_symlink() or path.stat().st_nlink > 1 or path.stat().st_size > 262144:
            raise ValueError('Unsafe first-party skill')
        raw = path.read_bytes(); text = raw.decode(); meta = frontmatter(text)
        if any(len(line)>8192 for line in text.splitlines()):
            raise ValueError('First-party line length exceeds bound')
        if any(re.search(pattern,text,flags=re.I) for pattern in SUSPICIOUS):
            raise ValueError('First-party authority instruction requires review')
        links = [link for line in text.splitlines() for link in re.findall(r'!?\[[^\[\]]*\]\(([^\s)]+)', line)]
        links += re.findall(r'^[ \t]{0,3}\[[^\[\]\r\n]+\]:[ \t]*<?([^\s>]+)>?',text,flags=re.M)
        for link in links:
            if not link.startswith(('https://','http://','mailto:','#')):
                target = _safe(path.parent,link.split('#')[0])
                if not target.exists(): raise ValueError('Missing first-party resource')
        if not meta.get('name') or not meta.get('description') or not re.fullmatch(r'\d+\.\d+\.\d+',meta.get('version','')):
            raise ValueError('First-party skill needs versioned frontmatter')
        rows.append({'id':path.stem,'version':meta['version'],'name':path.stem.replace('-',' ').title(),'description':meta['description'],'category':'composition' if path.stem=='compose-verified-skills' else 'assurance' if path.stem in ['assess-upstream-skill','review-block-candidate'] else 'integration','license':'Apache-2.0','origin':'first-party','kind':'reference-skill','contentSha256':digest(raw),'source':'packages/blocks-skills/skills/'+path.name,'content':text,'resources':[],'requiredPermissions':['Explicit user intent','Separately authorized tool access'],'quality':{'status':'screened-reference','checks':{'firstPartyVersionAndHash':'pass'},'behavioral':'not_evaluated'},'dependencyStatus':'Host tools and permissions require explicit verification','status':'Versioned reference; enterprise admission remains open'})
    for screened in report['records']:
        candidate = candidates[screened['id']]; source = sources.get(candidate.get('sourceId'),{})
        passed = screened['status']=='screened-reference'
        raw = b''
        if passed:
            raw = _safe(_safe(root,candidate['path']),candidate['entrypoint']).read_bytes()
            if digest(raw)!=screened['contentSha256']:
                raise ValueError('Source changed after screening')
        metadata = frontmatter(raw.decode()) if passed else {}
        version = 'upstream-' + candidate['commit'][:12]
        if re.fullmatch(r'\d+\.\d+\.\d+', metadata.get('version','')):
            version = metadata['version'] + '+' + candidate['commit'][:12]
        row = {'id':candidate['id'],'name':candidate['name'],'description':metadata.get('description','Upstream candidate retained for review; instructions unavailable until screening passes.')[:1200],'category':candidate['category'],'origin':'upstream','kind':'upstream-reference','version':version,'license':candidate['license'],'contentSha256':screened['contentSha256'] or next((f['sha256'] for f in candidate['files'] if f['path']==candidate['entrypoint']),''),'source':candidate['path']+'/'+candidate['entrypoint'],'upstream':{'repository':source.get('repository'),'commit':candidate['commit'],'path':candidate.get('sourceUrl','').split(candidate['commit']+'/',1)[-1],'sourceUrl':candidate['sourceUrl'],'licensePath':source.get('licensePath'),'licenseSha256':source.get('licenseSha256')},'quality':{'status':screened['status'],'checks':screened['checks'],'reasons':screened['reasons'],'behavioral':'not_evaluated'},'resources':screened['resources'],'requiresTools':[d['id'] for d in candidate.get('dependencies',[]) if d.get('kind')=='host-tool' and isinstance(d.get('id'),str)],'dependencies':[{'id':d['id'],'kind':'skill','status':next((r['status'] for r in report['records'] if r['id']==d['id']),'not_in_catalog')} for d in candidate.get('dependencies',[]) if d.get('kind')=='skill' and isinstance(d.get('id'),str)],'dependencyStatus':'Not verified in the target host','overlaps':screened.get('overlaps',[]),'requiredPermissions':['Explicit user intent','Separately authorized host tools','Applicable upstream license obligations'],'status':'Statically screened reference; behavioral qualification open' if passed else 'Quarantined; metadata only'}
        if screened.get('duplicateOf'):row['duplicateOf']=screened['duplicateOf']
        if passed:
            row['content']=raw.decode()
            for resource in screened['resources']:
                data = _safe(_safe(root,candidate['path']),resource['name']).read_bytes()
                if digest(data)!=resource['contentSha256']:
                    raise ValueError('Resource changed after screening')
                resources.append({'skillId':candidate['id'],**resource,'content':data.decode()})
        rows.append(row)
    if len({r['id'] for r in rows})!=len(rows):
        raise ValueError('Skill identity collides with an existing namespace')
    report['projection']={'skills':len(rows),'retrievable':sum(r['quality']['status']=='screened-reference' for r in rows),'resources':len(resources),'firstParty':sum(r['origin']=='first-party' for r in rows)}
    return rows,resources,report
