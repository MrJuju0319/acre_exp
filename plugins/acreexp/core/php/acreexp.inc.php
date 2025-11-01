<?php
/*
 * Fonctions utilitaires partagées par le plugin ACRE SPC.
 */

require_once __DIR__ . '/../../../../core/php/core.inc.php';

if (!function_exists('acreexp_apply_default_configuration')) {
    /**
     * Enregistre les clés de configuration globale attendues par le plugin si elles sont absentes.
     */
    function acreexp_apply_default_configuration() {
        $defaults = [
            'poll_interval' => 60,
            'python_binary' => '',
        ];

        foreach ($defaults as $key => $value) {
            $sentinel = '__acreexp_missing__';
            $current = config::byKey($key, 'acreexp', $sentinel);
            if ($current === $sentinel) {
                config::save($key, $value, 'acreexp');
            }
        }
    }
}
