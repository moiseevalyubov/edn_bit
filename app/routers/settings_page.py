from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.api_route("/settings", methods=["GET", "POST"], response_class=HTMLResponse)
async def settings_page(request: Request):
    return HTMLResponse(SETTINGS_HTML)


SETTINGS_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Настройки каналов</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    /* Стиль формы — по дизайн-системе edna. Справочник и правила: docs/STYLE_GUIDE.md */
    /* ---- Дизайн-токены edna (из дизайн-системы) ---- */
    :root {
      --edna-font: 'Noto Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      --edna-text: #121212;           /* основной текст (Grey900) */
      --edna-text-secondary: #5B5B5B; /* вторичный текст, заголовки таблиц (Grey600) */
      --edna-text-tertiary: #B7B7B7;  /* приглушённый */
      --edna-divider: #ECECEC;        /* линии-разделители (Grey100) */
      --edna-green: #09A460;          /* Positive500 — «активно» */
      --edna-red: #E11900;            /* Red500 — ошибка/негатив */
      --edna-blue: #276EF1;           /* Blue500 — нейтральный статус */
      --edna-primary: #121212;        /* Grey900 — заливка главной кнопки (подтверждено по Figma) */
    }

    body { background: #fff; padding: 24px; font-size: 14px; color: var(--edna-text); font-family: var(--edna-font); }
    .card { border: none; box-shadow: 0 1px 4px rgba(0,0,0,.1); border-radius: 8px; overflow: hidden; }
    .webhook-box { background: #e8f0fe; border-radius: 6px; padding: 10px 14px; word-break: break-all; font-family: monospace; font-size: 13px; }

    /* ---- Таблица в стиле edna: серые мелкие заголовки, тонкие светлые разделители ---- */
    #channelsTable { font-family: var(--edna-font); }
    #channelsTable thead th {
      background: #fff;
      color: var(--edna-text-secondary);
      font-size: 12px;
      font-weight: 600;
      border-bottom: 1px solid var(--edna-divider);
      padding: 12px 8px;
    }
    #channelsTable tbody td {
      color: var(--edna-text);
      font-size: 13px;
      border-bottom: 1px solid var(--edna-divider);
      padding: 12px 8px;
      vertical-align: middle;
    }

    /* ---- Статусы edna: контурные «таблетки», прозрачный фон, цветной текст + рамка ---- */
    .badge-active, .badge-inactive, .badge-warning {
      background: transparent;
      border: 1px solid;
      border-radius: 24px;
      padding: 4px 12px;
      font-size: 12px;
      font-weight: 600;
      font-family: var(--edna-font);
    }
    .badge-active { color: var(--edna-green); border-color: var(--edna-green); }
    .badge-inactive { color: var(--edna-text-tertiary); border-color: var(--edna-text-tertiary); }
    .badge-warning { color: var(--edna-blue); border-color: var(--edna-blue); } /* edna для нейтральных статусов использует синий */

    /* ---- Кнопки edna ---- */
    .btn { border-radius: 8px; font-size: 14px; font-weight: 600; padding: 8px 20px; font-family: var(--edna-font); }
    .btn-sm { border-radius: 8px; padding: 6px 14px; font-size: 13px; }

    /* Главная кнопка: сплошная заливка, белый текст; при наведении текст светлеет (как у edna) */
    .btn-primary { background: var(--edna-primary); border-color: var(--edna-primary); color: #fff; }
    .btn-primary:hover, .btn-primary:focus, .btn-primary:active {
      background: var(--edna-primary); border-color: var(--edna-primary); color: #d9d9d9;
    }
    .btn-primary:disabled { background: var(--edna-primary); border-color: var(--edna-primary); opacity: .55; }

    /* Вторичная кнопка: белый фон, тёмный текст, мягкая тень (точно по edna) */
    .btn-outline-secondary {
      background: #fff; color: var(--edna-text); border: 1px solid #fff;
      box-shadow: 0 1px 4px -1px rgba(18,18,18,.2);
    }
    .btn-outline-secondary:hover, .btn-outline-secondary:focus {
      background: #fff; color: var(--edna-text); border-color: #fff;
      box-shadow: 0 2px 6px -1px rgba(18,18,18,.28);
    }

    /* Кнопка «Отключить»: контурная красная (Red500), заливается при наведении */
    .btn-outline-danger { background: #fff; color: var(--edna-red); border: 1px solid var(--edna-red); }
    .btn-outline-danger:hover, .btn-outline-danger:focus { background: var(--edna-red); color: #fff; border-color: var(--edna-red); }

    .btn-link { color: var(--edna-blue); font-weight: 600; text-decoration: none; }
    .btn-link:hover { color: var(--edna-blue); text-decoration: underline; }

    /* ---- Поля ввода edna: тёмная рамка, скругление 8px ---- */
    .form-control, .form-select {
      border: 1px solid var(--edna-text);
      border-radius: 8px;
      font-size: 14px;
      color: var(--edna-text);
      padding: 8px 12px;
    }
    .form-control::placeholder { color: var(--edna-text-tertiary); }
    .form-control:focus, .form-select:focus {
      border-color: var(--edna-text);
      box-shadow: 0 0 0 2px rgba(18,18,18,.12);  /* нейтральная подсветка вместо синей Bootstrap */
    }
    .form-label { font-weight: 600; font-size: 13px; color: var(--edna-text); }

    /* ---- Блок помощи: плитки edna (зелёный акцент Positive500) ---- */
    .help-block {
      background: #f0faf5;
      border: 1px solid #cdebdd;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 20px;
    }
    .help-block-title { font-size: 13px; font-weight: 600; color: var(--edna-text); margin-bottom: 12px; }
    .help-tiles { display: flex; gap: 12px; flex-wrap: wrap; }
    .help-tile {
      flex: 1 1 220px;
      display: block;
      background: #fff;
      border: 1px solid var(--edna-divider);
      border-radius: 8px;
      padding: 14px 16px;
      text-decoration: none;
      color: var(--edna-text);
      box-shadow: 0 1px 4px -1px rgba(18,18,18,.12);
      transition: box-shadow .15s, border-color .15s;
    }
    .help-tile:hover { box-shadow: 0 3px 10px -2px rgba(9,164,96,.22); border-color: var(--edna-green); color: var(--edna-text); }
    .help-tile-name { font-weight: 600; font-size: 14px; color: var(--edna-text); }
    .help-tile-ico { width: 20px; height: 20px; vertical-align: -4px; margin-right: 7px; }
    .help-tile-name .arrow { color: var(--edna-green); }
    .help-tile-sub { font-size: 12px; color: var(--edna-text-secondary); margin-top: 4px; }
    /* Узкое окно: три плитки в ряд не помещаются — «Подключиться к edna» уходит
       широким блоком наверх, две инструкции остаются под ней. */
    @media (max-width: 820px) {
      .help-tile-primary { flex-basis: 100%; }
    }

    /* ---- Внутреннее модальное окно edna (вместо браузерных confirm/alert) ---- */
    .modal-backdrop-edna {
      position: fixed; inset: 0; z-index: 1050;
      background: rgba(18,18,18,.45);
      display: flex; align-items: center; justify-content: center;
      padding: 24px;
    }
    .modal-box-edna {
      background: #fff; border-radius: 8px;
      box-shadow: 0 8px 28px rgba(18,18,18,.25);
      max-width: 420px; width: 100%;
      padding: 24px;
      font-family: var(--edna-font);
    }
    .modal-title-edna { font-size: 16px; font-weight: 700; color: var(--edna-text); margin-bottom: 8px; }
    .modal-title-edna:empty { display: none; }
    .modal-message-edna { font-size: 14px; color: var(--edna-text); line-height: 1.5; }
    .modal-actions-edna { display: flex; justify-content: flex-end; gap: 10px; margin-top: 24px; }
  </style>
</head>
<body>

<div id="app">
  <div id="loading" class="text-center py-5">
    <div class="spinner-border text-primary" role="status"></div>
    <p class="mt-2 text-muted">Загрузка...</p>
  </div>

  <div id="main" style="display:none">
    <div id="paymentNotice" class="d-none mb-4" style="background:#fffbf0; border-left:3px solid #e0c060; border-radius:4px; padding:10px 16px; font-size:13px; color:#666;">
      Подписка Bitrix истекла. Канал может не работать в Открытых линиях.
    </div>

    <h5 class="mb-4">Настройки каналов</h5>

    <!-- Open Line section -->
    <div class="card mb-4">
      <div class="card-header bg-white fw-semibold">Открытая линия Битрикс24</div>
      <div class="card-body">
        <p class="text-muted small mb-3">Сообщения от клиентов будут поступать в эту открытую линию.</p>
        <div id="openLineStatus" class="d-flex flex-wrap align-items-center gap-2"></div>
        <div id="openLineForm" class="d-none mt-2">
          <select class="form-select mb-2" id="lineSelect"></select>
          <div id="openLineError" class="alert alert-danger d-none mb-2"></div>
          <button class="btn btn-primary btn-sm" id="saveLineBtn" onclick="saveOpenLine()">Сохранить</button>
          <button class="btn btn-outline-secondary btn-sm ms-2" onclick="cancelLineEdit()">Отмена</button>
        </div>
      </div>
    </div>

    <!-- Channels list -->
    <div class="card mb-4">
      <div class="card-body p-0">
        <table class="table table-hover mb-0" id="channelsTable">
          <thead class="table-light">
            <tr>
              <th>Название</th>
              <th>Sender ID</th>
              <th>Тип канала</th>
              <th>Подключён</th>
              <th>Статус</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="channelsList">
            <tr id="emptyRow"><td colspan="6" class="text-center text-muted py-3">Каналов нет</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Add channel form -->
    <div class="card">
      <div class="card-header bg-white fw-semibold">Подключить новый канал</div>
      <div class="card-body">
        <div class="help-block">
          <div class="help-block-title">Ещё нет аккаунта или канала в edna? Начните отсюда:</div>
          <!-- Логотипы каналов MAX и MAX Bot (из макетов edna). Форма и значок общие,
               различаются только градиенты. Конический градиент оригинала заменён линейным
               из тех же цветов: в SVG конического нет, Figma выгружает его через foreignObject. -->
          <svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
            <defs>
              <linearGradient id="maxGrad" x1="2" y1="22" x2="22" y2="2" gradientUnits="userSpaceOnUse">
                <stop offset="0" stop-color="#DB59FF"/><stop offset=".5" stop-color="#5213E2"/><stop offset="1" stop-color="#A500C6"/>
              </linearGradient>
              <linearGradient id="maxBotGrad" x1="2" y1="22" x2="22" y2="2" gradientUnits="userSpaceOnUse">
                <stop offset="0" stop-color="#00CCFF"/><stop offset=".5" stop-color="#0026FF"/><stop offset="1" stop-color="#A500C6"/>
              </linearGradient>
              <path id="maxIcoShape" d="M20.8611 22.131C22.0666 21.2682 22.7979 20.1238 22.9081 18.6276C22.9662 17.8351 22.9999 17.0426 23.0029 16.2501C23.0121 13.0771 23.0121 9.90411 22.9938 6.73417C22.9907 6.10691 22.9417 5.47047 22.8285 4.8524C22.5593 3.35616 21.6689 2.30359 20.3623 1.57842C19.729 1.22655 19.0313 1.10416 18.3184 1.06744C17.4892 1.0246 16.66 1.00318 15.8308 1.00318C12.82 0.997064 9.81219 1.00012 6.80136 1.0093C6.08843 1.01236 5.37244 1.04296 4.67481 1.20513C3.27342 1.52641 2.27287 2.38315 1.5783 3.62542C1.22336 4.26492 1.11627 4.96561 1.05814 5.6816C1.03672 5.93556 1.01836 6.18953 1 6.44043C1 10.1367 1 13.8359 1 17.5322C1.02142 17.7953 1.03978 18.0584 1.0612 18.3216C1.11933 19.0376 1.22642 19.7413 1.58442 20.3778C2.41362 21.8526 3.6406 22.7399 5.34796 22.9082C5.66618 22.9388 5.98745 22.9541 6.30567 22.9755C6.36075 22.9786 6.41582 22.9939 6.4709 23C10.1579 23 13.845 23 17.532 23C17.7952 22.9816 18.0583 22.9602 18.3215 22.9419C19.2333 22.8837 20.1023 22.6757 20.8611 22.131Z"/>
              <path id="maxIcoGlyph" d="M18.9989 11.5935C18.9963 11.511 18.986 11.431 18.9783 11.351C18.8243 9.72579 18.2056 8.29918 17.0785 7.12538C15.5535 5.53882 13.6717 4.84744 11.4895 5.02802C9.64361 5.18281 8.06986 5.94384 6.80674 7.31628C5.68997 8.52877 5.10206 9.97603 5.01477 11.6219C4.96599 12.5377 5.03787 13.451 5.23299 14.3487C5.39216 15.0866 5.58728 15.8166 5.76442 16.5519C5.90305 17.1246 6.00831 17.7024 6.01088 18.2932C6.01345 18.7034 6.26504 18.982 6.67068 18.9975C7.5564 19.0336 8.29065 18.6776 8.91963 18.0791C9.01719 17.9862 9.06854 17.9862 9.1738 18.0533C9.46133 18.239 9.74887 18.4274 10.0492 18.5847C10.5396 18.8375 11.0736 18.9253 11.6204 18.9588C12.6114 19.0207 13.5742 18.902 14.5035 18.5512C17.2377 17.5193 19.004 14.9524 18.9989 12.0192C18.9989 11.8773 19.0014 11.7354 18.9989 11.5935ZM13.7025 15.1227C12.9015 15.5406 12.062 15.6851 11.1866 15.4219C10.8682 15.3265 10.5678 15.1562 10.2675 15.0092C10.175 14.9627 10.1263 14.9627 10.0467 15.0298C9.85927 15.1923 9.66672 15.3497 9.46133 15.4864C9.19947 15.6593 9.07367 15.6206 8.9299 15.342C8.71682 14.9369 8.62183 14.4958 8.56021 14.0495C8.50116 13.6264 8.47549 13.1956 8.43441 12.7699C8.45495 11.9521 8.56278 11.155 8.93247 10.4172C9.52295 9.23305 10.4574 8.52877 11.7924 8.4359C13.7154 8.30175 15.4329 9.77996 15.5972 11.7019C15.7153 13.0898 14.9554 14.47 13.7025 15.1227Z"/>
            </defs>
          </svg>
          <div class="help-tiles">
            <a class="help-tile help-tile-primary" href="https://edna.ru/max/" target="_blank" rel="noopener">
              <div class="help-tile-name">Подключиться к edna <span class="arrow">↗</span></div>
              <div class="help-tile-sub">Зарегистрироваться и подключить MAX — edna.ru/max</div>
            </a>
            <a class="help-tile" href="https://docs-pulse.edna.ru/docs/channel/max/maxbot-getting-started" target="_blank" rel="noopener">
              <div class="help-tile-name"><svg class="help-tile-ico" viewBox="0 0 24 24" aria-hidden="true"><use href="#maxIcoShape" fill="url(#maxBotGrad)"/><use href="#maxIcoGlyph" fill="#fff"/></svg>Как подключить MAX Bot <span class="arrow">↗</span></div>
              <div class="help-tile-sub">Пошаговая инструкция в базе знаний edna</div>
            </a>
            <a class="help-tile" href="https://docs-pulse.edna.ru/docs/channel/max/max-getting-started" target="_blank" rel="noopener">
              <div class="help-tile-name"><svg class="help-tile-ico" viewBox="0 0 24 24" aria-hidden="true"><use href="#maxIcoShape" fill="url(#maxGrad)"/><use href="#maxIcoGlyph" fill="#fff"/></svg>Как подключить MAX <span class="arrow">↗</span></div>
              <div class="help-tile-sub">Пошаговая инструкция в базе знаний edna</div>
            </a>
          </div>
        </div>
        <div id="formError" class="alert alert-danger d-none"></div>
        <div class="mb-3">
          <label class="form-label">Название канала</label>
          <input type="text" class="form-control" id="channelName" placeholder="Например: Продажи">
          <div id="nameError" class="text-danger small mt-1 d-none"></div>
        </div>
        <div class="mb-3">
          <label class="form-label">API-ключ (X-API-KEY)</label>
          <div class="form-text mb-2">Вставьте ваш API-ключ из edna - раздел <a href="https://app.edna.ru/integration/settings" target="_blank" rel="noopener">Интеграции → Настройки → Основной профиль ↗</a></div>
          <input type="text" class="form-control" id="apiKey" placeholder="Ваш API-ключ от edna">
        </div>
        <div class="mb-3">
          <label class="form-label">Sender ID</label>
          <div class="form-text mb-2">Отображается в столбце «Отправитель» в <a href="https://app.edna.ru/channels/tabs/list" target="_blank" rel="noopener">списке каналов edna ↗</a></div>
          <input type="text" class="form-control" id="sender" placeholder="Идентификатор канала в edna">
          <div id="senderError" class="text-danger small mt-1 d-none"></div>
        </div>
        <button class="btn btn-primary" id="saveBtn" onclick="saveChannel()">
          <span id="saveBtnText">Подключить</span>
          <span id="saveBtnSpinner" class="spinner-border spinner-border-sm d-none ms-1"></span>
        </button>
        <div id="connectingNotice" class="text-muted small mt-2 d-none">
          Выполняется подключение канала и настройка webhook в edna — это может занять несколько секунд…
        </div>
      </div>
    </div>

    <!-- Result (shown after save) -->
    <div id="webhookBlock" class="card mt-4 d-none">
      <div class="card-body">
        <!-- Success: webhook auto-registered in edna -->
        <div id="webhookAuto" class="d-none">
          <h6 class="mb-1 text-success">✓ Канал подключён и настроен</h6>
          <p class="text-muted small mb-0">Webhook автоматически прописан в edna — больше ничего делать не нужно.</p>
        </div>
        <!-- Fallback: ask the user to set the URL manually -->
        <div id="webhookManual" class="d-none">
          <h6 class="mb-2">✓ Канал подключён</h6>
          <div id="webhookManualReason" class="alert alert-warning small d-none mb-2"></div>
          <p class="text-muted small mb-2">Скопируйте этот URL и укажите его как webhook для входящих сообщений в личном кабинете edna:</p>
          <div class="webhook-box" id="webhookUrl"></div>
          <button class="btn btn-outline-secondary btn-sm mt-2" onclick="copyWebhook()">Скопировать</button>
          <span id="copyConfirm" class="text-success ms-2 small" style="display:none">Ссылка скопирована</span>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Внутреннее модальное окно (замена браузерных confirm/alert — они блокируются в iframe) -->
<div id="modalBackdrop" class="modal-backdrop-edna d-none">
  <div class="modal-box-edna" role="dialog" aria-modal="true">
    <div id="modalTitle" class="modal-title-edna"></div>
    <div id="modalMessage" class="modal-message-edna"></div>
    <div class="modal-actions-edna">
      <button type="button" id="modalCancelBtn" class="btn btn-outline-secondary btn-sm">Отмена</button>
      <button type="button" id="modalOkBtn" class="btn btn-primary btn-sm">OK</button>
    </div>
  </div>
</div>

<script src="//api.bitrix24.com/api/v1/"></script>
<script>
var memberId = null;
var openLinesData = null;

BX24.init(function() {
  BX24.callMethod('profile', {}, function(res) {
    if (res.error()) {
      showError('Ошибка авторизации Битрикс24');
      return;
    }
    var auth = BX24.getAuth();
    memberId = auth.member_id;
    if (auth.domain) {
      fetch('/api/portal/repair-endpoint', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({member_id: memberId, domain: auth.domain})
      }).catch(function() {});
    }
    loadChannels();
    loadOpenLines();
    loadPortalStatus();
  });
});

function loadPortalStatus() {
  fetch('/api/portal/status?member_id=' + encodeURIComponent(memberId))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.payment_required) {
        document.getElementById('paymentNotice').classList.remove('d-none');
      }
    })
    .catch(function() {});
}

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

