#!/bin/sh
# Executado pela imagem PostgreSQL somente quando PGDATA está vazio.
set -eu
APP_DB_PASSWORD=$(cat /run/secrets/postgres_password)
export APP_DB_PASSWORD
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_user APP_POSTGRES_USER
\getenv app_db APP_POSTGRES_DB
\getenv app_password APP_DB_PASSWORD
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'app_user', :'app_password') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'app_db', :'app_user') \gexec
SQL
unset APP_DB_PASSWORD
