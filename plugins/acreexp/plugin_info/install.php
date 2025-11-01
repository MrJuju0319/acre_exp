<?php
/*
 * Script d'installation et de mise à jour du plugin ACRE SPC.
 */

require_once __DIR__ . '/../core/php/acreexp.inc.php';

// Fonction exécutée automatiquement après l'installation du plugin
function acreexp_install() {
    acreexp_apply_default_configuration();
}

// Fonction exécutée automatiquement après la mise à jour du plugin
function acreexp_update() {
    acreexp_apply_default_configuration();
}

// Fonction exécutée automatiquement après la suppression du plugin
function acreexp_remove() {
    // Suppression des fichiers runtime (PID, cache de session, etc.).
    $runtimeDir = jeedom::getTmpFolder('acreexp');
    if (file_exists($runtimeDir)) {
        try {
            $files = new RecursiveIteratorIterator(
                new RecursiveDirectoryIterator($runtimeDir, RecursiveDirectoryIterator::SKIP_DOTS),
                RecursiveIteratorIterator::CHILD_FIRST
            );
            foreach ($files as $fileInfo) {
                if ($fileInfo->isDir()) {
                    @rmdir($fileInfo->getRealPath());
                } else {
                    @unlink($fileInfo->getRealPath());
                }
            }
            @rmdir($runtimeDir);
        } catch (Exception $e) {
            log::add('acreexp', 'warning', sprintf(__('Impossible de nettoyer le dossier temporaire : %s', __FILE__), $e->getMessage()));
        }
    }
}
