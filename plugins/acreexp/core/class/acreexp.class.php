<?php
/* This file is part of Jeedom and is licensed under the AGPL.
 *
 * Plugin ACRE SPC : intégration de la centrale ACRE/Siemens SPC dans Jeedom.
 */

require_once __DIR__ . '/../../../../core/php/core.inc.php';

class acreexp extends eqLogic {
    /**
     * Retourne des informations sur le démon du plugin.
     *
     * @return array
     */
    public static function deamon_info() {
        $info = [
            'state' => 'nok',
            'launchable' => 'ok',
            'launchable_message' => '',
        ];

        if (self::isDaemonRunning()) {
            $info['state'] = 'ok';
        }

        if (count(eqLogic::byType('acreexp', true)) === 0) {
            $info['launchable'] = 'nok';
            $info['launchable_message'] = __('Aucun équipement configuré', __FILE__);
        }

        return $info;
    }

    /**
     * Démarre le démon PHP qui interroge périodiquement les centrales configurées.
     *
     * @param bool $_debug
     * @throws Exception
     */
    public static function deamon_start($_debug = false) {
        $info = self::deamon_info();
        if ($info['launchable'] === 'nok') {
            throw new Exception(__('Le démon ne peut pas être lancé : ', __FILE__) . $info['launchable_message']);
        }

        if (self::isDaemonRunning()) {
            self::deamon_stop();
            sleep(1);
        }

        $runtimeDir = self::getRuntimeDirectory();
        if (!file_exists($runtimeDir)) {
            mkdir($runtimeDir, 0770, true);
        }

        $pidFile = self::getPidFile();
        $stopFile = self::getStopFile();
        if (file_exists($stopFile)) {
            unlink($stopFile);
        }

        $cmd = 'php ' . escapeshellarg(__DIR__ . '/../php/acreexp.php')
            . ' --pid ' . escapeshellarg($pidFile)
            . ' --stop ' . escapeshellarg($stopFile);

        if ($_debug) {
            $cmd .= ' --debug';
        }

        $cmd .= ' >> ' . log::getPathToLog('acreexp_daemon') . ' 2>&1 & echo $!';
        $pid = exec($cmd);

        if (!is_numeric($pid)) {
            log::add('acreexp', 'error', __('Impossible de démarrer le démon (PID invalide)', __FILE__));
            throw new Exception(__('Impossible de démarrer le démon', __FILE__));
        }

        file_put_contents($pidFile, trim($pid));
        log::add('acreexp', 'info', sprintf(__('Démon lancé (PID %s)', __FILE__), $pid));
    }

    /**
     * Arrête le démon du plugin.
     */
    public static function deamon_stop() {
        $pid = self::getDaemonPid();
        $pidFile = self::getPidFile();
        $stopFile = self::getStopFile();

        if (!file_exists($stopFile)) {
            @file_put_contents($stopFile, (string)time());
        }

        self::sendSignal($pid, 'SIGTERM');

        for ($i = 0; $i < 10; $i++) {
            if (!self::isDaemonRunning()) {
                break;
            }
            sleep(1);
            self::sendSignal($pid, 'SIGTERM');
        }

        if (self::isDaemonRunning()) {
            log::add('acreexp', 'warning', __('Arrêt forcé du démon', __FILE__));
            self::sendSignal($pid, 'SIGKILL');
            sleep(1);
        }

        if (file_exists($pidFile)) {
            @unlink($pidFile);
        }
        if (file_exists($stopFile)) {
            @unlink($stopFile);
        }
    }

    /**
     * Validation avant sauvegarde de l'équipement.
     *
     * @throws Exception
     */
    public function preSave() {
        $host = trim((string)$this->getConfiguration('host'));
        $user = trim((string)$this->getConfiguration('user'));
        $code = trim((string)$this->getConfiguration('code'));

        if ($host === '') {
            throw new Exception(__('L\'adresse IP ou le nom d\'hôte est obligatoire', __FILE__));
        }
        if ($user === '') {
            throw new Exception(__('L\'identifiant est obligatoire', __FILE__));
        }
        if ($code === '') {
            throw new Exception(__('Le code utilisateur est obligatoire', __FILE__));
        }
    }

