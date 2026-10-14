<?php
// Sandbox-only config, mounted instead of (never modifying) the operational file.
if (getenv('PLATFORM_SYNTHETIC_SANDBOX') !== 's3-imds-v1'
    || preg_match('/^nc-s3-imds-test-[a-f0-9]{16}$/D', (string)getenv('PLATFORM_SANDBOX_PROJECT')) !== 1
    || getenv('AWS_EC2_METADATA_SERVICE_ENDPOINT') !== 'https://imds.sandbox.test:1338'
    || getenv('AWS_EC2_METADATA_V1_DISABLED') !== 'true'
    || getenv('NEXTCLOUD_S3_BUCKET') !== 'fixture-data'
    || getenv('NEXTCLOUD_S3_REGION') !== 'us-east-1') {
    throw new RuntimeException('SANDBOX_CONFIG_REJECTED');
}
$CONFIG = [
    'objectstore' => [
        'class' => '\OC\Files\ObjectStore\S3',
        'arguments' => [
            'bucket' => 'fixture-data', 'region' => 'us-east-1',
            'hostname' => 's3.sandbox.test', 'port' => 5000,
            'use_ssl' => true, 'use_path_style' => false, 'verify_bucket_exists' => false,
        ],
    ],
    'memcache.local' => '\OC\Memcache\APCu',
    'memcache.distributed' => '\OC\Memcache\Redis',
    'memcache.locking' => '\OC\Memcache\Redis',
    'trusted_domains' => ['nextcloud'],
];
