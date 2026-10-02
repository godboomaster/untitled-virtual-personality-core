/* Эталонный bridge-скрипт скина. Приложение ВСТАВЛЯЕТ этот код в файл
   скина при загрузке (заменяя блок между маркерами VPC-BRIDGE), поэтому
   сторонний редактор не может его сломать окончательно.

   Bridge живёт внутри sandboxed iframe скина и отвечает за:
   - приём снапшота данных от хоста (postMessage {vpc:'host', type:'state'})
     и наполнение hook-точек [data-vpc] / [data-vpc-field] / [data-vpc-label];
     лента сообщений сверяется по id (узлы переиспользуются), остальные
     списки перерисовываются, только когда изменились их данные;
   - окружение: data-theme / lang / data-vpc-time-of-day на <html>;
   - отправку событий хосту: ready / send / clear / select-persona /
     open-dossier / close-dossier / action / set-setting / zoom-image /
     key / error;
   - программный API для JS скина: window.vpc (см. contract.ts).

   JS написан без шаблонных литералов и стрелок нарочно: файл встраивается
   в TS как строка и исполняется в любом современном браузере. */

import { SKIN_CONTRACT_VERSION } from './meta';

export const BRIDGE_START = '<!-- ==== VPC-BRIDGE:START';
export const BRIDGE_END = 'VPC-BRIDGE:END ==== -->';

// Место для метки документа (prepareSkin подставляет её вместо заглушки):
// bridge прикладывает метку к каждому событию, хост по ней отсеивает
// события прошлых документов того же iframe
export const BRIDGE_GEN_PLACEHOLDER = '__VPC_GEN__';

