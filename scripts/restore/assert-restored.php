<?php
// Read-only precondition; do not bootstrap/install the application.
ini_set('display_errors', '0'); ini_set('log_errors', '0');
ini_set('zend.exception_ignore_args', '1');
final class OC {
    public static string $SERVERROOT = '/var/www/html';
    public static string $configDir = '/var/www/html/config/';
}
$finished = false; ob_start();
register_shutdown_function(static function () use (&$finished): void {
    if (!$finished) { while (ob_get_level()) { ob_end_clean(); } exit(1); }
});
set_error_handler(static function (): never { throw new RuntimeException('RESTORE_CONFIG_ERROR'); });
try {
    require '/var/www/html/lib/private/Config.php';
    $config = new \OC\Config(OC::$configDir);
    $identity = [];
    foreach (['instanceid', 'passwordsalt', 'secret'] as $key) {
        $value = $config->getValue($key);
        if (!is_string($value) || $value === '') { throw new RuntimeException('RESTORE_IDENTITY_MISSING'); }
        $identity[$key] = $value;
    }
    $maintenance = getenv('RESTORE_EXPECTED_MAINTENANCE');
    if (!in_array($maintenance, ['true', 'false'], true)
        || $config->getValue('installed') !== true || $config->getValue('dbtype') !== 'pgsql'
        || $config->getValue('maintenance', false) !== ($maintenance === 'true')
        || $config->getValue('datadirectory') !== '/var/www/html/data'
        || hash('sha256', json_encode($identity, JSON_THROW_ON_ERROR)) !== getenv('RESTORE_EXPECTED_IDENTITY_SHA256')) {
        throw new RuntimeException('RESTORE_NOT_INSTALLED_OR_IDENTITY_MISMATCH');
    }
    while (ob_get_level()) { ob_end_clean(); } $finished = true;
    echo "RESTORED_INSTALLATION_PRECONDITION_PASSED\n";
} catch (Throwable $error) {
    while (ob_get_level()) { ob_end_clean(); } $finished = true;
    fwrite(STDERR, "RESTORED_INSTALLATION_PRECONDITION_FAILED\n"); exit(1);
}
