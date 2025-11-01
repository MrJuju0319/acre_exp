<?php
if (!isConnect('admin')) {
    throw new Exception(__('401 - Accès non autorisé', __FILE__));
}
$plugin = plugin::byId('acreexp');
sendVarToJS('eqType', $plugin->getId());
$eqLogics = eqLogic::byType($plugin->getId());
?>
<div class="row row-overflow">
  <div class="col-xs-12 eqLogicThumbnailDisplay">
    <legend><i class="fas fa-cog"></i> {{Gestion}}</legend>
    <div class="eqLogicThumbnailContainer">
      <div class="cursor eqLogicAction logoPrimary" data-action="add">
        <i class="fas fa-plus-circle"></i>
        <br />
        <span>{{Ajouter}}</span>
      </div>
      <div class="cursor eqLogicAction logoSecondary" data-action="gotoPluginConf">
        <i class="fas fa-wrench"></i>
        <br />
        <span>{{Configuration}}</span>
      </div>
      <div class="cursor eqLogicAction" data-action="openLog" data-log="acreexp">
        <i class="fas fa-file-alt"></i>
        <br />
        <span>{{Logs}}</span>
      </div>
      <div class="cursor eqLogicAction" data-action="openLog" data-log="acreexp_daemon">
        <i class="fas fa-terminal"></i>
        <br />
        <span>{{Log démon}}</span>
      </div>
    </div>
    <legend><i class="fas fa-shield-alt"></i> {{Mes centrales ACRE/Siemens}}</legend>
    <?php if (count($eqLogics) === 0) { ?>
      <div class="text-center" style="font-size:1.2em;font-weight:bold;">
        {{Aucun équipement ACRE SPC trouvé. Cliquez sur "Ajouter" pour commencer.}}
      </div>
    <?php } else { ?>
      <div class="input-group" style="margin:5px;">
        <input class="form-control roundedLeft" placeholder="{{Rechercher}}" id="in_searchEqlogic" />
        <div class="input-group-btn">
          <a id="bt_resetSearch" class="btn" style="width:30px"><i class="fas fa-times"></i></a>
          <a class="btn roundedRight hidden" id="bt_pluginDisplayAsTable" data-coreSupport="1" data-state="0"><i class="fas fa-grip-lines"></i></a>
        </div>
      </div>
      <div class="eqLogicThumbnailContainer">
        <?php foreach ($eqLogics as $eqLogic) {
            $opacity = ($eqLogic->getIsEnable()) ? '' : 'disableCard';
        ?>
          <div class="eqLogicDisplayCard cursor <?php echo $opacity; ?>" data-eqLogic_id="<?php echo $eqLogic->getId(); ?>">
            <i class="fas fa-shield-alt"></i>
            <br />
            <span class="name"><?php echo $eqLogic->getHumanName(true, true); ?></span>
            <span class="hiddenAsCard displayTableRight hidden">
              <?php echo ($eqLogic->getIsVisible() == 1) ? '<i class="fas fa-eye" title="' . __('Equipement visible', __FILE__) . '"></i>' : '<i class="fas fa-eye-slash" title="' . __('Equipement non visible', __FILE__) . '"></i>'; ?>
            </span>
          </div>
        <?php } ?>
      </div>
    <?php } ?>
  </div>

  <div class="col-xs-12 eqLogic" style="display: none;">
    <div class="input-group pull-right" style="display:inline-flex;">
      <span class="input-group-btn">
        <a class="btn btn-sm btn-default eqLogicAction roundedLeft" data-action="configure"><i class="fas fa-cogs"></i><span class="hidden-xs"> {{Configuration avancée}}</span></a>
        <a class="btn btn-sm btn-default eqLogicAction" data-action="copy"><i class="fas fa-copy"></i><span class="hidden-xs"> {{Dupliquer}}</span></a>
        <a class="btn btn-sm btn-success eqLogicAction" data-action="save"><i class="fas fa-check-circle"></i> {{Sauvegarder}}</a>
        <a class="btn btn-sm btn-danger eqLogicAction roundedRight" data-action="remove"><i class="fas fa-minus-circle"></i> {{Supprimer}}</a>
      </span>
    </div>
    <ul class="nav nav-tabs" role="tablist">
      <li role="presentation"><a href="#" class="eqLogicAction" aria-controls="home" role="tab" data-toggle="tab" data-action="returnToThumbnailDisplay"><i class="fas fa-arrow-circle-left"></i></a></li>
      <li role="presentation" class="active"><a href="#eqlogictab" aria-controls="home" role="tab" data-toggle="tab"><i class="fas fa-tachometer-alt"></i> {{Equipement}}</a></li>
      <li role="presentation"><a href="#commandtab" aria-controls="profile" role="tab" data-toggle="tab"><i class="fas fa-list"></i> {{Commandes}}</a></li>
    </ul>
    <div class="tab-content">
      <div role="tabpanel" class="tab-pane active" id="eqlogictab">
        <form class="form-horizontal">
          <fieldset>
            <div class="col-lg-6">
              <legend><i class="fas fa-wrench"></i> {{Paramètres généraux}}</legend>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Nom de l'équipement}}</label>
                <div class="col-sm-6">
                  <input type="text" class="eqLogicAttr form-control" data-l1key="id" style="display:none;" />
                  <input type="text" class="eqLogicAttr form-control" data-l1key="name" placeholder="{{ACRE SPC}}" />
                </div>
              </div>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Objet parent}}</label>
                <div class="col-sm-6">
                  <select id="sel_object" class="eqLogicAttr form-control" data-l1key="object_id">
                    <option value="">{{Aucun}}</option>
                    <?php
                    $options = '';
                    foreach ((jeeObject::buildTree(null, false)) as $object) {
                        $options .= '<option value="' . $object->getId() . '">' . str_repeat('&nbsp;&nbsp;', $object->getConfiguration('parentNumber')) . $object->getName() . '</option>';
                    }
                    echo $options;
                    ?>
                  </select>
                </div>
              </div>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Catégorie}}</label>
                <div class="col-sm-8">
                  <?php
                  foreach (jeedom::getConfiguration('eqLogic:category') as $key => $value) {
                      echo '<label class="checkbox-inline">';
                      echo '<input type="checkbox" class="eqLogicAttr" data-l1key="category" data-l2key="' . $key . '">' . $value['name'];
                      echo '</label>';
                  }
                  ?>
                </div>
              </div>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Options}}</label>
                <div class="col-sm-6">
                  <label class="checkbox-inline"><input type="checkbox" class="eqLogicAttr" data-l1key="isEnable" checked />{{Activer}}</label>
                  <label class="checkbox-inline"><input type="checkbox" class="eqLogicAttr" data-l1key="isVisible" checked />{{Visible}}</label>
                </div>
              </div>

              <legend><i class="fas fa-network-wired"></i> {{Connexion à la centrale}}</legend>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Adresse IP / nom d'hôte}}</label>
                <div class="col-sm-6">
                  <input class="eqLogicAttr form-control" data-l1key="configuration" data-l2key="host" placeholder="192.168.0.10" />
                </div>
              </div>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Port}}</label>
                <div class="col-sm-3">
                  <input class="eqLogicAttr form-control" data-l1key="configuration" data-l2key="port" placeholder="443" />
                </div>
              </div>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Utiliser HTTPS}}</label>
                <div class="col-sm-6">
                  <input type="checkbox" class="eqLogicAttr" data-l1key="configuration" data-l2key="https" />
                </div>
              </div>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Utilisateur}}</label>
                <div class="col-sm-6">
                  <input class="eqLogicAttr form-control" data-l1key="configuration" data-l2key="user" />
                </div>
              </div>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Code / PIN}}</label>
                <div class="col-sm-6">
                  <input class="eqLogicAttr form-control inputPassword" data-l1key="configuration" data-l2key="code" type="password" autocomplete="new-password" />
                </div>
              </div>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Actions}}</label>
                <div class="col-sm-6">
                  <a class="btn btn-default" id="bt_acreexp_refresh"><i class="fas fa-sync"></i> {{Synchroniser maintenant}}</a>
                </div>
              </div>
            </div>

            <div class="col-lg-6">
              <legend><i class="fas fa-info-circle"></i> {{Informations}}</legend>
              <div class="form-group">
                <label class="col-sm-4 control-label">{{Description}}</label>
                <div class="col-sm-6">
                  <textarea class="form-control eqLogicAttr autogrow" data-l1key="comment"></textarea>
                </div>
              </div>
              <div class="alert alert-info">
                <i class="fas fa-lightbulb"></i> {{Après enregistrement, utilisez le bouton "Synchroniser maintenant" pour récupérer automatiquement les secteurs et zones depuis la centrale.}}
              </div>
              <div class="alert alert-warning" id="acreexp_daemon_warning" style="display:none;"></div>
            </div>
          </fieldset>
        </form>
      </div>

      <div role="tabpanel" class="tab-pane" id="commandtab">
        <a class="btn btn-default btn-sm pull-right cmdAction" data-action="add" style="margin-top:5px;"><i class="fas fa-plus-circle"></i> {{Ajouter une commande}}</a>
        <br /><br />
        <div class="table-responsive">
          <table id="table_cmd" class="table table-bordered table-condensed">
            <thead>
              <tr>
                <th class="hidden-xs" style="width:70px;">ID</th>
                <th style="min-width:200px;">{{Nom}}</th>
                <th>{{Type}}</th>
                <th style="min-width:180px;">{{Logical ID}}</th>
                <th>{{Etat}}</th>
                <th style="width:140px;">{{Actions}}</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<?php include_file('desktop', 'acreexp', 'js', 'acreexp'); ?>
<?php include_file('core', 'plugin.template', 'js'); ?>
