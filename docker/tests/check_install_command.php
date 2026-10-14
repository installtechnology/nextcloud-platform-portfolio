<?php
// Offline API contract against the pinned image. Never bootstrap Nextcloud or execute install.
final class OC { public static string $SERVERROOT; }
$stage = 'AUTOLOAD';
try {
    OC::$SERVERROOT = $argv[1] ?? '/usr/src/nextcloud';
    require OC::$SERVERROOT . '/3rdparty/autoload.php';
    require OC::$SERVERROOT . '/lib/private/Setup.php';
    require OC::$SERVERROOT . '/core/Command/Maintenance/Install.php';
    $stage = 'COMMAND_DEFINITION';
    $reflection = new ReflectionClass(\OC\Core\Command\Maintenance\Install::class);
    $command = $reflection->newInstanceWithoutConstructor();
    // Initialize only Symfony's definition. No SystemConfig, Setup, database or S3.
    (new ReflectionMethod(\Symfony\Component\Console\Command\Command::class, '__construct'))->invoke($command);
    $password = str_repeat('x', 64); // Public fixture, not an operational secret.
    $input = new \Symfony\Component\Console\Input\ArrayInput([
        '--database' => 'pgsql', '--database-host' => 'postgres', '--database-name' => 'fixture_db',
        '--database-user' => 'fixture_user', '--database-pass' => $password,
        '--admin-user' => 'fixture_admin', '--admin-pass' => $password, '--data-dir' => '/var/www/html/data',
    ]);
    $input->setInteractive(false);
    $stage = 'BIND_INPUT';
    $input->bind($command->getDefinition());
    $stage = 'VALIDATE_INPUT';
    $options = (new ReflectionMethod($command, 'validateInput'))->invoke($command, $input,
        new \Symfony\Component\Console\Output\NullOutput(), ['pgsql']);
    if (!$input->hasParameterOption('--database-pass') || $options['dbpass'] !== $password
        || $options['adminpass'] !== $password || $options['dbtype'] !== 'pgsql'
        || $options['dbhost'] !== 'postgres' || count($argv) > 2) {
        throw new RuntimeException('API_CONTRACT_MISMATCH');
    }
    echo "Pinned maintenance:install / ArrayInput contract: OK; install not executed\n";
} catch (Throwable $error) {
    fwrite(STDERR, 'Pinned install API contract: FAIL; stage=' . $stage . '; type=' . get_class($error)
        . '; file=' . basename($error->getFile()) . '; line=' . $error->getLine() . "\n");
    exit(1);
}
