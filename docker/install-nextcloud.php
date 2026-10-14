<?php
// Pinned Nextcloud 34: invoke the official maintenance:install command in memory.
// No passwords in argv, shell, output or a generated command/artifact.
final class PlatformInstallFailure extends RuntimeException {}

function completePlatformInstallation(array $adapter): string {
    ($adapter['guard'])();
    $state = ($adapter['state'])();
    if (!$state['storage_matches']) { throw new PlatformInstallFailure('STORAGE_CONFIG_MISMATCH'); }
    if (!$state['install_config_matches']) { throw new PlatformInstallFailure('INSTALL_CONFIG_MISMATCH'); }
    if ($state['installed']) { return 'ALREADY_INSTALLED'; }
    if (!($adapter['local_data_safe'])()) { throw new PlatformInstallFailure('LOCAL_DATA_NOT_EMPTY'); }
    if (!($adapter['database_empty'])()) { throw new PlatformInstallFailure('DATABASE_NOT_EMPTY'); }
    if (!($adapter['bucket_empty'])()) { throw new PlatformInstallFailure('DATA_BUCKET_NOT_EMPTY'); }
    // Exactly one attempt. A failed attempt with schema/objects is never retried automatically.
    if (($adapter['install'])() !== 0) { throw new PlatformInstallFailure('INSTALL_FAILED'); }
    $after = ($adapter['state'])();
    if (!$after['installed'] || !$after['storage_matches']) {
        throw new PlatformInstallFailure('INSTALL_NOT_CONFIRMED');
    }
    return 'INSTALLED';
}

function runPlatformInstallation(array $adapter, string $configDirectory): int {
    ini_set('display_errors', '0');
    ini_set('log_errors', '0');
    ini_set('zend.exception_ignore_args', '1');
    $level = ob_get_level();
    ob_start();
    $finished = false;
    register_shutdown_function(static function () use (&$finished, $level): void {
        if (!$finished) {
            while (ob_get_level() > $level) { ob_end_clean(); }
            fwrite(STDERR, "Platform install: INITIAL_INSTALL_ERROR\n");
            exit(1);
        }
    });
    set_error_handler(static function (int $severity): bool {
        // Preserve vendor @-suppression (e.g. optional files) without leaking warnings.
        if (!(error_reporting() & $severity)) { return true; }
        throw new RuntimeException('SUPPRESSED_WARNING');
    });
    $lock = null;
    try {
        $lock = fopen($configDirectory . '/.platform-install.lock', 'c');
        if ($lock === false || !flock($lock, LOCK_EX)) { throw new PlatformInstallFailure('INSTALL_LOCK_ERROR'); }
        $result = completePlatformInstallation($adapter);
        $exit = 0;
    } catch (Throwable $error) {
        // Only our fixed reason codes; never vendor/PDO exception text or traces.
        $result = $error instanceof PlatformInstallFailure ? $error->getMessage() : 'INITIAL_INSTALL_ERROR';
        $exit = 1;
    } finally {
        if (is_resource($lock)) { flock($lock, LOCK_UN); fclose($lock); }
        while (ob_get_level() > $level) { ob_end_clean(); }
        restore_error_handler();
        $finished = true;
    }
    fwrite($exit === 0 ? STDOUT : STDERR, "Platform install: $result\n");
    return $exit;
}

function platformInstallSecret(string $name, string $expectedPath): string {
    if (getenv($name) !== $expectedPath) { throw new PlatformInstallFailure('SECRET_INPUT_INVALID'); }
    $value = trim(file_get_contents($expectedPath));
    if (!preg_match('/^[A-Za-z0-9_-]{32,256}$/D', $value)) { throw new PlatformInstallFailure('SECRET_INPUT_INVALID'); }
    return $value;
}

function platformLocalDataSafe(string $path): bool {
    if (!is_dir($path)) { return !file_exists($path); }
    foreach (scandir($path) as $name) {
        if ($name === '.' || $name === '..') { continue; }
        if (!in_array($name, ['.htaccess', 'index.html', '.ncdata', '.ocdata', 'nextcloud.log'], true)
            || !is_file($path . '/' . $name) || is_link($path . '/' . $name)) { return false; }
    }
    return true;
}

