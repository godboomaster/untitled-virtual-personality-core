/* Эталонный bridge-скрипт скина. Приложение ВСТАВЛЯЕТ этот код в файл
   скина при загрузке (заменяя блок между маркерами VPC-BRIDGE), поэтому
   сторонний редактор не может его сломать окончательно.

   Bridge живёт внутри sandboxed iframe скина и отвечает за:
   - приём снапшота данных от хоста (postMessage {vpc:'host', type:'state'})
     и наполнение hook-точек [data-vpc] / [data-vpc-field];
   - отправку событий хосту: ready / send / clear / select-persona /
     open-dossier / close-dossier / error.

   JS написан без шаблонных литералов и стрелок нарочно: файл встраивается
   в TS как строка и исполняется в любом современном браузере. */

export const BRIDGE_START = '<!-- ==== VPC-BRIDGE:START';
export const BRIDGE_END = 'VPC-BRIDGE:END ==== -->';

export const BRIDGE_SOURCE = String.raw`
(function () {
  'use strict';

  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function post(msg) {
    try { parent.postMessage(msg, '*'); } catch (e) { /* нет родителя — ок */ }
  }

  // ── Заполнение полей [data-vpc-field] внутри клона шаблона ──
  function fillFields(root, data) {
    var els = qsa('[data-vpc-field]', root);
    // Поле может быть на самом корне клона (напр. <div data-vpc-field="text">)
    if (root.hasAttribute && root.hasAttribute('data-vpc-field')) { els.unshift(root); }
    els.forEach(function (el) {
      var name = el.getAttribute('data-vpc-field');
      var value = data[name];
      if (value == null || value === '') { el.remove(); return; }
      if (name === 'image') {
        if (el.tagName === 'IMG') { el.src = value; }
        else { el.style.backgroundImage = 'url("' + String(value).replace(/"/g, '%22') + '")'; }
        el.hidden = false;
        return;
      }
      // Поля-инпуты (напр. модель провайдера) получают value, остальные — текст
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
        el.value = String(value);
        return;
      }
      // Элемент с data-vpc-bar — заполняемая полоса прогресса (значение = %)
      if (el.hasAttribute('data-vpc-bar')) {
        el.style.width = String(value) + '%';
        return;
      }
      el.textContent = String(value);
    });
  }

  // ── Привязка интерактива внутри элемента списка ──
  function bindOnchange(el) {
    el.addEventListener('change', function () {
      var host = el.closest('[data-item-id]');
      var values = {};
      values[el.getAttribute('data-vpc-input') || 'value'] = el.value;
      post({
        vpc: 'skin', type: 'action',
        action: el.getAttribute('data-vpc-onchange'),
        id: host ? host.getAttribute('data-item-id') : null,
        values: values
      });
    });
  }

  function bindInner(root) {
    // Кнопки действий над элементом (data-vpc-item-action="toggle-todo" и т.п.);
    // атрибут может быть и на самом корне клона (напр. feature-flag)
    var actionEls = qsa('[data-vpc-item-action]', root);
    if (root.hasAttribute && root.hasAttribute('data-vpc-item-action')) { actionEls.unshift(root); }
    actionEls.forEach(function (el) {
      el.addEventListener('click', function (e) {
        e.stopPropagation();
        var action = el.getAttribute('data-vpc-item-action');
        var host = el.closest('[data-item-id]');
        // Правка (edit-*): заполняет форму значениями элемента, хосту ничего
        // не уходит — форма отправит своё действие с id (data-editing)
        if (action.indexOf('edit-') === 0) {
          var form = qs('[data-vpc-form][data-vpc-for="' + action.slice(5) + '"]');
          if (form && host) {
            qsa('[data-vpc-input]', form).forEach(function (inp) {
              var src = qs('[data-vpc-field="' + inp.getAttribute('data-vpc-input') + '"]', host);
              if (src) { inp.value = src.tagName === 'INPUT' ? src.value : src.textContent; }
            });
            form.setAttribute('data-editing', host.getAttribute('data-item-id'));
            var first = qs('[data-vpc-input]', form);
            if (first) { first.focus(); }
          }
          return;
        }
        post({
          vpc: 'skin', type: 'action',
          action: action,
          id: host ? host.getAttribute('data-item-id') : null,
          values: {}
        });
      });
    });
    // Инпуты с data-vpc-onchange (напр. модель провайдера)
    qsa('[data-vpc-onchange]', root).forEach(bindOnchange);
    if (root.hasAttribute && root.hasAttribute('data-vpc-onchange')) { bindOnchange(root); }
  }

  // ── Опустевшие необязательные контейнеры (напр. цитата без содержимого) ──
  function pruneOptional(root) {
    qsa('[data-vpc-optional]', root).forEach(function (el) {
      if (!el.querySelector('[data-vpc-field]')) { el.remove(); }
    });
  }

  // ── Лента сообщений ──
  function renderMessages(messages) {
    var box = qs('[data-vpc="messages"]');
    var tpl = qs('template[data-vpc="message"]');
    if (!box || !tpl || !tpl.content.firstElementChild) return;
    box.innerHTML = '';
    messages.forEach(function (m) {
      var node = tpl.content.firstElementChild.cloneNode(true);
      node.setAttribute('data-role', m.role === 'user' ? 'user' : 'persona');
      node.setAttribute('data-item-id', String(m.id));
      fillFields(node, {
        text: m.text,
        time: m.time,
        image: m.image,
        'quote-author': m.quote && m.quote.author,
        'quote-text': m.quote && m.quote.text
      });
      pruneOptional(node);
      bindInner(node); // кнопка «ответить» (data-vpc-item-action="reply") и т.п.
      box.appendChild(node);
    });
    box.scrollTop = box.scrollHeight;
  }

  /* ── Реестр повторяющихся списков: контейнер + <template> + маппинг.
     rootFn — атрибуты корня клона (data-active, data-done…),
     clickFn — событие хосту по клику на элемент,
     countKey — элементы [data-vpc-count="<key>"] получают число записей
     (для заголовков раскрывающихся списков). ── */
  var LISTS = [
    {
      get: function (p) { return p.personas; },
      box: '[data-vpc="persona-list"]', tpl: 'persona-item', countKey: 'personas',
      map: function (x) { return { name: x.name, status: x.statusText }; },
      root: function (node, x) { if (x.active) node.setAttribute('data-active', ''); },
      click: function (x) { return { vpc: 'skin', type: 'select-persona', id: x.id }; }
    },
    {
      get: function (p) { return p.todos; },
      box: '[data-vpc="todo-list"]', tpl: 'todo-item', countKey: 'todos',
      countOf: function (items) {
        return items.filter(function (x) { return !x.done; }).length;
      },
      map: function (x) { return { text: x.text }; },
      root: function (node, x) { if (x.done) node.setAttribute('data-done', ''); }
    },
    {
      get: function (p) { return p.context && p.context.features; },
      box: '[data-vpc="feature-list"]', tpl: 'feature-item', countKey: 'features',
      map: function (label) { return { label: label }; }
    },
    {
      get: function (p) { return p.inventory; },
      box: '[data-vpc="inventory-list"]', tpl: 'inventory-item', countKey: 'inventory',
      map: function (x) { return { icon: x.icon, name: x.name, description: x.description, tag: x.tag }; }
    },
    {
      get: function (p) { return p.inventory; },
      box: '[data-vpc="room-inventory"]', tpl: 'inventory-item', countKey: 'inventory',
      map: function (x) { return { icon: x.icon, name: x.name, description: x.description, tag: x.tag }; }
    },
    {
      get: function (p) { return p.feed; },
      box: '[data-vpc="room-feed"]', tpl: 'feed-item', countKey: 'feed',
      map: function (x) { return { time: x.time, text: x.text }; }
    },
    {
      get: function (p) { return p.dossier && p.dossier.facts; },
      box: '[data-vpc="fact-list"]', tpl: 'fact-item', countKey: 'facts',
      map: function (x) { return { category: x.category, fact: x.fact }; }
    },
    {
      get: function (p) { return p.dossier && p.dossier.reminders; },
      box: '[data-vpc="reminder-list"]', tpl: 'reminder-item', countKey: 'reminders',
      map: function (x) { return { time: x.time, text: x.text, repeat: x.repeat, date: x.date, clock: x.clock }; },
      root: function (node, x) { node.setAttribute('data-active', x.active ? 'true' : 'false'); }
    },
    {
      get: function (p) { return p.dossier && p.dossier.initiatives; },
      box: '[data-vpc="initiative-list"]', tpl: 'initiative-item', countKey: 'initiatives',
      map: function (x) { return { 'type-label': x.typeLabel, text: x.text, time: x.time, outcome: x.outcome }; }
    },
    {
      get: function (p) { return p.dossier && p.dossier.diary; },
      box: '[data-vpc="diary-list"]', tpl: 'diary-item', countKey: 'diary',
      map: function (x) { return { date: x.date, text: x.text }; }
    },
    {
      get: function (p) { return p.dossier && p.dossier.stm; },
      box: '[data-vpc="stm-list"]', tpl: 'stm-item', countKey: 'stm',
      map: function (x) { return { author: x.author, role: x.role, text: x.text, time: x.time }; },
      root: function (node, x) { node.setAttribute('data-role', x.role); }
    },
    {
      get: function (p) { return p.dossier && p.dossier.courses; },
      box: '[data-vpc="course-list"]', tpl: 'course-item', countKey: 'courses',
      map: function (x) {
        return {
          subject: x.subject, status: x.status, lessons: x.lessons, topics: x.topics,
          words: x.words, quiz: x.quiz, frequency: x.frequency, next: x.next,
          progress: x.progress, 'topics-list': x.topicsList, 'vocab-list': x.vocabList,
          'quiz-line': x.quizLine
        };
      },
      root: function (node, x) { node.setAttribute('data-status', x.statusKey); }
    },
    {
      // История курсов: на паузе и завершённые
      get: function (p) {
        return p.dossier && p.dossier.courses &&
          p.dossier.courses.filter(function (c) { return c.statusKey !== 'active'; });
      },
      box: '[data-vpc="course-history-list"]', tpl: 'course-history-item', countKey: 'courseHistory',
      map: function (x) { return { subject: x.subject, status: x.status, lessons: x.lessons }; },
      root: function (node, x) { node.setAttribute('data-status', x.statusKey); }
    },
    {
      get: function (p) { return p.files; },
      box: '[data-vpc="file-list"]', tpl: 'file-item', countKey: 'files',
      map: function (x) { return { name: x.name, kind: x.kind, size: x.size, date: x.date, description: x.description }; }
    },
    {
      get: function (p) { return p.providers; },
      box: '[data-vpc="provider-list"]', tpl: 'provider-item', countKey: 'providers',
      map: function (x) {
        return { name: x.name, model: x.model, local: x.local ? 'local' : '', 'key-label': x.keyLabel };
      },
      root: function (node, x) {
        node.setAttribute('data-main', x.active ? 'true' : 'false');
        node.setAttribute('data-backup', x.backup ? 'true' : 'false');
      }
    },
    {
      // Карточка «Модели провайдеров» — отдельный список с полем ввода модели
      get: function (p) { return p.providerModels; },
      box: '[data-vpc="pmodel-list"]', tpl: 'pmodel-item', countKey: 'pmodels',
      map: function (x) { return { name: x.name, model: x.model, local: x.local ? 'local' : '' }; }
    },
    {
      // Флаги фич (досье: настройки)
      get: function (p) { return p.featureFlags; },
      box: '[data-vpc="feature-flag-list"]', tpl: 'feature-flag-item', countKey: 'featureFlags',
      map: function (x) { return { label: x.label }; },
      root: function (node, x) { node.setAttribute('data-enabled', x.enabled ? 'true' : 'false'); }
    }
  ];

  function renderList(cfg, payload) {
    var items = cfg.get(payload);
    if (!items) return;
    // Контейнеров с одним hook может быть несколько (напр. дела в сайдбаре и в досье);
    // шаблон ищем сначала в том же экране, что и контейнер, затем — глобально
    qsa(cfg.box).forEach(function (box) {
      var scope = box.closest('[data-vpc-screen]') || document;
      var tpl = qs('template[data-vpc="' + cfg.tpl + '"]', scope) || qs('template[data-vpc="' + cfg.tpl + '"]');
      if (!tpl || !tpl.content.firstElementChild) return;
      box.innerHTML = '';
      items.forEach(function (item) {
        var node = tpl.content.firstElementChild.cloneNode(true);
        if (item && item.id != null) { node.setAttribute('data-item-id', String(item.id)); }
        fillFields(node, cfg.map(item));
        pruneOptional(node);
        if (cfg.root) cfg.root(node, item);
        bindInner(node);
        if (cfg.click) {
          node.addEventListener('click', function () { post(cfg.click(item)); });
        }
        box.appendChild(node);
      });
      // Сворачивание длинных списков: [data-vpc-limit="N"] оставляет
      // последние N элементов, старшие помечаются data-extra (их прячет
      // CSS скина); кнопка [data-vpc-expand="<box>"] переключает вид
      var limit = parseInt(box.getAttribute('data-vpc-limit') || '', 10);
      if (!isNaN(limit) && items.length > limit) {
        var extra = box.children.length - limit;
        for (var i = 0; i < extra; i++) { box.children[i].setAttribute('data-extra', ''); }
        box.setAttribute('data-collapsed', '');
      } else {
        box.removeAttribute('data-collapsed');
        box.removeAttribute('data-expanded');
      }
    });
    // Кнопки «показать всё» видны, только когда список реально свёрнут
    qsa('[data-vpc-expand]').forEach(function (btn) {
      var target = qs('[data-vpc="' + btn.getAttribute('data-vpc-expand') + '"]');
      btn.hidden = !target || !target.hasAttribute('data-collapsed');
      if (target && !target.hasAttribute('data-expanded')) { btn.removeAttribute('data-expanded'); }
    });
    // Счётчики для заголовков раскрывающихся списков
    if (cfg.countKey) {
      var n = cfg.countOf ? cfg.countOf(items) : items.length;
      qsa('[data-vpc-count="' + cfg.countKey + '"]').forEach(function (el) {
        el.textContent = String(n);
      });
    }
  }

  // ── Скалярные текстовые слоты ──
  var TEXT_SLOTS = {
    'persona-name': function (p) { return p.persona && p.persona.name; },
    'persona-status': function (p) { return p.persona && p.persona.statusText; },
    'persona-model': function (p) { return p.persona && p.persona.model; },
    'persona-mood': function (p) { return p.persona && p.persona.mood; },
    'persona-pastime': function (p) { return p.persona && p.persona.pastime; },
    'ctx-pastime': function (p) { return p.context && p.context.pastime; },
    'ctx-mood': function (p) { return p.context && p.context.mood; },
    'ctx-trend': function (p) { return p.context && p.context.trend; },
    'ctx-initiative': function (p) { return p.context && p.context.initiative; },
    'ctx-last-reply': function (p) { return p.context && p.context.lastReply; },
    'ctx-next-reminder': function (p) { return p.context && p.context.nextReminder; },
    'ctx-learning': function (p) { return p.context && p.context.learning; },
    // Параметры и состояние самоинициативы (досье: вкладка «Инициатива»)
    'ini-probability': function (p) { return p.dossier && p.dossier.initState && String(p.dossier.initState.probability); },
    'ini-threshold': function (p) { return p.dossier && p.dossier.initState && String(p.dossier.initState.threshold); },
    'ini-max-per-day': function (p) { return p.dossier && p.dossier.initState && String(p.dossier.initState.maxPerDay); },
    'ini-interval': function (p) { return p.dossier && p.dossier.initState && String(p.dossier.initState.interval); },
    'ini-adaptive': function (p) { return p.dossier && p.dossier.initState && p.dossier.initState.adaptive; },
    'ini-bayes': function (p) { return p.dossier && p.dossier.initState && p.dossier.initState.bayes; },
    'ini-ignore-streak': function (p) { return p.dossier && p.dossier.initState && String(p.dossier.initState.ignoreStreak); },
    'ini-today': function (p) { return p.dossier && p.dossier.initState && String(p.dossier.initState.today); },
    'ini-mood': function (p) { return p.dossier && p.dossier.initState && p.dossier.initState.mood; },
    'ini-stages': function (p) { return p.dossier && p.dossier.initState && p.dossier.initState.stages; },
    'ini-silence-text': function (p) { return p.dossier && p.dossier.initState && p.dossier.initState.silenceText; },
    'room-pastime': function (p) {
      if (!p.room) return null;
      return p.room.pastimeLabel + (p.room.pastimePlace ? ' · ' + p.room.pastimePlace : '');
    },
    'room-mood': function (p) { return p.room && p.room.mood; },
    'room-energy': function (p) { return p.room && p.room.energy; },
    'room-place': function (p) { return p.room && p.room.pastimePlace; },
    'room-pet-label': function (p) { return p.room && p.room.petLabel; }
  };

  function applyState(p) {
    if (!p || typeof p !== 'object') return;

    Object.keys(TEXT_SLOTS).forEach(function (slot) {
      var value = TEXT_SLOTS[slot](p);
      if (value == null) return;
      qsa('[data-vpc="' + slot + '"]').forEach(function (el) { el.textContent = String(value); });
    });

    // Аватар: <img> получает src, иначе — первая буква имени
    qsa('[data-vpc="persona-avatar"]').forEach(function (el) {
      if (el.tagName === 'IMG') {
        if (p.persona && p.persona.avatar) { el.src = p.persona.avatar; el.hidden = false; }
        else { el.hidden = true; }
      } else {
        el.textContent = p.persona && p.persona.name ? p.persona.name.charAt(0) : '?';
      }
    });

    // Плашка «ответ на сообщение» над полем ввода
    var replyBar = qs('[data-vpc="reply-bar"]');
    if (replyBar) {
      if (p.reply) {
        replyBar.hidden = false;
        qsa('[data-vpc-field="reply-author"]', replyBar).forEach(function (el) {
          el.textContent = String(p.reply.author);
        });
        qsa('[data-vpc-field="reply-text"]', replyBar).forEach(function (el) {
          el.textContent = String(p.reply.text);
        });
      } else {
        replyBar.hidden = true;
      }
    }

    // Индикатор «печатает»
    qsa('[data-vpc="typing-indicator"]').forEach(function (el) {
      if (p.typing) { el.setAttribute('data-active', ''); }
      else { el.removeAttribute('data-active'); }
    });

    // Полоса прогресса молчания пользователя (досье: инициатива)
    if (p.dossier && p.dossier.initState) {
      qsa('[data-vpc="ini-silence-bar"]').forEach(function (el) {
        el.style.width = p.dossier.initState.silencePct + '%';
      });
    }

    if (p.messages) { renderMessages(p.messages); }
    LISTS.forEach(function (cfg) { renderList(cfg, p); });

    // Инпуты настроек: значения из снапшота (не трогаем поле в фокусе).
    // Параметры самоинициативы (ini*) берутся из блока досье; чекбоксы
    // (адаптивный порог, байесовская обратная связь) заполняются checked.
    if (p.settings || (p.dossier && p.dossier.initState)) {
      qsa('[data-vpc-setting]').forEach(function (el) {
        var key = el.getAttribute('data-vpc-setting');
        var v = p.settings ? p.settings[key] : null;
        if (v == null && p.dossier && p.dossier.initState) {
          var ini = p.dossier.initState;
          if (key === 'iniSilence') { v = ini.threshold; }
          if (key === 'iniProbability') { v = ini.probability; }
          if (key === 'iniMaxPerDay') { v = ini.maxPerDay; }
          if (key === 'iniInterval') { v = ini.interval; }
          if (key === 'iniAdaptive') { v = ini.adaptive; }
          if (key === 'iniBayes') { v = ini.bayes; }
        }
        if (v != null && document.activeElement !== el) {
          if (el.type === 'checkbox') {
            el.checked = v === true || v === 'true' || v === '✓';
          } else {
            el.value = String(v);
          }
        }
      });
    }

    if (p.room) {
      qsa('[data-vpc="room-avatar"]').forEach(function (el) {
        el.style.setProperty('--vpc-x', (p.room.x == null ? 50 : p.room.x) + '%');
        if (p.room.y != null) { el.style.setProperty('--vpc-y', p.room.y + '%'); }
      });
      qsa('[data-vpc="room-bg"]').forEach(function (el) {
        if (p.room.bg && el.tagName === 'IMG') { el.src = p.room.bg; el.hidden = false; }
        else { el.hidden = true; }
      });
      qsa('[data-vpc="room-sprite"]').forEach(function (el) {
        if (p.room.sprite && el.tagName === 'IMG') { el.src = p.room.sprite; el.hidden = false; }
        else { el.hidden = true; }
      });
      qsa('[data-vpc="room-pet"]').forEach(function (el) {
        if (p.room.pet && p.room.pet !== 'none') {
          el.setAttribute('data-pet', p.room.pet);
          el.hidden = false;
        } else { el.hidden = true; }
      });
    }

    // Декоративный JS скина может реагировать на это событие
    try { document.dispatchEvent(new CustomEvent('vpc:state', { detail: p })); } catch (e) { /* старый движок */ }
  }

  // ── События скина → хост ──
  function bind() {
    var input = qs('[data-vpc="input"]');
    var sendBtn = qs('[data-vpc="send"]');

    // Прикреплённая картинка (читается локально, уходит с send)
    var pendingImage = null;
    function showAttach() {
      qsa('[data-vpc="attach-preview"]').forEach(function (el) {
        if (pendingImage) {
          if (el.tagName === 'IMG') { el.src = pendingImage; }
          el.hidden = false;
        } else {
          el.hidden = true;
        }
      });
    }
    var attachBtn = qs('[data-vpc="attach-image"]');
    if (attachBtn) {
      attachBtn.addEventListener('click', function () {
        var fi = document.createElement('input');
        fi.type = 'file';
        fi.accept = 'image/*';
        fi.onchange = function () {
          var f = fi.files && fi.files[0];
          if (!f || !f.type || f.type.indexOf('image/') !== 0) { return; }
          var reader = new FileReader();
          reader.onload = function () { pendingImage = String(reader.result); showAttach(); };
          reader.readAsDataURL(f);
        };
        fi.click();
      });
    }
    qsa('[data-vpc="cancel-attach"]').forEach(function (el) {
      el.addEventListener('click', function () { pendingImage = null; showAttach(); });
    });

    function send() {
      if (!input) return;
      var text = String(input.value || '').trim();
      if (!text && !pendingImage) return;
      var msg = { vpc: 'skin', type: 'send', text: text };
      if (pendingImage) { msg.image = pendingImage; }
      post(msg);
      input.value = '';
      pendingImage = null;
      showAttach();
    }
    if (sendBtn) { sendBtn.addEventListener('click', send); }
    if (input) {
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
      });
    }
    qsa('[data-vpc="clear-chat"]').forEach(function (el) {
      el.addEventListener('click', function () { post({ vpc: 'skin', type: 'clear' }); });
    });
    qsa('[data-vpc="open-dossier"]').forEach(function (el) {
      el.addEventListener('click', function () { post({ vpc: 'skin', type: 'open-dossier' }); });
    });
    qsa('[data-vpc="close-dossier"]').forEach(function (el) {
      el.addEventListener('click', function () { post({ vpc: 'skin', type: 'close-dossier' }); });
    });
    // Кнопки «показать всё / свернуть» для свёрнутых списков [data-vpc-limit]
    qsa('[data-vpc-expand]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var box = qs('[data-vpc="' + btn.getAttribute('data-vpc-expand') + '"]');
        if (!box) return;
        if (box.hasAttribute('data-expanded')) {
          box.removeAttribute('data-expanded');
          btn.removeAttribute('data-expanded');
        } else {
          box.setAttribute('data-expanded', '');
          btn.setAttribute('data-expanded', '');
        }
      });
    });

    // Лайтбокс картинок: клик по изображению сообщения (data-vpc-field=
    // "image") или превью аттача — просим хост показать её крупно
    // (sandbox: скин сам окно открыть не может, opaque origin)
    document.addEventListener('click', function (e) {
      var el = e.target && e.target.closest
        ? e.target.closest('[data-vpc-field="image"], [data-vpc="attach-preview"]')
        : null;
      if (!el || el.hidden) return;
      var src = '';
      if (el.tagName === 'IMG') { src = el.src || ''; }
      else {
        var m = String(el.style.backgroundImage || '').match(/^url\("?([\s\S]+?)"?\)$/);
        if (m) { src = m[1]; }
      }
      if (src) { post({ vpc: 'skin', type: 'zoom-image', src: src }); }
    });

    // Формы записи: кнопка [data-vpc-action] собирает значения инпутов
    // [data-vpc-input] из ближайшего контейнера [data-vpc-form].
    // Если форма в режиме правки (data-editing выставлен кнопкой edit-*),
    // действие уходит с id редактируемого элемента.
    qsa('[data-vpc-action]').forEach(function (el) {
      el.addEventListener('click', function () {
        var form = el.closest('[data-vpc-form]');
        var values = {};
        if (form) {
          qsa('[data-vpc-input]', form).forEach(function (inp) {
            values[inp.getAttribute('data-vpc-input')] = inp.value;
          });
        }
        post({
          vpc: 'skin', type: 'action',
          action: el.getAttribute('data-vpc-action'),
          id: form ? form.getAttribute('data-editing') : null,
          values: values
        });
        // Очистка полей формы после отправки
        if (form) {
          form.removeAttribute('data-editing');
          qsa('[data-vpc-input]', form).forEach(function (inp) { inp.value = ''; });
        }
      });
    });
    // Enter в инпуте формы = клик по её кнопке действия
    qsa('[data-vpc-form]').forEach(function (form) {
      form.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') return;
        var btn = qs('[data-vpc-action]', form);
        if (btn) { e.preventDefault(); btn.click(); }
      });
    });
    // Инпуты настроек: [data-vpc-setting="temperature"] → set-setting при change
    // (у чекбоксов уходит 'true'/'false' по checked)
    qsa('[data-vpc-setting]').forEach(function (el) {
      el.addEventListener('change', function () {
        post({
          vpc: 'skin', type: 'set-setting',
          key: el.getAttribute('data-vpc-setting'),
          value: el.type === 'checkbox' ? String(el.checked) : el.value
        });
      });
    });
  }

  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.vpc !== 'host') return;
    if (d.type === 'state') { applyState(d.payload); }
  });

  window.addEventListener('error', function (e) {
    post({ vpc: 'skin', type: 'error', message: String(e.message || 'unknown error') });
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { bind(); post({ vpc: 'skin', type: 'ready' }); });
  } else {
    bind();
    post({ vpc: 'skin', type: 'ready' });
  }
})();
`;
