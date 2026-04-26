from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return HTMLResponse(SETTINGS_HTML)


SETTINGS_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MAX Bot — настройки</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background: #f8f9fa; padding: 24px; font-size: 14px; }
    .card { border: none; box-shadow: 0 1px 4px rgba(0,0,0,.1); }
    .webhook-box { background: #e8f0fe; border-radius: 6px; padding: 10px 14px; word-break: break-all; font-family: monospace; font-size: 13px; }
    .badge-active { background: #d4edda; color: #155724; }
    .badge-inactive { background: #f8d7da; color: #721c24; }
  </style>
</head>
<body>

<div id="app">
  <div id="loading" class="text-center py-5">
    <div class="spinner-border text-primary" role="status"></div>
    <p class="mt-2 text-muted">Загрузка...</p>
  </div>

  <div id="main" style="display:none">
    <h5 class="mb-4">MAX Bot — подключённые каналы</h5>

    <!-- Channels list -->
    <div class="card mb-4">
      <div class="card-body p-0">
        <table class="table table-hover mb-0" id="channelsTable">
          <thead class="table-light">
            <tr>
              <th>Название</th>
              <th>Sender ID</th>
              <th>Подключён</th>
              <th>Статус</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="channelsList">
            <tr id="emptyRow"><td colspan="5" class="text-center text-muted py-3">Каналов нет</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Add channel form -->
    <div class="card">
      <div class="card-header bg-white fw-semibold">Подключить новый канал</div>
      <div class="card-body">
        <div id="formError" class="alert alert-danger d-none"></div>
        <div class="mb-3">
          <label class="form-label">Название канала</label>
          <input type="text" class="form-control" id="channelName" placeholder="Например: MAX Bot продажи">
        </div>
        <div class="mb-3">
          <label class="form-label">API-ключ (X-API-KEY)</label>
          <input type="text" class="form-control" id="apiKey" placeholder="Ваш API-ключ от edna">
        </div>
        <div class="mb-3">
          <label class="form-label">Sender ID</label>
          <input type="text" class="form-control" id="sender" placeholder="Идентификатор канала MAX Bot">
        </div>
        <button class="btn btn-primary" id="saveBtn" onclick="saveChannel()">
          <span id="saveBtnText">Подключить</span>
          <span id="saveBtnSpinner" class="spinner-border spinner-border-sm d-none ms-1"></span>
        </button>
      </div>
    </div>

    <!-- Webhook URL (shown after save) -->
    <div id="webhookBlock" class="card mt-4 d-none">
      <div class="card-body">
        <h6 class="mb-2">✓ Канал подключён</h6>
        <p class="text-muted small mb-2">Скопируйте этот URL и укажите его как webhook на сервере MAX Bot:</p>
        <div class="webhook-box" id="webhookUrl"></div>
        <button class="btn btn-outline-secondary btn-sm mt-2" onclick="copyWebhook()">Скопировать</button>
      </div>
    </div>
  </div>
</div>

<script src="//api.bitrix24.com/api/v1/"></script>
<script>
var memberId = null;

BX24.init(function() {
  BX24.callMethod('profile', {}, function(res) {
    if (res.error()) {
      showError('Ошибка авторизации Битрикс24');
      return;
    }
    // Get member_id from auth
    var auth = BX24.getAuth();
    memberId = auth.member_id;
    loadChannels();
  });
});

function loadChannels() {
  fetch('/api/channels?member_id=' + encodeURIComponent(memberId))
    .then(function(r) { return r.json(); })
    .then(function(channels) {
      renderChannels(channels);
      document.getElementById('loading').style.display = 'none';
      document.getElementById('main').style.display = '';
    })
    .catch(function(e) {
      document.getElementById('loading').innerHTML = '<p class="text-danger">Ошибка загрузки каналов</p>';
    });
}

function renderChannels(channels) {
  var tbody = document.getElementById('channelsList');
  var emptyRow = document.getElementById('emptyRow');
  var rows = [];

  channels.forEach(function(ch) {
    var date = new Date(ch.connected_at).toLocaleDateString('ru-RU');
    var badge = ch.is_active
      ? '<span class="badge badge-active rounded-pill">Активен</span>'
      : '<span class="badge badge-inactive rounded-pill">Отключён</span>';
    var btn = ch.is_active
      ? '<button class="btn btn-outline-danger btn-sm" onclick="disconnect(' + ch.id + ')">Отключить</button>'
      : '';
    rows.push('<tr>' +
      '<td>' + esc(ch.name) + '</td>' +
      '<td class="font-monospace">' + esc(ch.sender) + '</td>' +
      '<td>' + date + '</td>' +
      '<td>' + badge + '</td>' +
      '<td>' + btn + '</td>' +
      '</tr>');
  });

  if (rows.length) {
    emptyRow.style.display = 'none';
    tbody.innerHTML = rows.join('') + emptyRow.outerHTML;
  }
}

function saveChannel() {
  var name = document.getElementById('channelName').value.trim();
  var apiKey = document.getElementById('apiKey').value.trim();
  var sender = document.getElementById('sender').value.trim();

  if (!name || !apiKey || !sender) {
    showFormError('Заполните все поля');
    return;
  }

  setLoading(true);
  document.getElementById('formError').classList.add('d-none');

  fetch('/api/channels', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({member_id: memberId, name: name, api_key: apiKey, sender: sender})
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail || 'Ошибка сохранения'); });
    return r.json();
  })
  .then(function(data) {
    setLoading(false);
    document.getElementById('channelName').value = '';
    document.getElementById('apiKey').value = '';
    document.getElementById('sender').value = '';

    document.getElementById('webhookUrl').textContent = data.webhook_url;
    document.getElementById('webhookBlock').classList.remove('d-none');
    loadChannels();
  })
  .catch(function(e) {
    setLoading(false);
    showFormError(e.message);
  });
}

function disconnect(channelId) {
  if (!confirm('Отключить канал?')) return;
  fetch('/api/channels/' + channelId + '/disconnect?member_id=' + encodeURIComponent(memberId), {
    method: 'POST'
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail || 'Ошибка'); });
    return r.json();
  })
  .then(function() { loadChannels(); })
  .catch(function(e) { alert('Ошибка: ' + e.message); });
}

function copyWebhook() {
  var url = document.getElementById('webhookUrl').textContent;
  navigator.clipboard.writeText(url).then(function() {
    alert('URL скопирован');
  });
}

function setLoading(on) {
  document.getElementById('saveBtn').disabled = on;
  document.getElementById('saveBtnSpinner').classList.toggle('d-none', !on);
}

function showFormError(msg) {
  var el = document.getElementById('formError');
  el.textContent = msg;
  el.classList.remove('d-none');
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>
"""
