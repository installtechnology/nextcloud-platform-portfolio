<?php
function platformNextcloudReady(?array $status): bool {
    return $status !== null && ($status['installed'] ?? false) === true
        && ($status['maintenance'] ?? true) === false && ($status['needsDbUpgrade'] ?? true) === false;
}
if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') !== __FILE__) { return; }
$domains = preg_split('/\s+/', trim((string) getenv('NEXTCLOUD_TRUSTED_DOMAINS')));
$host = $domains[0] ?? '';
if ($host === '' || strpbrk($host, "\r\n") !== false) {
    exit(1);
}
$context = stream_context_create(['http' => [
    'timeout' => 5,
    'header' => "Host: $host\r\n",
]]);
$status = json_decode((string) @file_get_contents('http://127.0.0.1/status.php', false, $context), true);
exit(platformNextcloudReady(is_array($status) ? $status : null) ? 0 : 1);