function loadOpenLines() {
  fetch('/api/open-lines?member_id=' + encodeURIComponent(memberId))
    .then(function(r) { return r.json(); })
    .then(function(data) { openLinesData = data; renderOpenLine(data); })
    .catch(function() {
      document.getElementById('openLineStatus').innerHTML = '<span class="text-danger">Не удалось загрузить список линий</span>';
    });
}

function renderOpenLine(data) {
  var status = document.getElementById('openLineStatus');
  var line = data.current_line_id
    ? (data.lines || []).find(function(l) { return String(l.ID) === String(data.current_line_id); })
    : null;
  var name = line
    ? (line.LINE_NAME || line.NAME || ('Линия #' + data.current_line_id))
    : (data.current_line_id ? ('Линия #' + data.current_line_id) : null);

  var lines = data.lines || [];
  if (name) {
    status.innerHTML =
      '<span class="badge badge-active rounded-pill">Подключена</span><span>' + esc(name) + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="editOpenLine()">Изменить</button>';
  } else if (lines.length) {
    status.innerHTML =
      '<span class="badge badge-warning rounded-pill">Не выбрана</span>' +
      '<span>Выберите открытую линию — без неё сообщения от клиентов не попадут в Битрикс24.</span>' +
      '<button class="btn btn-primary btn-sm" onclick="editOpenLine()">Выбрать линию</button>';
  } else {
    status.innerHTML =
      '<span class="badge badge-warning rounded-pill">Не создана</span>' +
      '<span>В вашем Битрикс24 нет открытых линий. Нажмите кнопку, чтобы создать линию «edna MAX» автоматически.</span>' +
      '<button class="btn btn-primary btn-sm" id="createLineBtn" onclick="createOpenLine()">Создать линию</button>';
  }
}

