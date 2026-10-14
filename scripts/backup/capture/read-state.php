<?php
// Read the pinned OC\Config without bootstrapping the application or writing a marker.
ini_set('display_errors', '0');
ini_set('log_errors', '0');
ini_set('zend.exception_ignore_args', '1');
final class OC {
    public static string $SERVERROOT = '/var/www/html';
    public static string $configDir = '/var/www/html/config/';
}
ob_start();
$finished = false;
register_shutdown_function(static function () use (&$finished): void {
    if (!$finished) { while (ob_get_level()) { ob_end_clean(); } exit(1); }
});
set_error_handler(static function (): never { throw new RuntimeException('CONFIG_ERROR'); });
try {
    define('OC_CONSOLE', 1);
    require '/var/www/html/lib/private/Config.php';
    $config = new \OC\Config(OC::$configDir);
    $store = $config->getValue('objectstore');
    $marker = json_decode(file_get_contents(OC::$configDir . '.platform-storage.json'), true, 32, JSON_THROW_ON_ERROR);
    if ($config->getValue('installed') !== true || $config->getValue('dbtype') !== 'pgsql'
        || $config->getValue('datadirectory') !== '/var/www/html/data'
        || $store !== $marker || ($store['class'] ?? '') !== '\OC\Files\ObjectStore\S3'
        || isset($store['arguments']['key']) || isset($store['arguments']['secret'])
        || getenv('NC_maintenance') !== false || $config->getValue('config_is_read_only', false)) {
        throw new RuntimeException('INCOMPATIBLE_CONFIG');
    }
    $identity = [];
    foreach (['instanceid', 'passwordsalt', 'secret'] as $key) {
        $value = $config->getValue($key);
        if (!is_string($value) || $value === '') { throw new RuntimeException('IDENTITY_MISSING'); }
        $identity[$key] = $value;
    }
    $maintenance = $config->getValue('maintenance', false);
    if (!is_bool($maintenance)) { throw new RuntimeException('MAINTENANCE_INVALID'); }
    $files = array_merge([OC::$configDir . 'config.php'], glob(OC::$configDir . '*.config.php'));
    natsort($files);
    $fingerprint = [];
    foreach ($files as $file) { $fingerprint[basename($file)] = hash_file('sha256', $file); }
    $result = ['installed' => true, 'maintenance' => $maintenance,
        'version' => $config->getValue('version'), 'database' => $config->getValue('dbname'),
        'owner' => $config->getValue('dbuser'), 'table_prefix' => $config->getValue('dbtableprefix'),
        'installation_identity_sha256' => hash('sha256', json_encode($identity, JSON_THROW_ON_ERROR)),
        'config_fingerprint' => hash('sha256', json_encode($fingerprint, JSON_THROW_ON_ERROR))];
    while (ob_get_level()) { ob_end_clean(); }
    $finished = true;
    echo json_encode($result, JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    while (ob_get_level()) { ob_end_clean(); }
    $finished = true;
    exit(1); // No config values, file paths, warnings, errors or stack trace.
}
