<?php
if (!isConnect('admin')) {
    throw new Exception(__('401 - Accès non autorisé', __FILE__));
}
$plugin = plugin::byId('acreexp');
$eqLogics = eqLogic::byType($plugin->getId());
include_file('core', 'plugin.template', 'php');
?>
<div class="row row-overflow">
  <div class="col-xs-12 eqLogicThumbnailDisplay">
    <div class="eqLogicThumbnailContainer">
      <div class="cursor eqLogicAction" data-action="add">
        <i class="fas fa-plus-circle"></i>
        <br>
        <span>{{Ajouter}}</span>
      </div>
      <div class="cursor eqLogicAction" data-action="gotoPluginConf">
        <i class="fas fa-wrench"></i>
        <br>
        <span>{{Configuration}}</span>
      </div>
    </div>
    <div class="eqLogicThumbnailContainer">
      <?php foreach ($eqLogics as $eqLogic) { ?>
        <div class="eqLogicDisplayCard cursor" data-eqLogic_id="<?php echo $eqLogic->getId(); ?>">
          <img src="plugins/acreexp/plugin_info/acreexp_icon.png" />
          <br>
          <span><?php echo $eqLogic->getHumanName(true, true); ?></span>
        </div>
      <?php } ?>
    </div>
  </div>

  <div class="col-xs-12 eqLogic" style="display: none;">
    <div class="col-xs-12 col-sm-6">
      <form class="form-horizontal">
        <fieldset>
          <legend><i class="fas fa-wrench"></i> {{Général}}</legend>
          <div class="form-group">
            <label class="col-sm-4 control-label">{{Nom de l'équipement}}</label>
            <div class="col-sm-6">
              <input class="eqLogicAttr form-control" data-l1key="name" placeholder="{{ACRE SPC}}" />
            </div>
          </div>
          <div class="form-group">
            <label class="col-sm-4 control-label">{{Objet parent}}</label>
            <div class="col-sm-6">
              <select class="eqLogicAttr form-control" data-l1key="object_id">
                <?php
                foreach (jeeObject::all() as $object) {
                    echo '<option value="' . $object->getId() . '">' . $object->getName() . '</option>';
                }
                ?>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label class="col-sm-4 control-label">{{Activer}}</label>
            <div class="col-sm-6">
              <input type="checkbox" class="eqLogicAttr" data-l1key="isEnable" />
            </div>
          </div>
          <div class="form-group">
            <label class="col-sm-4 control-label">{{Visible}}</label>
            <div class="col-sm-6">
              <input type="checkbox" class="eqLogicAttr" data-l1key="isVisible" />
            </div>
          </div>
        </fieldset>
      </form>
      <form class="form-horizontal">
        <fieldset>
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
            <label class="col-sm-4 control-label">{{Code/PIN}}</label>
            <div class="col-sm-6">
              <input class="eqLogicAttr form-control" data-l1key="configuration" data-l2key="code" type="password" />
            </div>
          </div>
        </fieldset>
      </form>
    </div>

    <div class="col-xs-12 col-sm-6">
      <legend><i class="fas fa-list"></i> {{Commandes}}</legend>
      <table id="table_cmd" class="table table-bordered table-condensed">
        <thead>
          <tr>
            <th>{{Nom}}</th>
            <th>{{Type}}</th>
            <th>{{Logical ID}}</th>
            <th>{{Paramètres}}</th>
            <th>{{Actions}}</th>
          </tr>
        </thead>
        <tbody>
        </tbody>
      </table>
    </div>
  </div>
</div>

<?php include_file('desktop', 'acreexp', 'js', 'acreexp'); ?>
<?php include_file('core', 'plugin.template', 'js'); ?>