function createOpenLine() {
  showConfirm('Создать открытую линию «edna MAX» в Битрикс24?', function() {
    var btn = document.getElementById('createLineBtn');
    btn.disabled = true;
    btn.textContent = 'Создаём...';
    fetch('/api/open-lines/create?member_id=' + encodeURIComponent(memberId), {method: 'POST'})
      .then(function(r) {
        if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail || 'Ошибка'); });
        return r.json();
      })
      .then(function() { loadOpenLines(); })
      .catch(function(e) {
        btn.disabled = false;
        btn.textContent = 'Создать линию';
        showNotice('Ошибка: ' + e.message);
      });
  });
}

function editOpenLine() {
  var select = document.getElementById('lineSelect');
  select.innerHTML = '';
  var lines = openLinesData && openLinesData.lines ? openLinesData.lines : [];
  if (!lines.length) {
    select.innerHTML = '<option disabled>Нет доступных открытых линий</option>';
  } else {
    lines.forEach(function(l) {
      var opt = document.createElement('option');
      opt.value = l.ID;
      opt.textContent = l.LINE_NAME || l.NAME || ('Линия #' + l.ID);
      if (openLinesData && String(l.ID) === String(openLinesData.current_line_id)) opt.selected = true;
      select.appendChild(opt);
    });
  }
  document.getElementById('openLineError').classList.add('d-none');
  document.getElementById('openLineForm').classList.remove('d-none');
  document.getElementById('openLineStatus').classList.add('d-none');
}

