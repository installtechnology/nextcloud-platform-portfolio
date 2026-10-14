<?php
// Explicit hook before the unchanged installer. Does not initialize Nextcloud.
ini_set('display_errors', '0');
ini_set('log_errors', '0');
ini_set('zend.exception_ignore_args', '1');
try {
    $allowed = [
        'AWS_EC2_METADATA_SERVICE_ENDPOINT' => 'https://imds.sandbox.test:1338',
        'AWS_EC2_METADATA_V1_DISABLED' => 'true',
    ];
    foreach (getenv() as $name => $value) {
        if (str_starts_with($name, 'AWS_') && (!isset($allowed[$name]) || $allowed[$name] !== $value)) {
            throw new RuntimeException('REJECTED');
        }
        if (str_starts_with($name, 'NC_') || in_array(strtoupper($name), ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'], true)) {
            throw new RuntimeException('REJECTED');
        }
    }
    foreach ($allowed as $name => $value) {
        if (getenv($name) !== $value) { throw new RuntimeException('REJECTED'); }
    }
    if (getenv('PLATFORM_SYNTHETIC_SANDBOX') !== 's3-imds-v1'
        || preg_match('/^nc-s3-imds-test-[a-f0-9]{16}$/D', (string)getenv('PLATFORM_SANDBOX_PROJECT')) !== 1
        || (getenv('HOME') !== false && !in_array(getenv('HOME'), ['/root','/var/www'], true))
        || getenv('POSTGRES_HOST') !== 'postgres' || getenv('POSTGRES_DB') !== 'fixture_db'
        || getenv('POSTGRES_USER') !== 'fixture_app' || getenv('PLATFORM_ADMIN_USER') !== 'fixture_admin'
        || getenv('NEXTCLOUD_S3_BUCKET') !== 'fixture-data'
        || ini_get('curl.cainfo') !== '/fixture/ca.pem' || ini_get('openssl.cafile') !== '/fixture/ca.pem'
        || !is_file('/fixture/ca.pem') || is_link('/fixture/ca.pem')
        || file_exists('/root/.aws/credentials') || file_exists('/root/.aws/config')
        || file_exists('/var/www/.aws/credentials') || file_exists('/var/www/.aws/config')) {
        throw new RuntimeException('REJECTED');
    }
    // No SDK network call: compare declared bundled version before any install.
    require_once '/var/www/html/3rdparty/autoload.php';
    if (\Aws\Sdk::VERSION !== '3.376.2') { throw new RuntimeException('REJECTED'); }
    echo "Sandbox preflight: INPUTS_VERIFIED_NOT_ISOLATION_PROOF\n";
} catch (Throwable $error) {
    fwrite(STDERR, "Sandbox preflight: REJECTED\n");
    exit(2);
}