    /**
     * Synchronise les commandes après la sauvegarde.
     */
    public function postSave() {
        try {
            $this->synchronize(true);
        } catch (Exception $e) {
            log::add('acreexp', 'error', sprintf(__('Synchronisation impossible : %s', __FILE__), $e->getMessage()));
        }
    }

    /**
     * Interroge la centrale et met à jour les commandes existantes.
     */
    public function refreshFromController() {
        $this->synchronize(false);
    }

    /**
     * Synchronise l'équipement avec la centrale.
     *
     * @param bool $createMissing Crée les commandes manquantes si vrai.
     */
    public function synchronize($createMissing = false) {
        $snapshot = $this->fetchControllerSnapshot();
        $this->applySnapshot($snapshot, (bool)$createMissing);
    }

    /**
     * Effectue un appel au script Python pour récupérer l'état courant.
     *
     * @return array
     * @throws Exception
     */
    private function fetchControllerSnapshot() {
        $host = trim((string)$this->getConfiguration('host'));
        $user = trim((string)$this->getConfiguration('user'));
        $code = trim((string)$this->getConfiguration('code'));
        $port = trim((string)$this->getConfiguration('port'));
        $https = (bool)$this->getConfiguration('https', 0);

        $protocol = $https ? 'https' : 'http';

        if ($host === '') {
            throw new Exception(__('Hôte non défini', __FILE__));
        }

        $baseUrl = $host;
        if (strpos($baseUrl, 'http://') !== 0 && strpos($baseUrl, 'https://') !== 0) {
            $baseUrl = $protocol . '://' . $baseUrl;
        }
        if ($port !== '') {
            if (preg_match('#^https?://#i', $baseUrl)) {
                $baseUrl = preg_replace('#^(https?://[^/:]+)(:\d+)?#i', '$1:' . $port, $baseUrl);
            } else {
                $baseUrl .= ':' . $port;
            }
        }

        $runtimeDir = self::getRuntimeDirectory();
        if (!file_exists($runtimeDir)) {
            mkdir($runtimeDir, 0770, true);
        }

        $config = [
            'spc' => [
                'host' => $baseUrl,
                'user' => $user,
                'pin' => $code,
                'session_cache_dir' => $runtimeDir . '/session_' . $this->getId(),
            ],
        ];

        $tmpFile = tempnam($runtimeDir, 'cfg_');
        if ($tmpFile === false) {
            throw new Exception(__('Impossible de créer un fichier de configuration temporaire', __FILE__));
        }

        file_put_contents($tmpFile, self::arrayToYaml($config));

        $python = self::findPythonBinary();
        $script = realpath(__DIR__ . '/../../../acre_exp_status.py');
        if ($script === false || !file_exists($script)) {
            throw new Exception(__('Script acre_exp_status.py introuvable', __FILE__));
        }

        $cmd = escapeshellcmd($python) . ' ' . escapeshellarg($script) . ' -c ' . escapeshellarg($tmpFile);
        $descriptorSpec = [
            0 => ['pipe', 'r'],
            1 => ['pipe', 'w'],
            2 => ['pipe', 'w'],
        ];

        $process = proc_open($cmd, $descriptorSpec, $pipes);
        if (!is_resource($process)) {
            unlink($tmpFile);
            throw new Exception(__('Impossible d\'exécuter le script Python', __FILE__));
        }

        fclose($pipes[0]);
        $output = stream_get_contents($pipes[1]);
        $errors = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        $exitCode = proc_close($process);
        unlink($tmpFile);

        if ($exitCode !== 0) {
            throw new Exception(sprintf(__('Le script Python a retourné une erreur (%s)', __FILE__), trim($errors)));
        }

        $data = json_decode($output, true);
        if (!is_array($data)) {
            throw new Exception(__('Réponse JSON invalide depuis la centrale', __FILE__));
        }
        if (isset($data['error'])) {
            throw new Exception(sprintf(__('La centrale a retourné une erreur : %s', __FILE__), $data['error']));
        }

        return $data;
    }