function cancelLineEdit() {
  document.getElementById('openLineForm').classList.add('d-none');
  document.getElementById('openLineStatus').classList.remove('d-none');
}

function saveOpenLine() {
  var lineId = document.getElementById('lineSelect').value;
  if (!lineId) return;
  document.getElementById('saveLineBtn').disabled = true;
  document.getElementById('openLineError').classList.add('d-none');
  fetch('/api/portal/open-line', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({member_id: memberId, line_id: lineId})
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail || 'Ошибка сохранения'); });
    return r.json();
  })
  .then(function() {
    document.getElementById('saveLineBtn').disabled = false;
    cancelLineEdit();
    loadOpenLines();
  })
  .catch(function(e) {
    document.getElementById('saveLineBtn').disabled = false;
    var err = document.getElementById('openLineError');
    err.textContent = e.message;
    err.classList.remove('d-none');
  });
}

function renderChannels(channels) {
  var tbody = document.getElementById('channelsList');
  var emptyRow = document.getElementById('emptyRow');
  var rows = [];

  channels.forEach(function(ch, idx) {
    var date = new Date(ch.connected_at).toLocaleDateString('ru-RU');
    var badge = ch.is_active
      ? '<span class="badge badge-active rounded-pill">Активен</span>'
      : '<span class="badge badge-inactive rounded-pill">Отключён</span>';
    var btn = ch.is_active
      ? '<button class="btn btn-outline-danger btn-sm" onclick="disconnect(' + ch.id + ')">Отключить</button>'
      : '';
    var hiddenAttr = idx >= 3 ? ' class="channel-hidden" style="display:none"' : '';
    rows.push('<tr' + hiddenAttr + '>' +
      '<td>' + esc(ch.name) + '</td>' +
      '<td class="font-monospace">' + esc(ch.sender) + '</td>' +
      '<td>' + (ch.channel_type ? esc(ch.channel_type) : '<span class="text-muted">—</span>') + '</td>' +
      '<td>' + date + '</td>' +
      '<td>' + badge + '</td>' +
      '<td>' + btn + '</td>' +
      '</tr>');
  });

  if (rows.length) {
    emptyRow.style.display = 'none';
    var html = rows.join('');
    if (channels.length > 3) {
      html += '<tr id="showAllRow"><td colspan="6" class="text-center py-2">' +
        '<button class="btn btn-link btn-sm p-0" onclick="showAllChannels()">' +
        'Все каналы (' + channels.length + ')' +
        '</button></td></tr>';
    }
    tbody.innerHTML = html + emptyRow.outerHTML;
  }
}

