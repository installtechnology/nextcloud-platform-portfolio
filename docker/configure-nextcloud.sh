#!/bin/sh
# before-starting runs after image synchronization on fresh and interrupted deployments.
set -eu
php /opt/platform/storage-guard.php
if ! cmp -s /opt/platform/platform.config.php /var/www/html/config/zz-platform.config.php; then
    cp /opt/platform/platform.config.php /var/www/html/config/zz-platform.config.php
fi
php /opt/platform/install-nextcloud.php
