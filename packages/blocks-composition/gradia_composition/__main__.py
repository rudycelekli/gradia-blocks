"""Operator CLI. Reads explicitly supplied local files; never fetches or executes nodes."""
import argparse
import json
import sys
from .core import CompositionError, read_json, build_lock, compare_graphs, diagram

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['lock','impact','diagram'])
    parser.add_argument('--graph', required=True)
    parser.add_argument('--candidate')
    parser.add_argument('--scope', required=True, help='Trusted operator tenant scope, or public')
    parser.add_argument('--root', action='append', default=[])
    args = parser.parse_args()
    try:
        graph = read_json(args.graph)
        if args.command == 'diagram':
            if args.candidate or args.root:
                raise CompositionError('unsupported_option')
            print(diagram(graph,args.scope),end='')
        else:
            if args.command == 'impact':
                if not args.candidate:
                    raise CompositionError('candidate_required')
                result = compare_graphs(graph,read_json(args.candidate),args.root,args.scope)
            else:
                if args.candidate:
                    raise CompositionError('unsupported_option')
                result = build_lock(graph,args.root,args.scope)
            print(json.dumps(result,sort_keys=True,indent=2))
        return 0
    except CompositionError as error:
        print(json.dumps({'status':'REFUSED','reason':str(error),'promotionAuthorized':False}),file=sys.stderr)
        return 2

if __name__ == '__main__':
    sys.exit(main())