function productionInstallationAdapter(): array {
    $booted = false;
    $settings = static function (): array {
        $db = (string) getenv('POSTGRES_DB');
        $user = (string) getenv('POSTGRES_USER');
        $admin = (string) getenv('PLATFORM_ADMIN_USER');
        if (getenv('POSTGRES_HOST') !== 'postgres'
            || !preg_match('/^[a-z][a-z0-9_]{1,62}$/D', $db)
            || !preg_match('/^[a-z][a-z0-9_]{1,62}$/D', $user) || $user === 'postgres'
            || !preg_match('/^[A-Za-z0-9][A-Za-z0-9_.@-]+$/D', $admin)) {
            throw new PlatformInstallFailure('INSTALL_INPUT_INVALID');
        }
        return ['dbtype' => 'pgsql', 'dbhost' => 'postgres', 'dbname' => $db,
            'dbuser' => $user, 'datadirectory' => '/var/www/html/data', 'dbtableprefix' => 'oc_', 'admin' => $admin];
    };
    return [
        'guard' => static function (): void {
            $process = proc_open([PHP_BINARY, __DIR__ . '/storage-guard.php'],
                [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
            if (!is_resource($process)) { throw new PlatformInstallFailure('STORAGE_GUARD_FAILED'); }
            fclose($pipes[0]);
            stream_get_contents($pipes[1]); fclose($pipes[1]);
            stream_get_contents($pipes[2]); fclose($pipes[2]);
            if (proc_close($process) !== 0) { throw new PlatformInstallFailure('STORAGE_GUARD_FAILED'); }
        },
        'state' => static function () use (&$booted, $settings): array {
            if (!$booted) {
                define('OC_CONSOLE', 1);
                chdir('/var/www/html');
                require '/var/www/html/lib/base.php';
                $booted = true;
            }
            // Fresh reader after installation: no stale installed flag from a config cache.
            $config = new \OC\Config(\OC::$configDir);
            $expected = (static function (): array { require __DIR__ . '/platform.config.php'; return $CONFIG; })();
            $matches = true;
            foreach ($settings() as $key => $value) {
                if ($key === 'admin') { continue; }
                $current = $config->getValue($key, '');
                if ($current !== '' && $current !== $value) { $matches = false; }
            }
            $installed = $config->getValue('installed', false);
            if (!is_bool($installed)) { throw new PlatformInstallFailure('INSTALL_CONFIG_MISMATCH'); }
            return ['installed' => $installed,
                'storage_matches' => $config->getValue('objectstore') === $expected['objectstore'],
                'install_config_matches' => $matches];
        },
        'database_empty' => static function () use ($settings): bool {
            $s = $settings();
            $db = new PDO('pgsql:host=postgres;port=5432;dbname=' . $s['dbname'], $s['dbuser'],
                platformInstallSecret('PLATFORM_POSTGRES_PASSWORD_FILE', '/run/secrets/postgres_password'),
                [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_TIMEOUT => 10]);
            $db->exec('BEGIN READ ONLY');
            // Dedicated application DB: refuse any user relation, function, domain/enum or extra schema.
            $count = $db->query("SELECT
                (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') +
                (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') +
                (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
                    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND t.typtype IN ('d','e')) +
                (SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema'))")->fetchColumn();
            $db->exec('COMMIT');
            return (int) $count === 0;
        },
        'local_data_safe' => static function (): bool {
            return platformLocalDataSafe('/var/www/html/data');
        },
        'bucket_empty' => static function (): bool {
            // Default credential provider => EC2 Instance Role, never static credentials.
            $client = new \Aws\S3\S3Client(['version' => 'latest', 'region' => getenv('NEXTCLOUD_S3_REGION')]);
            $result = $client->listObjectsV2(['Bucket' => getenv('NEXTCLOUD_S3_BUCKET'), 'MaxKeys' => 1]);
            return isset($result['KeyCount']) && (int) $result['KeyCount'] === 0 && empty($result['Contents']);
        },
        'install' => static function () use ($settings): int {
            $s = $settings();
            // Symfony options are PHP objects only: not OS process arguments or $_SERVER['argv'].
            $input = new \Symfony\Component\Console\Input\ArrayInput([
                '--database' => 'pgsql', '--database-host' => 'postgres', '--database-name' => $s['dbname'],
                '--database-user' => $s['dbuser'],
                '--database-pass' => platformInstallSecret('PLATFORM_POSTGRES_PASSWORD_FILE', '/run/secrets/postgres_password'),
                '--admin-user' => $s['admin'],
                '--admin-pass' => platformInstallSecret('PLATFORM_ADMIN_PASSWORD_FILE', '/run/secrets/nextcloud_admin_password'),
                '--data-dir' => $s['datadirectory'],
            ]);
            $input->setInteractive(false);
            $command = \OCP\Server::get(\OC\Core\Command\Maintenance\Install::class);
            return $command->run($input, new \Symfony\Component\Console\Output\NullOutput());
        },
    ];
}

if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    exit(runPlatformInstallation(productionInstallationAdapter(), '/var/www/html/config'));
}