    /**
     * Applique un snapshot récupéré depuis la centrale.
     *
     * @param array $snapshot
     * @param bool $createMissing Crée les commandes absentes si vrai.
     */
    private function applySnapshot(array $snapshot, $createMissing) {
        $zones = isset($snapshot['zones']) && is_array($snapshot['zones']) ? $snapshot['zones'] : [];
        $areas = isset($snapshot['areas']) && is_array($snapshot['areas']) ? $snapshot['areas'] : [];

        $validIds = [];

        foreach ($zones as $zone) {
            $zoneId = isset($zone['id']) ? (string)$zone['id'] : '';
            $zoneName = isset($zone['zone']) ? (string)$zone['zone'] : $zoneId;
            if ($zoneId === '') {
                $zoneId = strtolower(str_replace(' ', '_', $zoneName));
            }
            $validIds[] = 'zone::' . $zoneId . '::state';
            $validIds[] = 'zone::' . $zoneId . '::label';

            if ($createMissing) {
                $this->ensureInfoCmd('zone::' . $zoneId . '::state', sprintf(__('Zone %s - état', __FILE__), $zoneName), 'numeric');
                $this->ensureInfoCmd('zone::' . $zoneId . '::label', sprintf(__('Zone %s - texte', __FILE__), $zoneName), 'string');
            }

            $this->updateCmdValue('zone::' . $zoneId . '::state', isset($zone['etat']) ? $zone['etat'] : null);
            $this->updateCmdValue('zone::' . $zoneId . '::label', isset($zone['etat_txt']) ? $zone['etat_txt'] : '');
        }

        foreach ($areas as $area) {
            $areaId = isset($area['sid']) ? (string)$area['sid'] : '';
            $areaName = isset($area['secteur']) ? (string)$area['secteur'] : $areaId;
            if ($areaId === '') {
                $areaId = strtolower(str_replace(' ', '_', $areaName));
            }
            $validIds[] = 'area::' . $areaId . '::state';
            $validIds[] = 'area::' . $areaId . '::label';

            if ($createMissing) {
                $this->ensureInfoCmd('area::' . $areaId . '::state', sprintf(__('Secteur %s - état', __FILE__), $areaName), 'numeric');
                $this->ensureInfoCmd('area::' . $areaId . '::label', sprintf(__('Secteur %s - texte', __FILE__), $areaName), 'string');
            }

            $this->updateCmdValue('area::' . $areaId . '::state', isset($area['etat']) ? $area['etat'] : null);
            $this->updateCmdValue('area::' . $areaId . '::label', isset($area['etat_txt']) ? $area['etat_txt'] : '');
        }

        if ($createMissing) {
            $this->removeObsoleteCommands($validIds);
        }
    }

    /**
     * Crée une commande info si elle n'existe pas.
     *
     * @param string $logicalId
     * @param string $name
     * @param string $subType
     */
    private function ensureInfoCmd($logicalId, $name, $subType) {
        $cmd = $this->getCmd(null, $logicalId);
        if (!is_object($cmd)) {
            $cmd = new acreexpCmd();
            $cmd->setLogicalId($logicalId);
            $cmd->setName($name);
            $cmd->setType('info');
            $cmd->setSubType($subType);
            $cmd->setEqLogic_id($this->getId());
            $cmd->setIsHistorized(0);
            $cmd->save();
        }
    }

    /**
     * Met à jour la valeur d'une commande si elle existe.
     *
     * @param string $logicalId
     * @param mixed $value
     */
    private function updateCmdValue($logicalId, $value) {
        $cmd = $this->getCmd(null, $logicalId);
        if (is_object($cmd)) {
            $cmd->event($value);
        }
    }

