#!/bin/sh
# Restore-only entrypoint hook: no maintenance:install and no automatic migration.
set -eu
php /opt/platform/storage-guard.php
php /opt/platform/assert-restored.php
