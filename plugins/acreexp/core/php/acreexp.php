<?php
/* Script de démon pour le plugin acreexp */

require_once __DIR__ . '/../../../../core/php/core.inc.php';

$options = getopt('', ['pid:', 'stop:', 'debug']);
$pidFile = isset($options['pid']) ? $options['pid'] : '';
$stopFile = isset($options['stop']) ? $options['stop'] : '';
$debug = array_key_exists('debug', $options);

if ($pidFile !== '') {
    @file_put_contents($pidFile, getmypid());
}

$loopDelay = (int)config::byKey('poll_interval', 'acreexp', 60);
if ($loopDelay < 10) {
    $loopDelay = 10;
}

log::add('acreexp', 'info', sprintf(__('Démon démarré (cycle %ss)', __FILE__), $loopDelay));

while (true) {
    if ($stopFile !== '' && file_exists($stopFile)) {
        log::add('acreexp', 'info', __('Fichier stop détecté, arrêt du démon', __FILE__));
        @unlink($stopFile);
        break;
    }

    foreach (eqLogic::byType('acreexp', true) as $eqLogic) {
        if (!($eqLogic instanceof acreexp)) {
            continue;
        }
        if ($eqLogic->getIsEnable() != 1) {
            continue;
        }
        try {
            $eqLogic->refreshFromController();
        } catch (Exception $e) {
            log::add('acreexp', 'error', sprintf(__('Erreur lors du rafraîchissement de %s : %s', __FILE__), $eqLogic->getHumanName(), $e->getMessage()));
            if ($debug) {
                log::add('acreexp', 'debug', $e->getTraceAsString());
            }
        }
    }

    for ($i = 0; $i < $loopDelay; $i++) {
        if ($stopFile !== '' && file_exists($stopFile)) {
            break 2;
        }
        sleep(1);
    }
}

log::add('acreexp', 'info', __('Démon arrêté', __FILE__));
