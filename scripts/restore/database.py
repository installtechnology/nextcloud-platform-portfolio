"""Scoped PostgreSQL 17 restore algorithm; injected destination only, no CLI.

No --clean, DROP, installation command, source credentials, automatic retries or
success evidence. Production host identity/lease and role reconstruction must be
provided by the separate destination adapter before this algorithm is activated.
"""
from scripts.backup.sealing.common import require,SealFailure


def restore_database(destination,parameters,dump):
    database=parameters.database['database'];owner=parameters.database['owner']
    # Observation must come from destination, not caller-provided empty flags.
    before=destination.inspect_database(database)
    require(isinstance(before,dict) and set(before)=={'server_major','database','owner','tables','sessions','extensions'}
            and type(before['server_major']) is int and before['server_major']==17
            and before['database']==database and before['owner']==owner,
            'RESTORE_POSTGRES_TARGET_MISMATCH')
    require(before['tables']==[] and before['sessions']==[],'RESTORE_POSTGRES_NOT_EMPTY_OR_IDLE')
    require(before['extensions']==parameters.database['extensions'],'RESTORE_POSTGRES_EXTENSIONS_MISMATCH')
    # Adapter reviews/reconstructs roles/globals before exposing an empty DB.
    # Merely generating a dump or catalog cannot substitute this assertion.
    destination.assert_roles(parameters)
    dump.seek(0);require(dump.read(5)==b'PGDMP','RESTORE_DUMP_FORMAT_INVALID');dump.seek(0)
    destination.check_catalog(dump);dump.seek(0)
    destination.hold_writers()
    try:
        destination.execute(['pg_restore','--exit-on-error','--single-transaction',
                             '--dbname',database],dump)
    except BaseException:raise SealFailure('RESTORE_POSTGRES_FAILED_PRESERVE_DESTINATION') from None
    after=destination.inspect_database(database)
    require(after.get('server_major')==17 and after.get('database')==database and after.get('owner')==owner
            and isinstance(after.get('tables'),list) and len(after['tables'])>0
            and after.get('sessions')==[] and after.get('extensions')==before['extensions'],
            'RESTORE_POSTGRES_POSTCHECK_FAILED')
    destination.release_writers_after_verified_restore()
    return {'database_restored':True,'restore_proven':False,'destroy_authorized':False}