export const BRIDGE_SOURCE = String.raw`
(function () {
  'use strict';

  var APP_CONTRACT = ${SKIN_CONTRACT_VERSION};
  var SKIN_CONTRACT = parseInt(document.documentElement.getAttribute('data-vpc-contract') || '1', 10) || 1;
  var GEN = '__VPC_GEN__';
  // Лимит data-URL картинки у хоста (SkinFrame.IMAGE_MAX_CHARS)
  var IMAGE_MAX_CHARS = 8 * 1024 * 1024;

  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function post(msg) {
    msg.gen = GEN;
    try { parent.postMessage(msg, '*'); } catch (e) { /* нет родителя — ок */ }
  }

  // ── Сигнатуры данных: пропуск перерисовки неизменившихся частей ──
  // Длинные data-URL (картинки) заменяются отпечатком: длина + начало,
  // конец и выборка символов — чтобы не гонять мегабайты через stringify.
  // Обычный текст сравнивается целиком: правка в середине длинной строки
  // той же длины отпечаток бы не заметил
  function fingerprint(s) {
    var step = Math.max(1, Math.floor(s.length / 256));
    var out = s.length + '#' + s.slice(0, 64);
    for (var i = 64; i < s.length - 64; i += step) { out += s.charAt(i); }
    return out + s.slice(-64);
  }
  function sig(v) {
    try {
      return JSON.stringify(v, function (k, x) {
        return typeof x === 'string' && x.length > 512 && x.slice(0, 5) === 'data:' ? fingerprint(x) : x;
      });
    } catch (e) { return String(Math.random()); }
  }

  // ── Подписи UI: [data-vpc-label] (текст), -placeholder / -title / -aria
  // (атрибуты). Нет ключа в снапшоте — остаётся исходный текст скина ──
  var labels = {};
  var labelsSig = null;
  var LABEL_ATTRS = [
    ['data-vpc-label-placeholder', 'placeholder'],
    ['data-vpc-label-title', 'title'],
    ['data-vpc-label-aria', 'aria-label']
  ];
  function withRoot(sel, root) {
    var els = qsa(sel, root || document);
    if (root && root.matches && root.matches(sel)) { els.unshift(root); }
    return els;
  }
  function applyLabels(root) {
    withRoot('[data-vpc-label]', root).forEach(function (el) {
      if (el.__vpcLabel === undefined) { el.__vpcLabel = el.textContent; }
      var v = labels[el.getAttribute('data-vpc-label')];
      var s = v != null ? String(v) : el.__vpcLabel;
      if (el.textContent !== s) { el.textContent = s; }
    });
    LABEL_ATTRS.forEach(function (pair) {
      withRoot('[' + pair[0] + ']', root).forEach(function (el) {
        var store = '__vpcAttr_' + pair[1];
        if (el[store] === undefined) { el[store] = el.getAttribute(pair[1]); }
        var v = labels[el.getAttribute(pair[0])];
        var s = v != null ? String(v) : el[store];
        if (s == null) { el.removeAttribute(pair[1]); }
        else if (el.getAttribute(pair[1]) !== s) { el.setAttribute(pair[1], s); }
      });
    });
  }

  /* ── Клон <template> с реестром полей ──
     Поле с пустым значением вынимается из DOM (как и раньше), но на его
     месте остаётся комментарий-якорь: при следующем обновлении ТОГО ЖЕ
     узла поле возвращается на место. Так элементы списков можно обновлять
     на месте, не пересоздавая (анимации появления не повторяются). ── */
  function makeClone(tpl) {
    var node = tpl.content.firstElementChild.cloneNode(true);
    node.__vpc = {
      fields: withRoot('[data-vpc-field]', node).map(function (el) {
        return { el: el, name: el.getAttribute('data-vpc-field'), anchor: null, root: el === node };
      }),
      optional: qsa('[data-vpc-optional]', node).map(function (el) {
        return { el: el, anchor: null, root: false };
      })
    };
    applyLabels(node);
    return node;
  }
  function detach(rec) {
    if (rec.root || !rec.el.parentNode) return;
    if (!rec.anchor) { rec.anchor = document.createComment('vpc'); }
    rec.el.parentNode.replaceChild(rec.anchor, rec.el);
  }
  function attach(rec) {
    if (rec.anchor && rec.anchor.parentNode) { rec.anchor.parentNode.replaceChild(rec.el, rec.anchor); }
  }

  // Значение одного поля: картинка, инпут, полоса прогресса или текст
  function setField(el, name, value) {
    if (name === 'image') {
      if (el.tagName === 'IMG') {
        if (el.getAttribute('src') !== value) { el.src = value; }
      } else {
        el.style.backgroundImage = 'url("' + String(value).replace(/"/g, '%22') + '")';
      }
      el.hidden = false;
      return;
    }
    // Поля-инпуты (напр. модель провайдера) получают value, остальные — текст;
    // поле в фокусе не трогаем — пользователь его правит
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      if (document.activeElement !== el) { el.value = String(value); }
      return;
    }
    // Элемент с data-vpc-bar — заполняемая полоса прогресса (значение = %)
    if (el.hasAttribute('data-vpc-bar')) {
      el.style.width = String(value) + '%';
      return;
    }
    var s = String(value);
    if (el.textContent !== s) { el.textContent = s; }
  }

  // Заполнение полей [data-vpc-field] клона (корень клона тоже может быть полем)
  function fillNode(node, data) {
    var reg = node.__vpc;
    reg.fields.forEach(function (f) {
      var value = data[f.name];
      if (value == null || value === '') { detach(f); return; }
      attach(f);
      setField(f.el, f.name, value);
    });
    // Опустевшие необязательные контейнеры (напр. цитата без содержимого)
    reg.optional.forEach(function (o) {
      if (o.el.querySelector('[data-vpc-field]')) { attach(o); } else { detach(o); }
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

  // ── Прокрутка ленты ──
  // Прокручивается ближайший предок с overflow auto/scroll (обычно сам
  // контейнер ленты), иначе — страница
  function scrollerOf(el) {
    for (var n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      var oy = getComputedStyle(n).overflowY;
      if (oy === 'auto' || oy === 'scroll' || oy === 'overlay') return n;
    }
    return document.scrollingElement || document.documentElement;
  }
  function distanceToBottom(s) { return s.scrollHeight - s.scrollTop - s.clientHeight; }
  var NEAR_BOTTOM = 40; // px: ближе — пользователь «у низа», ленту докручиваем

  /* ── Лента сообщений: сверка по id ──
     Узлы сообщений переиспользуются: новые дописываются, изменившиеся
     обновляются на месте, пропавшие удаляются. Чужие узлы в контейнере
     (вставленные JS скина) убираются, как и при полной перерисовке.
     Лента докручивается вниз, только если пользователь был у низа, при
     первом рендере / смене персоны или после его собственной отправки. */
  var feed = { box: null, scroller: null, nodes: {}, pinned: true };
  function msgKey(m) { return (m.role === 'user' ? 'u' : 'p') + '\n' + String(m.text == null ? '' : m.text); }
  var stickUntil = 0; // до этого момента — докручивать (своя отправка)

  function renderMessages(messages, fresh) {
    var box = qs('[data-vpc="messages"]');
    var tpl = qs('template[data-vpc="message"]');
    if (!box || !tpl || !tpl.content.firstElementChild) return [];
    if (feed.box !== box) {
      fresh = true;
      feed.box = box;
      // Картинка догрузилась и выросла высота — держим низ, если были у низа
      box.addEventListener('load', function () {
        if (feed.pinned && feed.scroller) { feed.scroller.scrollTop = feed.scroller.scrollHeight; }
      }, true);
    }
    if (fresh) { feed.nodes = {}; }
    var scroller = scrollerOf(box);
    feed.scroller = scroller;
    var stick = fresh || Date.now() < stickUntil || distanceToBottom(scroller) <= NEAR_BOTTOM;

    var prev = feed.nodes;
    // Лента персоны была пуста, а пришло сразу несколько сообщений — это
    // догрузилась история, а не новые реплики
    var hadNodes = Object.keys(prev).length > 0;
    var fromHistory = !hadNodes && messages.length > 1;
    var next = {};
    var added = [];
    var changed = false;
    var cursor = box.firstChild;
    messages.forEach(function (m) {
      var id = String(m.id);
      var rec = prev[id];
      if (!rec || next[id]) {
        rec = { node: makeClone(tpl), sig: null, key: '' };
        bindInner(rec.node); // кнопка «ответить» (data-vpc-item-action="reply") и т.п.
        if (!fresh && !fromHistory) { added.push(m); }
      }
      rec.key = msgKey(m);
      var s = sig(m);
      if (rec.sig !== s) {
        rec.sig = s;
        rec.node.setAttribute('data-role', m.role === 'user' ? 'user' : 'persona');
        rec.node.setAttribute('data-item-id', id);
        fillNode(rec.node, {
          text: m.text,
          time: m.time,
          image: m.image,
          'quote-author': m.quote && m.quote.author,
          'quote-text': m.quote && m.quote.text
        });
        changed = true;
      }
      next[id] = rec;
      if (rec.node === cursor) { cursor = cursor.nextSibling; }
      else { box.insertBefore(rec.node, cursor); changed = true; }
    });
    while (cursor) {
      var after = cursor.nextSibling;
      box.removeChild(cursor);
      cursor = after;
      changed = true;
    }
    feed.nodes = next;

    // Сообщение сменило id (локальная копия → реплика из перечитанной
    // истории): пропавший узел с тем же автором и текстом — это оно же,
    // событие 'message' не повторяем
    if (added.length) {
      var gone = {};
      Object.keys(prev).forEach(function (pid) {
        if (next[pid] !== prev[pid]) { gone[prev[pid].key] = (gone[prev[pid].key] || 0) + 1; }
      });
      added = added.filter(function (m) {
        var k = msgKey(m);
        if (gone[k]) { gone[k] -= 1; return false; }
        return true;
      });
    }

    if (stick && (changed || fresh)) { scroller.scrollTop = scroller.scrollHeight; }
    if (added.length) { stickUntil = 0; }
    feed.pinned = distanceToBottom(scroller) <= NEAR_BOTTOM;
    return added;
  }

  // Пользователь прокручивает ленту — запоминаем, у низа ли он
  document.addEventListener('scroll', function () {
    if (feed.scroller) { feed.pinned = distanceToBottom(feed.scroller) <= NEAR_BOTTOM; }
  }, true);

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
    // Данные списка не изменились — DOM не трогаем
    var s = sig(items);
    if (cfg.sig === s) return;
    cfg.sig = s;
    // Контейнеров с одним hook может быть несколько (напр. дела в сайдбаре и в досье);
    // шаблон ищем сначала в том же экране, что и контейнер, затем — глобально
    qsa(cfg.box).forEach(function (box) {
      var scope = box.closest('[data-vpc-screen]') || document;
      var tpl = qs('template[data-vpc="' + cfg.tpl + '"]', scope) || qs('template[data-vpc="' + cfg.tpl + '"]');
      if (!tpl || !tpl.content.firstElementChild) return;
      box.innerHTML = '';
      items.forEach(function (item) {
        var node = makeClone(tpl);
        if (item && item.id != null) { node.setAttribute('data-item-id', String(item.id)); }
        fillNode(node, cfg.map(item));
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
    'room-pet-label': function (p) { return p.room && p.room.petLabel; },
    // Окружение: локальное время и погода (если бэкенд её знает)
    'local-time': function (p) { return p.env && p.env.localTime; },
    'weather': function (p) { return p.env && p.env.weather && p.env.weather.text; },
    'weather-temp': function (p) {
      var w = p.env && p.env.weather;
      return w && w.tempC != null ? (w.tempC > 0 ? '+' : '') + w.tempC + '°C' : null;
    }
  };

  // Слоты окружения очищаются, когда данных больше нет (погоду выключили);
  // остальные при null сохраняют текст скина
  var ENV_SLOTS = { 'local-time': 1, 'weather': 1, 'weather-temp': 1 };

  function setText(el, value) {
    var s = String(value);
    if (el.textContent !== s) { el.textContent = s; }
  }
  // src картинки меняем, только если он другой (без повторного декодирования)
  function setImg(el, src) {
    if (src && el.tagName === 'IMG') {
      if (el.getAttribute('src') !== src) { el.src = src; }
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  }

  // Окружение: тема, язык, время суток, погода, подписи UI
  function applyEnv(env) {
    if (!env || typeof env !== 'object') return;
    var html = document.documentElement;
    if (env.theme === 'light' || env.theme === 'dark') { html.setAttribute('data-theme', env.theme); }
    if (env.locale) { html.setAttribute('lang', String(env.locale)); }
    if (env.timeOfDay) { html.setAttribute('data-vpc-time-of-day', String(env.timeOfDay)); }
    if (env.weather && env.weather.condition) { html.setAttribute('data-vpc-weather', String(env.weather.condition)); }
    else { html.removeAttribute('data-vpc-weather'); }
    var ls = sig(env.labels || {});
    if (ls !== labelsSig) {
      labelsSig = ls;
      labels = env.labels && typeof env.labels === 'object' ? env.labels : {};
      applyLabels(null);
    }
  }

  // ── JS API скина: window.vpc ──
  var listeners = {};
  var lastState = null;
  function emit(ev, detail) {
    var fns = listeners[ev];
    if (!fns || !fns.length) return;
    fns.slice().forEach(function (fn) {
      try { fn(detail, lastState); }
      catch (e) {
        // Ошибка обработчика скина — такая же, как необработанная в его JS
        post({ vpc: 'skin', type: 'error', message: 'vpc.on(' + ev + '): ' + String((e && e.message) || e) });
      }
    });
  }
  function moodOf(p) {
    return (p.persona && p.persona.mood) || (p.room && p.room.mood) || (p.context && p.context.mood) || null;
  }

  function applyState(p) {
    if (!p || typeof p !== 'object') return;
    var prev = lastState;
    lastState = p;
    var personaId = p.persona ? p.persona.id : null;
    var personaChanged = !prev || (prev.persona ? prev.persona.id : null) !== personaId;

    applyEnv(p.env);

    Object.keys(TEXT_SLOTS).forEach(function (slot) {
      var value = TEXT_SLOTS[slot](p);
      if (value == null) {
        if (!ENV_SLOTS[slot]) return;
        value = '';
      }
      qsa('[data-vpc="' + slot + '"]').forEach(function (el) { setText(el, value); });
    });

    // Аватар: <img> получает src, иначе — первая буква имени
    qsa('[data-vpc="persona-avatar"]').forEach(function (el) {
      if (el.tagName === 'IMG') {
        setImg(el, p.persona && p.persona.avatar);
      } else {
        setText(el, p.persona && p.persona.name ? p.persona.name.charAt(0) : '?');
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

    var added = p.messages ? renderMessages(p.messages, personaChanged) : [];
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
      qsa('[data-vpc="room-bg"]').forEach(function (el) { setImg(el, p.room.bg); });
      qsa('[data-vpc="room-sprite"]').forEach(function (el) { setImg(el, p.room.sprite); });
      qsa('[data-vpc="room-pet"]').forEach(function (el) {
        if (p.room.pet && p.room.pet !== 'none') {
          el.setAttribute('data-pet', p.room.pet);
          el.hidden = false;
        } else { el.hidden = true; }
      });
    }

    // Декоративный JS скина может реагировать на это событие
    try { document.dispatchEvent(new CustomEvent('vpc:state', { detail: p })); } catch (e) { /* старый движок */ }

    // События window.vpc: первый снапшот тоже считается изменением;
    // 'message' — только сообщения, появившиеся после первого рендера персоны
    emit('state', p);
    if (personaChanged) { emit('persona', p.persona || null); }
    if (!prev || !!prev.typing !== !!p.typing) { emit('typing', !!p.typing); }
    var mood = moodOf(p);
    if (!prev || moodOf(prev) !== mood) { emit('mood', mood); }
    var theme = p.env && p.env.theme;
    if (theme && (!prev || !prev.env || prev.env.theme !== theme)) { emit('theme', theme); }
    added.forEach(function (m) { emit('message', m); });
  }

  // ── События скина → хост ──
  // Картинка больше лимита хоста ужимается холстом (до 2048 px, JPEG);
  // done(null) — ужать не вышло
  function fitImage(src, done) {
    if (src.length <= IMAGE_MAX_CHARS) { done(src); return; }
    var img = new Image();
    img.onload = function () {
      try {
        var w = img.naturalWidth || 1;
        var h = img.naturalHeight || 1;
        var k = Math.min(1, 2048 / Math.max(w, h));
        var c = document.createElement('canvas');
        c.width = Math.max(1, Math.round(w * k));
        c.height = Math.max(1, Math.round(h * k));
        var ctx = c.getContext('2d');
        ctx.fillStyle = '#fff'; // у JPEG нет прозрачности
        ctx.fillRect(0, 0, c.width, c.height);
        ctx.drawImage(img, 0, 0, c.width, c.height);
        var q = 0.9;
        var out = c.toDataURL('image/jpeg', q);
        while (out.length > IMAGE_MAX_CHARS && q > 0.4) {
          q -= 0.2;
          out = c.toDataURL('image/jpeg', q);
        }
        done(out.length <= IMAGE_MAX_CHARS ? out : null);
      } catch (e) { done(null); }
    };
    img.onerror = function () { done(null); };
    img.src = src;
  }

  /* Отправка сообщения (поле ввода скина или vpc.send). Хост отвечает
     {vpc:'host', type:'send-result', sid, ok}; при отказе (лимит частоты,
     картинка не подошла) restore(text, image) возвращает текст и картинку
     в поле ввода. После своей отправки лента докручивается вниз, даже
     если пользователь листал историю */
  var sendSeq = 0;
  var pendingSends = {};
  function sendMessage(text, image, restore) {
    if (!text && !image) return false;
    var sid = ++sendSeq;
    stickUntil = Date.now() + 3000;
    function go(img) {
      if (image && !img) {
        if (restore) { restore(text, null); }
        return;
      }
      var msg = { vpc: 'skin', type: 'send', text: text, sid: sid };
      if (img) { msg.image = img; }
      pendingSends[sid] = { text: text, image: img, restore: restore || null };
      post(msg);
    }
    if (image) { fitImage(String(image), go); } else { go(null); }
    return true;
  }
  function onSendResult(d) {
    var rec = pendingSends[d.sid];
    if (!rec) return;
    delete pendingSends[d.sid];
    if (d.ok === false && rec.restore) { rec.restore(rec.text, rec.image); }
  }

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
          reader.onload = function () {
            fitImage(String(reader.result), function (img) {
              if (img) { pendingImage = img; showAttach(); }
            });
          };
          reader.readAsDataURL(f);
        };
        fi.click();
      });
    }
    qsa('[data-vpc="cancel-attach"]').forEach(function (el) {
      el.addEventListener('click', function () { pendingImage = null; showAttach(); });
    });

    // Хост отказал в отправке — вернуть текст и картинку, если пользователь
    // ещё не начал новое сообщение
    function restoreInput(text, image) {
      if (input && !String(input.value || '').trim()) { input.value = text; }
      if (image && !pendingImage) {
        pendingImage = image;
        showAttach();
      }
    }
    function send() {
      if (!input) return;
      if (!sendMessage(String(input.value || '').trim(), pendingImage, restoreInput)) return;
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

  /* Программный API для JS скина. Bridge стоит в конце <body>, поэтому
     скрипт скина, выполненный раньше, берёт window.vpc в обработчике
     DOMContentLoaded или события document 'vpc:ready' (detail — сам API). */
  var api = {
    contract: APP_CONTRACT, // версия контракта, которую понимает приложение
    skinContract: SKIN_CONTRACT, // версия из <meta name="vpc-skin-contract"> (нет меты — 1)
    on: function (ev, fn) {
      if (typeof fn !== 'function') { return function () {}; }
      var key = String(ev);
      (listeners[key] = listeners[key] || []).push(fn);
      return function () {
        var arr = listeners[key];
        var i = arr ? arr.indexOf(fn) : -1;
        if (i >= 0) { arr.splice(i, 1); }
      };
    },
    send: function (text, image) {
      return sendMessage(String(text == null ? '' : text).trim(), image || null, null);
    },
    action: function (name, values, id) {
      var vals = {};
      if (values && typeof values === 'object') {
        Object.keys(values).forEach(function (k) { vals[k] = String(values[k]); });
      }
      post({ vpc: 'skin', type: 'action', action: String(name), id: id == null ? null : String(id), values: vals });
    },
    setSetting: function (key, value) {
      post({ vpc: 'skin', type: 'set-setting', key: String(key), value: String(value) });
    },
    selectPersona: function (id) { post({ vpc: 'skin', type: 'select-persona', id: String(id) }); },
    openDossier: function () { post({ vpc: 'skin', type: 'open-dossier' }); },
    closeDossier: function () { post({ vpc: 'skin', type: 'close-dossier' }); },
    zoom: function (src) { post({ vpc: 'skin', type: 'zoom-image', src: String(src) }); }
  };
  Object.defineProperty(api, 'state', { get: function () { return lastState; }, enumerable: true });
  try {
    Object.defineProperty(window, 'vpc', { value: Object.freeze(api), writable: false, configurable: false });
  } catch (e) { window.vpc = api; }
  try { document.dispatchEvent(new CustomEvent('vpc:ready', { detail: api })); } catch (e) { /* старый движок */ }

  /* Горячие клавиши приложения работают и при фокусе внутри скина:
     Escape и сочетания с Ctrl/Meta/Alt пересылаются хосту. Обычный набор
     текста не уходит; в полях ввода не уходят и стандартные сочетания
     правки (Ctrl/Cmd + A/C/V/X/Z/Y, стрелки…) и Alt-символы macOS.
     Скин может оставить клавишу себе через preventDefault(). */
  var EDIT_KEYS = { a: 1, c: 1, v: 1, x: 1, y: 1, z: 1 };
  window.addEventListener('keydown', function (e) {
    if (e.defaultPrevented || e.isComposing) return;
    var k = e.key;
    if (!k || k === 'Control' || k === 'Meta' || k === 'Alt' || k === 'Shift') return;
    if (k !== 'Escape' && !e.ctrlKey && !e.metaKey && !e.altKey) return;
    var t = e.target;
    var editable = t && (t.isContentEditable || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT');
    if (editable && k !== 'Escape') {
      if (!e.ctrlKey && !e.metaKey) return;
      if (EDIT_KEYS[k.toLowerCase()] || /^(Arrow|Home|End|Backspace|Delete)/.test(k)) return;
    }
    post({
      vpc: 'skin', type: 'key', key: k, code: e.code,
      ctrlKey: e.ctrlKey, metaKey: e.metaKey, altKey: e.altKey, shiftKey: e.shiftKey
    });
  });

  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.vpc !== 'host') return;
    if (d.type === 'state') { applyState(d.payload); }
    else if (d.type === 'send-result') { onSendResult(d); }
  });

  /* Ссылки не уводят документ скина: переход iframe на чужую страницу
     хост считает поломкой скина и отключает его. Якоря (#…) работают */
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (a && String(a.getAttribute('href') || '').charAt(0) !== '#') { e.preventDefault(); }
  }, true);

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
