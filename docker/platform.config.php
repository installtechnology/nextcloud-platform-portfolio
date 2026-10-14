<?php
// Fresh installation only. SDK default provider obtains temporary IMDSv2 credentials.
$bucket = (string) getenv("NEXTCLOUD_S3_BUCKET");
$region = (string) getenv("NEXTCLOUD_S3_REGION");
if (!preg_match("/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/", $bucket)
    || !preg_match("/^[a-z]{2}(-[a-z]+)+-[0-9]+$/", $region)) {
    throw new RuntimeException("Configure bucket e região S3 válidos");
}
$CONFIG = [
    'objectstore' => [
        'class' => '\\OC\\Files\\ObjectStore\\S3',
        'arguments' => [
            'bucket' => $bucket,
            'region' => $region,
            'use_ssl' => true,
            'use_path_style' => false,
            // stable34 may create buckets when this check is enabled. Terraform owns them.
            'verify_bucket_exists' => false,
        ],
    ],
    'memcache.local' => '\\OC\\Memcache\\APCu',
    'memcache.distributed' => '\\OC\\Memcache\\Redis',
    'memcache.locking' => '\\OC\\Memcache\\Redis',
    'trusted_domains' => preg_split('/\s+/', trim((string) getenv('NEXTCLOUD_TRUSTED_DOMAINS')), -1, PREG_SPLIT_NO_EMPTY),
];
// Redis host/port/password vêm do redis.config.php oficial e das variáveis REDIS_*.
// Trusted proxies/HTTPS somente em fase posterior, com endereços reais aprovados.
