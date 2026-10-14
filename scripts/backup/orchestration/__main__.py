"""Always refuses execution; no input, variable or simulator can activate backup."""
import json
import os
import sys

from .gates import GateFailure, blocked_result, dispatch_context


def main(argv=None, context=None):
    # Exit 2: invalid context; 78: disabled by design; 3: internal failure.
    try:
        if argv is None:
            argv = sys.argv[1:]
        if argv:
            raise GateFailure('INVALID_ARGUMENTS')
        dispatch_context(os.environ if context is None else context)
        result, status = blocked_result(), 78
    except GateFailure as error:
        result = blocked_result(error.code)
        status = 3 if result['code'] == 'INTERNAL_GATE_ERROR' else 2
    except Exception:
        result, status = blocked_result('INTERNAL_GATE_ERROR'), 3
    print(json.dumps(result, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
