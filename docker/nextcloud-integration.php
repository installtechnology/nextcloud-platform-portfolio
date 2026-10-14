<?php
// Probe de integração executado como www-data; não exibe credenciais.
try {
    require '/var/www/html/lib/base.php';
    $config = \OCP\Server::get(\OCP\IConfig::class);
    if ($config->getSystemValue('dbtype') !== 'pgsql'
        || $config->getSystemValue('dbname') !== getenv('POSTGRES_DB')
        || $config->getSystemValue('dbuser') !== getenv('POSTGRES_USER')
        || $config->getSystemValue('memcache.local') !== '\\OC\\Memcache\\APCu'
        || $config->getSystemValue('memcache.locking') !== '\\OC\\Memcache\\Redis') {
        throw new RuntimeException('Configuração inválida');
    }
    $database = \OCP\Server::get(\OCP\IDBConnection::class);
    if ((int) $database->executeQuery('SELECT 1')->fetchOne() !== 1) {
        throw new RuntimeException('Banco indisponível');
    }
    $settings = $config->getSystemValue('redis');
    $redis = new \Redis();
    if (!in_array($settings['host'], ['redis', 'tcp://redis'], true) || (int) $settings['port'] !== 6379) {
        throw new RuntimeException('Endpoint Redis inválido');
    }
    $host = preg_replace('#^tcp://#', '', $settings['host']);
    if (!$redis->connect($host, (int) $settings['port'], 5)
        || !$redis->auth($settings['password']) || $redis->ping() === false) {
        throw new RuntimeException('Redis indisponível');
    }
    $storage = $config->getSystemValue('objectstore');
    if (($storage['class'] ?? '') !== '\\OC\\Files\\ObjectStore\\S3'
        || ($storage['arguments']['bucket'] ?? '') !== getenv('NEXTCLOUD_S3_BUCKET')
        || ($storage['arguments']['region'] ?? '') !== getenv('NEXTCLOUD_S3_REGION')
        || isset($storage['arguments']['key']) || isset($storage['arguments']['secret'])) {
        throw new RuntimeException('Storage inválido');
    }
    $store = new \OC\Files\ObjectStore\S3($storage['arguments']);
    $store->getConnection()->headBucket(['Bucket' => $storage['arguments']['bucket']]);
    echo "PostgreSQL/Redis/cache/S3 autenticado: OK\n";
} catch (\Throwable $error) {
    fwrite(STDERR, "Integração indisponível\n");
    exit(1);
}
