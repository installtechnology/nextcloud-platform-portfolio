"""Only fixed reason codes and safe metadata may cross the CLI boundary."""
EXIT_INPUT = 2
EXIT_SEMANTIC = 3
EXIT_INTEGRITY = 4
EXIT_INTERNAL = 5


class ValidationFailure(Exception):
    def __init__(self, code, exit_code=EXIT_INPUT, document=None, keyword=None, field=None):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.document = document
        self.keyword = keyword
        self.field = field

    def result(self):
        result = {'ok': False, 'code': self.code, 'functional_restore': 'not_verified'}
        for name in ('document', 'keyword', 'field'):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result
