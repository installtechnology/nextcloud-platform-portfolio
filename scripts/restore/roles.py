"""Recognize the project's PG17 globals recipe; never execute captured SQL."""
import re
from scripts.backup.sealing.common import require

FLAGS={'SUPERUSER':'superuser','CREATEDB':'createdb','CREATEROLE':'createrole',
       'INHERIT':'inherit','LOGIN':'login','REPLICATION':'replication','BYPASSRLS':'bypassrls'}
ALLOWED_SET={"SET default_transaction_read_only = off","SET client_encoding = 'UTF8'",
             'SET standard_conforming_strings = on',"SELECT pg_catalog.set_config('search_path', '', false)"}


def role_recipe(sql,owner):
    require(isinstance(sql,str) and len(sql)<=65536 and re.fullmatch('[a-z][a-z0-9_]{1,62}',owner)
            and owner!='postgres','RESTORE_GLOBALS_REVIEW_REQUIRED')
    lines=[]
    for line in sql.splitlines():
        line=line.strip()
        if not line or line.startswith('--'):continue
        if re.fullmatch(r'\\(?:unrestrict|restrict) [A-Za-z0-9]{16,128}',line):continue
        require(not line.startswith('\\'),'RESTORE_GLOBALS_REVIEW_REQUIRED');lines.append(line)
    created=set();roles={}
    for raw in ' '.join(lines).split(';'):
        statement=' '.join(raw.split())
        if not statement or statement in ALLOWED_SET:continue
        create=re.fullmatch(r'CREATE ROLE ([a-z][a-z0-9_]{1,62})',statement)
        alter=re.fullmatch(r'ALTER ROLE ([a-z][a-z0-9_]{1,62}) WITH ([A-Z ]+)',statement)
        if create:
            name=create[1];require(name in (owner,'postgres') and name not in created,'RESTORE_GLOBALS_REVIEW_REQUIRED')
            created.add(name);continue
        require(alter is not None,'RESTORE_GLOBALS_REVIEW_REQUIRED');name=alter[1]
        require(name in (owner,'postgres') and name not in roles,'RESTORE_GLOBALS_REVIEW_REQUIRED')
        flags={}
        for token in alter[2].split():
            base=token[2:] if token.startswith('NO') else token
            require(base in FLAGS and FLAGS[base] not in flags,'RESTORE_GLOBALS_REVIEW_REQUIRED')
            flags[FLAGS[base]]=not token.startswith('NO')
        require(set(flags)==set(FLAGS.values()),'RESTORE_GLOBALS_REVIEW_REQUIRED');roles[name]=flags
    require(created==set(roles)=={owner,'postgres'},'RESTORE_GLOBALS_REVIEW_REQUIRED')
    require(not any(roles[owner][k] for k in ('superuser','createdb','createrole','replication','bypassrls'))
            and roles[owner]['login'],'RESTORE_OWNER_PRIVILEGES_REJECTED')
    return roles