    /**
     * Supprime les commandes obsolètes qui ne figurent plus dans le snapshot.
     *
     * @param array $validLogicalIds
     */
    private function removeObsoleteCommands(array $validLogicalIds) {
        $valid = array_unique($validLogicalIds);
        foreach ($this->getCmd() as $cmd) {
            $logicalId = (string)$cmd->getLogicalId();
            if (strpos($logicalId, 'zone::') !== 0 && strpos($logicalId, 'area::') !== 0) {
                continue;
            }
            if (!in_array($logicalId, $valid)) {
                $cmd->remove();
            }
        }
    }

    /**
     * Convertit un tableau PHP en YAML minimaliste.
     *
     * @param array $data
     * @param int $indent
     * @return string
     */
    private static function arrayToYaml(array $data, $indent = 0) {
        $yaml = '';
        foreach ($data as $key => $value) {
            $spaces = str_repeat(' ', $indent);
            if (is_array($value)) {
                $yaml .= $spaces . $key . ":\n";
                $yaml .= self::arrayToYaml($value, $indent + 2);
            } else {
                $yaml .= $spaces . $key . ': ' . self::yamlScalar($value) . "\n";
            }
        }
        return $yaml;
    }

    /**
     * Formate un scalaire pour YAML.
     *
     * @param mixed $value
     * @return string
     */
    private static function yamlScalar($value) {
        if (is_bool($value)) {
            return $value ? 'true' : 'false';
        }
        if (is_numeric($value)) {
            return (string)$value;
        }
        $string = (string)$value;
        return '\'' . str_replace('\'', '\'\'', $string) . '\'';
    }

    /**
     * Retourne le chemin du binaire Python.
     *
     * @return string
     */
    private static function findPythonBinary() {
        $configured = trim((string)config::byKey('python_binary', 'acreexp', ''));
        if ($configured !== '') {
            return $configured;
        }
        $candidates = [
            '/opt/spc-venv/bin/python3',
            '/usr/bin/python3',
            '/usr/local/bin/python3',
        ];
        foreach ($candidates as $candidate) {
            if (file_exists($candidate)) {
                return $candidate;
            }
        }
        return '/usr/bin/env python3';
    }

    /**
     * Retourne le dossier runtime utilisé par le plugin.
     *
     * @return string
     */
    private static function getRuntimeDirectory() {
        return jeedom::getTmpFolder('acreexp');
    }

    /**
     * Retourne le chemin du fichier PID.
     *
     * @return string
     */
    private static function getPidFile() {
        return self::getRuntimeDirectory() . '/acreexp.pid';
    }

    /**
     * Retourne le chemin du fichier stop.
     *
     * @return string
     */
    private static function getStopFile() {
        return self::getRuntimeDirectory() . '/acreexp.stop';
    }

    /**
     * Retourne le PID courant du démon.
     *
     * @return int
     */
    private static function getDaemonPid() {
        $pidFile = self::getPidFile();
        if (!file_exists($pidFile)) {
            return 0;
        }
        $content = trim((string)file_get_contents($pidFile));
        return (int)$content;
    }

    /**
     * Indique si le démon est actif.
     *
     * @return bool
     */
    private static function isDaemonRunning() {
        $pid = self::getDaemonPid();
        if ($pid <= 0) {
            return false;
        }
        return @posix_kill($pid, 0);
    }

    /**
     * Envoie un signal POSIX si possible.
     *
     * @param int $pid
     * @param string $signalName
     */
    private static function sendSignal($pid, $signalName) {
        if ($pid <= 0) {
            return;
        }
        $signal = defined($signalName) ? constant($signalName) : null;
        if ($signal === null && $signalName === 'SIGTERM') {
            $signal = 15;
        } elseif ($signal === null && $signalName === 'SIGKILL') {
            $signal = 9;
        }
        if ($signal === null) {
            return;
        }
        @posix_kill($pid, $signal);
    }
}

class acreexpCmd extends cmd {
    /**
     * Exécution d'une commande.
     * Les commandes info n'ont pas d'action.
     *
     * @param array $_options
     * @return mixed
     */
    public function execute($_options = array()) {
        throw new Exception(__('Cette commande est uniquement informative', __FILE__));
    }
}
