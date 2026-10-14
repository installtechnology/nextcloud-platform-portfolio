<?php
// Standalone reader for the pinned Nextcloud 34 image, not an application bootstrap.
// Official Docker configs only need these path properties (apps.config.php).
final class OC {
    public static string $SERVERROOT = '/var/www/html';
    public static string $configDir;
}

function readPlatformConfig(string $path): array {
    $handle = fopen($path, 'r');
    if ($handle === false) {
        throw new RuntimeException('CONFIG_READ_FAILED');
    }
    if (!flock($handle, LOCK_SH)) {
        fclose($handle);
        throw new RuntimeException('CONFIG_READ_FAILED');
    }
    ob_start();
    try {
        // Config locals cannot overwrite the guard's state.
        $config = (static function (string $file): array {
            $CONFIG = [];
            if (function_exists('opcache_invalidate')) { opcache_invalidate($file, true); }
            require $file;
            if (!is_array($CONFIG)) { throw new RuntimeException('CONFIG_NOT_ARRAY'); }
            return $CONFIG;
        })($path);
        if (ob_get_length() !== 0) { throw new RuntimeException('CONFIG_EMITTED_OUTPUT'); }
        return $config;
    } finally {
        ob_end_clean();
        flock($handle, LOCK_UN);
        fclose($handle);
    }
}

// Suppress config output and arbitrary exception text, which may contain secrets.
$finished = false;
$reason = 'CONFIG_LOAD_ERROR';
$fileLabel = 'none';
$bufferLevel = ob_get_level();
ob_start();
register_shutdown_function(static function () use (&$finished, $bufferLevel): void {
    if (!$finished) {
        while (ob_get_level() > $bufferLevel) { ob_end_clean(); }
        fwrite(STDERR, "Storage guard: CONFIG_LOAD_ERROR; nenhuma migração realizada\n");
        exit(1); // A config exit/die must never make the guard succeed.
    }
});
set_error_handler(static function (): never {
    throw new RuntimeException('CONFIG_OR_FILESYSTEM_WARNING');
});

try {
    $directory = rtrim($argv[1] ?? '/var/www/html/config', '/\\');
    OC::$configDir = $directory . '/';
    $existing = [];
    // Match OC\Config::readData: main file, natural file order, recursive replacements.
    $extra = glob($directory . '/*.config.php');
    if ($extra === false) { throw new RuntimeException('CONFIG_DISCOVERY_FAILED'); }
    natsort($extra);
    foreach (array_merge([$directory . '/config.php'], $extra) as $file) {
        $fileLabel = preg_replace('/[^A-Za-z0-9_.-]/', '_', basename($file));
        if ($file === $directory . '/config.php' && !file_exists($file)) { continue; }
        $existing = array_replace_recursive($existing, readPlatformConfig($file));
    }
    // Honor NC_ reads, but never let NC_installed hide an installed volume.
    $installed = !empty($existing['installed']) || (bool) getenv('NC_installed');
    $objectstore = getenv('NC_objectstore') !== false ? getenv('NC_objectstore') : ($existing['objectstore'] ?? null);
    $reason = 'EXPECTED_STORAGE_CONFIG_ERROR';
    $fileLabel = 'platform.config.php';
    $expected = readPlatformConfig(__DIR__ . '/platform.config.php');
    $identity = $expected['objectstore'];
    $marker = $directory . '/.platform-storage.json';
    $fileLabel = '.platform-storage.json';
    if (is_file($marker)) {
        $reason = 'MARKER_READ_ERROR';
        $stored = json_decode(file_get_contents($marker), true, 512, JSON_THROW_ON_ERROR);
        $reason = 'MARKER_STORAGE_MISMATCH';
        if ($stored !== $identity) { throw new RuntimeException('STORAGE_CHANGED'); }
    } elseif ($installed) {
        $reason = 'INSTALLED_WITHOUT_STORAGE_MARKER';
        throw new RuntimeException('STORAGE_NOT_ADOPTED');
    }
    // Reject conflicting partial-install config too; never silently switch its storage.
    if (($installed || $objectstore !== null) && $objectstore !== $identity) {
        $reason = 'OBJECTSTORE_CONFIG_MISMATCH';
        throw new RuntimeException('STORAGE_MIGRATION_FORBIDDEN');
    }
    $reason = 'MARKER_WRITE_ERROR';
    if (!is_file($marker) && file_put_contents($marker, json_encode($identity, JSON_THROW_ON_ERROR), LOCK_EX) === false) {
        throw new RuntimeException('STORAGE_IDENTITY_NOT_RECORDED');
    }
    ob_end_clean();
    restore_error_handler();
    $finished = true;
} catch (Throwable $error) {
    while (ob_get_level() > $bufferLevel) { ob_end_clean(); }
    restore_error_handler();
    $finished = true;
    // Filename only: no CONFIG/env values, exception message or stack trace.
    fwrite(STDERR, "Storage guard: $reason; file=$fileLabel; nenhuma migração realizada\n");
    exit(1);
}
