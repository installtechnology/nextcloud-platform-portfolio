"""Supervised PG17 Unix-socket capture; no password or role secrets in argv.

Uses PR3 dump format/catalog conventions. Collector must also fence connection
sources; zero current sessions alone is not a fence against future sessions.
"""
import io
import re
from ..capture.storage import CaptureFailure
from ..json_input import parse_json


class Limited(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > 1024 * 1024:
            raise CaptureFailure('COMMAND_OUTPUT_LIMIT')
        return super().write(data)


class Discard:
    def write(self, data):
        return len(data)


class PostgreSQL:
    def __init__(self, supervisor, operation, container, database):
        if (not isinstance(database, str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,62}', database)
                or not isinstance(container, str) or not re.fullmatch(r'[a-f0-9]{64}', container)):
            raise CaptureFailure('POSTGRES_TARGET_INVALID')
        supervisor.store.operation(operation)
        self.supervisor, self.operation, self.container, self.database = supervisor, operation, container, database

    def command(self, executable, args, output, timeout, input_file=None):
        if executable not in ('pg_dump', 'pg_dumpall', 'psql'):
            raise CaptureFailure('POSTGRES_COMMAND_REJECTED')
        self.supervisor.execute(self.operation, self.container,
            [executable, '--host=/var/run/postgresql', '--username=postgres', *args],
            'postgres', output, timeout, input_file)

    def compatibility(self):
        output = Limited()
        self.command('psql', ['-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '--dbname', self.database,
            '-c', "SELECT current_setting('server_version_num')::int"], output, 30)
        try:
            version = int(output.getvalue().strip())
        except ValueError:
            raise CaptureFailure('POSTGRES_VERSION_INVALID') from None
        if version // 10000 != 17:
            raise CaptureFailure('POSTGRES_MAJOR_MISMATCH')
        output = Limited()
        # --version is a local executable check, not a database connection.
        # PostgreSQL handles it before parsing other connection arguments.
        self.supervisor.execute(self.operation, self.container,
            ['pg_dump', '--version'], 'postgres', output, 30)
        if not re.search(rb'\b17\.', output.getvalue()):
            raise CaptureFailure('DUMP_CLIENT_MAJOR_MISMATCH')

    def sessions(self):
        output = Limited()
        # Inspect all client sessions, excluding only this query's own PID.
        # Do not expose user/query/IP fields or consider ncbackup names exempt.
        query = "SELECT json_build_object('sessions',coalesce(json_agg(pid),'[]'::json)) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid()"
        self.command('psql', ['-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '--dbname', self.database, '-c', query], output, 30)
        try:
            document = parse_json(output.getvalue())
            if set(document) != {'sessions'}:
                raise CaptureFailure('SESSION_INSPECTION_REQUIRED')
            sessions = document['sessions']
        except Exception:
            raise CaptureFailure('SESSION_INSPECTION_REQUIRED') from None
        if not isinstance(sessions, list) or any(type(pid) is not int or pid <= 0 for pid in sessions):
            raise CaptureFailure('SESSION_INSPECTION_REQUIRED')
        return sessions

    def dump(self, output):
        self.command('pg_dump', ['--format=custom', '--create', '--dbname', self.database], output, 600)

    def check_dump(self, source):
        # Caller supplies an already confined, private regular-file descriptor.
        if source.read(5) != b'PGDMP':
            raise CaptureFailure('DUMP_FORMAT_INVALID')
        source.seek(0)
        self.supervisor.execute(self.operation, self.container, ['pg_restore', '--list'],
                                'postgres', Discard(), 300, source)

    def globals(self):
        output = Limited()
        self.command('pg_dumpall', ['--globals-only', '--no-role-passwords'], output, 120)
        return output.getvalue()  # Sensitive owner/role metadata: protected artifact only.
