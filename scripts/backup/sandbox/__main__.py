"""Offline static check only. There is no start/capture/restore command."""
import json
import sys
from ..capture.storage import CaptureFailure
from .validation import compose_model, validate_compose


def main(argv=None):
    try:
        args = sys.argv[1:] if argv is None else argv
        if args:
            raise CaptureFailure('SANDBOX_NO_EXECUTION_MODE')
        report, status = validate_compose(compose_model()), 0
    except CaptureFailure as error:
        report, status = {'code': error.code, 'runtime_allowed': False, 'backup_complete': False}, 2
    except Exception:
        report, status = {'code': 'SANDBOX_STATIC_CHECK_FAILED', 'runtime_allowed': False, 'backup_complete': False}, 3
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
