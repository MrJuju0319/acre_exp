<?php
/*
 * Points d'entrée AJAX pour le plugin ACRE SPC.
 */

try {
    require_once dirname(__FILE__) . '/../../../../core/php/core.inc.php';
    include_file('core', 'authentification', 'php');

    if (!isConnect('admin')) {
        throw new Exception(__('401 - Accès non autorisé', __FILE__));
    }

    ajax::init();

    $action = init('action');
    switch ($action) {
        case 'synchronize':
            $eqLogicId = (int)init('id');
            if ($eqLogicId <= 0) {
                throw new Exception(__('Identifiant d\'équipement invalide', __FILE__));
            }
            /** @var acreexp $eqLogic */
            $eqLogic = eqLogic::byId($eqLogicId);
            if (!is_object($eqLogic) || !$eqLogic instanceof acreexp) {
                throw new Exception(__('Equipement introuvable', __FILE__));
            }
            $createMissing = (bool)init('createMissing', 1);
            $eqLogic->synchronize($createMissing);
            ajax::success(__('Synchronisation effectuée', __FILE__));
            break;

        case 'daemonInfo':
            ajax::success(acreexp::deamon_info());
            break;

        default:
            throw new Exception(__('Aucune méthode correspondante à', __FILE__) . ' : ' . $action);
    }
} catch (Exception $e) {
    ajax::error(displayException($e), $e->getCode());
}
