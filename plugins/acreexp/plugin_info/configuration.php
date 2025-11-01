<?php
/*
 * Page de configuration globale du plugin ACRE SPC.
 */

require_once dirname(__FILE__) . '/../../../core/php/core.inc.php';
include_file('core', 'authentification', 'php');
if (!isConnect()) {
    include_file('desktop', '404', 'php');
    die();
}
?>
<form class="form-horizontal">
  <fieldset>
    <div class="form-group">
      <label class="col-lg-4 control-label">{{Intervalle de rafraîchissement du démon (secondes)}}
        <sup><i class="fas fa-question-circle tooltips" title="{{Durée entre deux interrogations successives des centrales. Valeur minimale recommandée : 10 secondes.}}"></i></sup>
      </label>
      <div class="col-lg-3">
        <input class="configKey form-control" data-l1key="poll_interval" placeholder="60" type="number" min="10" step="1" />
      </div>
    </div>
    <div class="form-group">
      <label class="col-lg-4 control-label">{{Binaire Python à utiliser}}
        <sup><i class="fas fa-question-circle tooltips" title="{{Chemin complet du binaire Python 3. Laissez vide pour utiliser la détection automatique.}}"></i></sup>
      </label>
      <div class="col-lg-5">
        <input class="configKey form-control" data-l1key="python_binary" placeholder="/usr/bin/python3" />
      </div>
    </div>
  </fieldset>
</form>
