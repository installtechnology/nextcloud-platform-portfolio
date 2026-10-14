"""Local CLI; emits sanitized JSON and never prints exception messages."""
import argparse
import json

from .errors import ValidationFailure
from .validator import validate


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValidationFailure('INVALID_ARGUMENTS')


def main(argv=None):
    parser = Parser(description='Offline recovery v1/v2 conformity and artifact integrity; no restore proof.')
    parser.add_argument('--root', required=True)
    parser.add_argument('--mode', choices=('documents', 'local'), default='local')
    parser.add_argument('--manifest', default='manifest.json')
    parser.add_argument('--inventory', default='inventory.json')
    parser.add_argument('--completion', default='completion.json')
    parser.add_argument('--evidence')
    try:
        args = parser.parse_args(argv)
        options = vars(args)
        options['root_path'] = options.pop('root')
        result = validate(**options)
        code = 0
    except ValidationFailure as error:
        result, code = error.result(), error.exit_code
    except KeyboardInterrupt:
        result, code = {'ok': False, 'code': 'INTERRUPTED', 'functional_restore': 'not_verified'}, 130
    except Exception:
        result, code = {'ok': False, 'code': 'INTERNAL_ERROR', 'functional_restore': 'not_verified'}, 5
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
