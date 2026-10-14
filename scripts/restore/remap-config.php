<?php
// Private staged configuration only; never bootstrap Nextcloud or install it.
ini_set('display_errors', '0');
ini_set('log_errors', '0');
ini_set('zend.exception_ignore_args', '1');
final class OC {
    public static string $SERVERROOT = '/var/www/html';
    public static string $configDir;
}

function restoreConfigRead(string $file): array {
    if (is_link($file) || !is_file($file) || filesize($file) > 1048576) {
        throw new RuntimeException('RESTORE_CONFIG_FILE_REJECTED');
    }
    $level = ob_get_level(); ob_start();
    try {
        $value = (static function (string $path): array {
            $CONFIG = []; require $path;
            if (!is_array($CONFIG)) { throw new RuntimeException('RESTORE_CONFIG_LOAD_FAILED'); }
            return $CONFIG;
        })($file);
        if (ob_get_length() !== 0) { throw new RuntimeException('RESTORE_CONFIG_LOAD_FAILED'); }
        $check = static function ($item) use (&$check): void {
            if (is_array($item)) { foreach ($item as $child) { $check($child); } return; }
            if (!is_null($item) && !is_scalar($item)) { throw new RuntimeException('RESTORE_CONFIG_LOAD_FAILED'); }
        };
        $check($value); json_encode($value, JSON_THROW_ON_ERROR);
        return $value;
    } finally { while (ob_get_level() > $level) { ob_end_clean(); } }
}

function restoreConfigIdentity(array $config): string {
    $identity = [];
    foreach (['instanceid', 'passwordsalt', 'secret'] as $key) {
        if (!is_string($config[$key] ?? null) || $config[$key] === '') {
            throw new RuntimeException('RESTORE_IDENTITY_MISSING');
        }
        $identity[$key] = $config[$key];
    }
    return hash('sha256', json_encode($identity, JSON_THROW_ON_ERROR));
}

function restoreWriteStaged(string $path, string $data, string $expected): void {
    if (is_link($path) || !is_file($path) || hash_file('sha256', $path) !== $expected) {
        throw new RuntimeException('RESTORE_CONFIG_CONCURRENT_CHANGE');
    }
    $temporary = $path . '.restore-pending';
    $stream = fopen($temporary, 'x');
    if ($stream === false) { throw new RuntimeException('RESTORE_CONFIG_WRITE_FAILED'); }
    try {
        chmod($temporary, 0600);
        if (fwrite($stream, $data) !== strlen($data) || !fflush($stream) || !fsync($stream)) {
            throw new RuntimeException('RESTORE_CONFIG_WRITE_FAILED');
        }
    } finally { fclose($stream); }
    if (hash_file('sha256', $path) !== $expected || !rename($temporary, $path)) {
        throw new RuntimeException('RESTORE_CONFIG_CONCURRENT_CHANGE');
    }
}