function showAllChannels() {
  document.querySelectorAll('#channelsList .channel-hidden').forEach(function(el) {
    el.style.display = '';
  });
  var row = document.getElementById('showAllRow');
  if (row) row.style.display = 'none';
}

function saveChannel() {
  var name = document.getElementById('channelName').value.trim();
  var apiKey = document.getElementById('apiKey').value.trim();
  var sender = document.getElementById('sender').value.trim();

  clearFieldErrors();
  document.getElementById('formError').classList.add('d-none');
  document.getElementById('webhookBlock').classList.add('d-none');

  if (!name || !apiKey || !sender) {
    showFormError('Заполните все поля');
    return;
  }

  setLoading(true);

  fetch('/api/channels', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({member_id: memberId, name: name, api_key: apiKey, sender: sender})
  })
  .then(function(r) {
    return r.json().then(function(data) {
      if (!r.ok) {
        setLoading(false);
        var detail = data.detail;
        if (detail && typeof detail === 'object' && detail.field) {
          showFieldError(detail.field === 'sender' ? 'senderError' : 'nameError', detail.message);
        } else {
          showFormError(typeof detail === 'string' ? detail : 'Ошибка сохранения');
        }
        return;
      }
      setLoading(false);
      document.getElementById('channelName').value = '';
      document.getElementById('apiKey').value = '';
      document.getElementById('sender').value = '';
      showWebhookResult(data);
      loadChannels();
    });
  })
  .catch(function() {
    setLoading(false);
    showFormError('Ошибка сети');
  });
}

