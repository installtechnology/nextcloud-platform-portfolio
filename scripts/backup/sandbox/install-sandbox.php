<?php
// Test adapter only. Keep the original installation algorithm and all its guards.
require '/fixture/sandbox-preflight.php';
require '/opt/platform/install-original.php';
$adapter = productionInstallationAdapter();
$adapter['bucket_empty'] = static function (): bool {
    $client = new \Aws\S3\S3Client([
        'version' => 'latest', 'region' => 'us-east-1',
        'endpoint' => 'https://s3.sandbox.test:5000',
        'use_path_style_endpoint' => false,
        'http' => ['verify' => '/fixture/ca.pem', 'connect_timeout' => 5, 'timeout' => 10],
    ]);
    $result = $client->listObjectsV2(['Bucket' => 'fixture-data', 'MaxKeys' => 1]);
    return isset($result['KeyCount']) && (int) $result['KeyCount'] === 0 && empty($result['Contents']);
};
exit(runPlatformInstallation($adapter, '/var/www/html/config'));
