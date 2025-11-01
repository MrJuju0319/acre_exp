<?php
/*
 * Gestion de l'installation et de la mise à jour du plugin ACRE SPC.
 */

require_once __DIR__ . '/../../../../core/php/core.inc.php';

/**
 * Enregistre les clés de configuration globales attendues par le front si elles sont absentes.
 */
function acreexp_apply_default_configuration() {
    $defaults = [
        'host' => '',
        'port' => '',
        'https' => 0,
        'user' => '',
        'code' => '',
        'poll_interval' => 60,
    ];

    foreach ($defaults as $key => $value) {
        $sentinel = '__acreexp_missing__';
        $current = config::byKey($key, 'acreexp', $sentinel);
        if ($current === $sentinel) {
            config::save($key, $value, 'acreexp');
        }
    }
}

function acreexp_install() {
    acreexp_apply_default_configuration();
}

function acreexp_update() {
    acreexp_apply_default_configuration();
}