function disconnect(channelId) {
  showConfirm('Отключить канал?', function() {
    fetch('/api/channels/' + channelId + '/disconnect?member_id=' + encodeURIComponent(memberId), {
      method: 'POST'
    })
    .then(function(r) {
      if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail || 'Ошибка'); });
      return r.json();
    })
    .then(function() { loadChannels(); })
    .catch(function(e) { showNotice('Ошибка: ' + e.message); });
  });
}

function showWebhookResult(data) {
  document.getElementById('webhookUrl').textContent = data.webhook_url;
  var auto = document.getElementById('webhookAuto');
  var manual = document.getElementById('webhookManual');
  if (data.auto_configured) {
    auto.classList.remove('d-none');
    manual.classList.add('d-none');
  } else {
    auto.classList.add('d-none');
    manual.classList.remove('d-none');
    var reason = document.getElementById('webhookManualReason');
    if (data.auto_error) {
      reason.textContent = 'Автоматическая настройка не удалась: ' + data.auto_error;
      reason.classList.remove('d-none');
    } else {
      reason.classList.add('d-none');
    }
  }
  document.getElementById('webhookBlock').classList.remove('d-none');
}

function copyWebhook() {
  var url = document.getElementById('webhookUrl').textContent;
  var ta = document.createElement('textarea');
  ta.value = url;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  var confirm = document.getElementById('copyConfirm');
  confirm.style.display = 'inline';
  setTimeout(function() { confirm.style.display = 'none'; }, 5000);
}

