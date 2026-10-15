"""Verify a distribution against its recorded static screen; never execute skills."""
import argparse
import json
from pathlib import Path
from .projection import build_projection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    expected = json.loads((args.root/'sources/screening.json').read_text())
    rows, resources, current = build_projection(args.root)
    required = {r['id']:r['contentSha256'] for r in expected['records'] if r['status']=='screened-reference'}
    actual = {r['id']:r['contentSha256'] for r in current['records'] if r['status']=='screened-reference'}
    if required != actual:
        raise SystemExit('Static distribution identities differ from recorded screen; review and regenerate deliberately')
    print(json.dumps({'screenedUpstream':len(actual),'references':len(rows),'retrievable':current['projection']['retrievable'],'resources':len(resources),'behavioral':'not_evaluated','candidateExecution':False}))


if __name__ == '__main__':
    main()