function restoreConfiguration(string $directory, array $request): array {
    $fields = ['mode', 'source_bucket', 'target_bucket', 'region', 'database', 'owner',
               'table_prefix', 'identity_sha256', 'maintenance', 'trusted_domains'];
    if (array_diff(array_keys($request), $fields) || array_diff($fields, array_keys($request))
        || !in_array($request['mode'], ['remap', 'verify'], true)
        || !is_bool($request['maintenance']) || !is_array($request['trusted_domains'])
        || !$request['trusted_domains'] || count($request['trusted_domains']) > 16) {
        throw new RuntimeException('RESTORE_CONFIG_REQUEST_REJECTED');
    }
    foreach (['source_bucket', 'target_bucket'] as $key) {
        if (!is_string($request[$key]) || !preg_match('/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/D', $request[$key])) {
            throw new RuntimeException('RESTORE_CONFIG_REQUEST_REJECTED');
        }
    }
    if ($request['source_bucket'] === $request['target_bucket']
        || !preg_match('/^[a-z]{2}(-[a-z]+)+-[0-9]+$/D', $request['region'])
        || !preg_match('/^[a-f0-9]{64}$/D', $request['identity_sha256'])) {
        throw new RuntimeException('RESTORE_CONFIG_REQUEST_REJECTED');
    }
    foreach ($request['trusted_domains'] as $host) {
        if (!is_string($host) || !preg_match('/^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$/D', $host)) {
            throw new RuntimeException('RESTORE_CONFIG_REQUEST_REJECTED');
        }
    }
    if (is_link($directory) || !is_dir($directory)) { throw new RuntimeException('RESTORE_CONFIG_DIRECTORY_REJECTED'); }
    OC::$configDir = rtrim($directory, '/') . '/';
    $extra = glob(OC::$configDir . '*.config.php');
    if ($extra === false || count($extra) > 64) { throw new RuntimeException('RESTORE_CONFIG_LOAD_FAILED'); }
    natsort($extra); $files = array_merge([OC::$configDir . 'config.php'], $extra);
    $loaded = []; $merged = []; $hashes = [];
    foreach ($files as $file) {
        $hashes[$file] = hash_file('sha256', $file);
        $loaded[$file] = restoreConfigRead($file);
        $merged = array_replace_recursive($merged, $loaded[$file]);
    }
    $markerPath = OC::$configDir . '.platform-storage.json';
    if (is_link($markerPath) || !is_file($markerPath) || filesize($markerPath) > 16384) {
        throw new RuntimeException('RESTORE_STORAGE_MARKER_REJECTED');
    }
    $markerHash = hash_file('sha256', $markerPath);
    $marker = json_decode(file_get_contents($markerPath), true, 32, JSON_THROW_ON_ERROR);
    $store = $merged['objectstore'] ?? null;
    $bucket = $request['mode'] === 'remap' ? $request['source_bucket'] : $request['target_bucket'];
    if (($merged['installed'] ?? null) !== true || ($merged['dbtype'] ?? null) !== 'pgsql'
        || ($merged['dbname'] ?? null) !== $request['database'] || ($merged['dbuser'] ?? null) !== $request['owner']
        || ($merged['dbtableprefix'] ?? null) !== $request['table_prefix']
        || ($merged['datadirectory'] ?? null) !== '/var/www/html/data'
        || restoreConfigIdentity($merged) !== $request['identity_sha256']) {
        throw new RuntimeException('RESTORE_INSTALLATION_MISMATCH');
    }
    $expected = ['class' => '\OC\Files\ObjectStore\S3', 'arguments' => [
        'bucket' => $bucket, 'region' => $request['region'], 'use_ssl' => true,
        'use_path_style' => false, 'verify_bucket_exists' => false]];
    if ($store !== $expected || $marker !== $expected || !empty($merged['config_is_read_only'])) {
        throw new RuntimeException('RESTORE_STORAGE_CONFIG_MISMATCH');
    }
    if ($request['mode'] === 'verify') {
        if (($merged['maintenance'] ?? false) !== $request['maintenance']
            || ($merged['trusted_domains'] ?? null) !== $request['trusted_domains']) {
            throw new RuntimeException('RESTORE_CONFIG_POSTCHECK_FAILED');
        }
    } else {
        $replacement = $expected; $replacement['arguments']['bucket'] = $request['target_bucket'];
        // No global string replacement: every independently supplied store must match.
        foreach ($loaded as $file => &$config) {
            if (array_key_exists('objectstore', $config)) {
                if ($config['objectstore'] !== $expected) { throw new RuntimeException('RESTORE_STORAGE_CONFIG_MISMATCH'); }
                $config['objectstore'] = $replacement;
            }
            if ($file === OC::$configDir . 'config.php' || array_key_exists('maintenance', $config)) {
                $config['maintenance'] = $request['maintenance'];
            }
            if ($file === OC::$configDir . 'config.php' || array_key_exists('trusted_domains', $config)) {
                $config['trusted_domains'] = $request['trusted_domains'];
            }
        }
        unset($config);
        // Validate all inputs before the first write. Interrupted staged output is retained.
        foreach ($loaded as $file => $config) {
            if ($config !== restoreConfigRead($file)) {
                restoreWriteStaged($file, "<?php\n\$CONFIG = " . var_export($config, true) . ";\n", $hashes[$file]);
            }
        }
        restoreWriteStaged($markerPath, json_encode($replacement, JSON_THROW_ON_ERROR), $markerHash);
        $request['mode'] = 'verify'; return restoreConfiguration($directory, $request);
    }
    return ['installed' => true, 'identity_matches' => true, 'storage_matches' => true,
            'maintenance' => $request['maintenance'], 'restore_proven' => false];
}

if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') !== __FILE__) { return; }
$finished = false; ob_start();
register_shutdown_function(static function () use (&$finished): void {
    if (!$finished) { while (ob_get_level()) { ob_end_clean(); } exit(1); }
});
set_error_handler(static function (): never { throw new RuntimeException('RESTORE_CONFIG_LOAD_FAILED'); });
try {
    $raw = stream_get_contents(STDIN, 16385);
    if (strlen($raw) > 16384) { throw new RuntimeException('RESTORE_CONFIG_REQUEST_REJECTED'); }
    $request = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($request) || count($argv) !== 2) { throw new RuntimeException('RESTORE_CONFIG_REQUEST_REJECTED'); }
    $result = restoreConfiguration($argv[1], $request);
    while (ob_get_level()) { ob_end_clean(); } $finished = true;
    echo json_encode($result, JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    while (ob_get_level()) { ob_end_clean(); } $finished = true;
    // Never exception text from PHP/config files, values, paths or backtraces.
    fwrite(STDERR, "RESTORE_CONFIG_REJECTED\n"); exit(1);
}
