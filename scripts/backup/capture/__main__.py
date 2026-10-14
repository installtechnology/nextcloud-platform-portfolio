"""No operational mode: existing production project names are rejected before Docker."""
import argparse
import json
import signal

from .docker_backend import DockerBackend
from .engine import Capture
from .storage import CaptureFailure, Store


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CaptureFailure('INVALID_ARGUMENTS')


def main(argv=None):
    parser = Parser(description='PR3 isolated local capture candidate only; no AWS, transfer or restore.')
    parser.add_argument('action', choices=('start', 'recover'))
    parser.add_argument('--isolated-project', required=True)
    parser.add_argument('--fixture-root', required=True)
    parser.add_argument('--staging', required=True)
    previous = {}
    try:
        args = parser.parse_args(argv)
        backend = DockerBackend(args.isolated_project, args.fixture_root)
        store = Store(args.staging)
        for signum in (signal.SIGTERM, signal.SIGINT):
            def cancel(_signum, _frame):
                raise CaptureFailure('CAPTURE_CANCELLED')
            previous[signum] = signal.signal(signum, cancel)
        capture = Capture(store, backend)
        result = capture.start() if args.action == 'start' else capture.recover()
        code = 0
    except CaptureFailure as error:
        result, code = {'code': error.code, 'backup_complete': False, 'functional_restore': 'not_verified'}, 2
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        result, code = {'code': 'LOCAL_CAPTURE_ERROR', 'backup_complete': False, 'functional_restore': 'not_verified'}, 3
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
