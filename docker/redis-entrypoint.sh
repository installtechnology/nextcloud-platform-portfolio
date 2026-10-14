#!/bin/sh
# Redis oficial não tem PASSWORD_FILE; manter senha fora de argv do servidor.
set -eu
umask 077
password=$(cat /run/secrets/redis_password)
case "$password" in
  ''|*[!A-Za-z0-9_-]*) echo 'Redis secret inválido' >&2; exit 1 ;;
esac
printf 'bind 0.0.0.0\nprotected-mode yes\nsave ""\nappendonly no\nrequirepass "%s"\n' "$password" > /tmp/platform-redis.conf
unset password
exec redis-server /tmp/platform-redis.conf
