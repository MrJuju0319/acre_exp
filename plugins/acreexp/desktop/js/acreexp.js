/* global jeedom, eqLogic, init */

$('#table_cmd').delegate('.cmd .remove', 'click', function () {
  $(this).closest('tr').remove();
});

function addCmdToTable(_cmd) {
  if (!isset(_cmd)) {
    _cmd = {configuration: {}};
  }
  if (!isset(_cmd.configuration)) {
    _cmd.configuration = {};
  }
  var tr = '<tr class="cmd" data-cmd_id="' + init(_cmd.id) + '">';
  tr += '<td>';
  tr += '<input class="cmdAttr form-control input-sm" data-l1key="name" value="' + init(_cmd.name) + '" />';
  tr += '</td>';
  tr += '<td>' + init(_cmd.type) + '/' + init(_cmd.subType) + '</td>';
  tr += '<td>' + init(_cmd.logicalId) + '</td>';
  tr += '<td></td>';
  tr += '<td><span class="pull-right">';
  if (init(_cmd.id) !== '') {
    tr += '<a class="btn btn-default btn-xs cmdAction" data-action="configure"><i class="fas fa-cogs"></i></a> ';
    tr += '<a class="btn btn-default btn-xs cmdAction" data-action="test"><i class="fas fa-rss"></i></a> ';
  }
  tr += '<a class="btn btn-danger btn-xs remove"><i class="fas fa-trash"></i></a>';
  tr += '</span></td>';
  tr += '</tr>';
  $('#table_cmd tbody').append(tr);
}

$('#table_cmd tbody').on('click', '.cmdAction[data-action=test]', function () {
  var cmdId = $(this).closest('tr').data('cmd_id');
  if (cmdId) {
    jeedom.cmd.execute({id: cmdId});
  }
});