function setLoading(on) {
  document.getElementById('saveBtn').disabled = on;
  document.getElementById('saveBtnSpinner').classList.toggle('d-none', !on);
  document.getElementById('saveBtnText').textContent = on ? 'Подключение…' : 'Подключить';
  document.getElementById('connectingNotice').classList.toggle('d-none', !on);
}

function showFormError(msg) {
  var el = document.getElementById('formError');
  el.textContent = msg;
  el.classList.remove('d-none');
}

function showFieldError(elId, msg) {
  var el = document.getElementById(elId);
  el.textContent = msg;
  el.classList.remove('d-none');
}

function clearFieldErrors() {
  ['nameError', 'senderError'].forEach(function(id) {
    var el = document.getElementById(id);
    el.textContent = '';
    el.classList.add('d-none');
  });
}

// ---- Внутреннее модальное окно (замена браузерных confirm/alert) ----
function showModal(opts) {
  var backdrop = document.getElementById('modalBackdrop');
  document.getElementById('modalTitle').textContent = opts.title || '';
  document.getElementById('modalMessage').textContent = opts.message || '';
  var okBtn = document.getElementById('modalOkBtn');
  var cancelBtn = document.getElementById('modalCancelBtn');
  okBtn.textContent = opts.okText || 'OK';
  cancelBtn.textContent = opts.cancelText || 'Отмена';
  cancelBtn.style.display = opts.showCancel ? '' : 'none';

  function close() {
    backdrop.classList.add('d-none');
    okBtn.onclick = null;
    cancelBtn.onclick = null;
    backdrop.onclick = null;
    document.onkeydown = null;
  }
  okBtn.onclick = function() { close(); if (opts.onOk) opts.onOk(); };
  cancelBtn.onclick = function() { close(); };
  backdrop.onclick = function(e) { if (e.target === backdrop) close(); };
  document.onkeydown = function(e) { if (e.key === 'Escape') close(); };

  backdrop.classList.remove('d-none');
  okBtn.focus();
}

// Подтверждение действия (две кнопки) — замена confirm()
function showConfirm(message, onOk) {
  showModal({ message: message, showCancel: true, okText: 'OK', onOk: onOk });
}

// Уведомление (одна кнопка) — замена alert()
function showNotice(message) {
  showModal({ message: message, showCancel: false, okText: 'Понятно' });
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>
"""
