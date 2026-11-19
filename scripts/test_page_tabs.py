"""Вкладки длинных страниц на телефоне (web/src/components/PageTabs.tsx).

Статические проверки: страницы настроек обёрнуты в PageTabs, у вкладок есть
подписи в обоих словарях, строка вкладок у досье и страниц — один хук,
на широком экране всё по-прежнему подряд.

Запуск: PYTHONPATH=. python -m scripts.test_page_tabs
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "web" / "src"

ok = 0
fail = 0


def check(name: str, cond: bool) -> None:
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"FAIL: {name}")


def read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


tabs = read("components/PageTabs.tsx")
strip = read("components/useTabStrip.ts")
dossier = read("components/PersonaDossier.tsx")
mobile = read("mobile.css")
ru = read("i18n/ru.ts")
en = read("i18n/en.ts")

# Компонент: узкий экран — строка и одна вкладка; широкий — всё подряд
check("узкий экран — useNarrowScreen", "useNarrowScreen()" in tabs)
check("широкий экран (без always) и одна вкладка — без строки", "if ((!narrow && !always) || list.length < 2)" in tabs)
check("скрытые вкладки остаются в дереве (hidden)", "hidden={x.id !== active}" in tabs)
check("вкладка запоминается для страницы", "vpc-page-tab:${page}" in tabs and "localStorage.setItem(key, id)" in tabs)
check("чтение запомненной — в try", re.search(r"try \{\s*return localStorage\.getItem", tabs) is not None)
check("неизвестная вкладка → первая", "list.find((x) => x.id === picked)?.id ?? list[0]?.id" in tabs)
check("смена вкладки ниже строки — к началу раздела", "scroller.scrollTop += d" in tabs)
check("роли вкладок", 'role="tablist"' in tabs and 'role="tab"' in tabs and 'role="tabpanel"' in tabs)

# Один хук строки вкладок у досье и страниц
check("PageTabs — useTabStrip", "useTabStrip(active, list.length)" in tabs)
check("досье — useTabStrip", "useTabStrip(tab, tabs.length)" in dossier and "tabsEdge" not in dossier)
check("хук — классы досье", "tabs dossier-tabs" in strip and "dossier-tabs--more-right" in strip)

# Стили: строка липкая, фон на обёртке; линия шапки страницы с вкладками убрана
m = re.search(r"\.page-tabs \{([^}]*)\}", mobile)
check("стиль .page-tabs есть", m is not None)
if m:
    body = m.group(1)
    check(".page-tabs — sticky сверху", "position: sticky" in body and "top: 0" in body)
    check(".page-tabs — свой фон", "background: var(--bg)" in body)
check("шапка страницы с вкладками без линии", ".section:has(> .page-tabs-host) > .section-header" in mobile)
check("главная: шаг блоков во вкладке", ".home .page-tab-panel {" in mobile)
check("строка и разделы — одна обёртка", "className={`page-tabs-host" in tabs)
media = mobile.find("@media (max-width: 760px)")
check("стили вкладок — только узкий экран", 0 <= media < mobile.find(".page-tabs {"))

# Страницы
PAGES = {
    "sections/ApiKeys.tsx": ("settings", ["offline", "commands", "screen", "general", "place", "keys", "economy", "webchats"]),
    "sections/Providers.tsx": ("providers", ["keys", "webchats", "gemma"]),
    "sections/Services.tsx": ("services", ["overview", "notify", "connections", "rules", "feed"]),
    "offline/PhoneServicesView.tsx": ("services", ["overview", "notify", "connections", "rules", "feed"]),
    "sections/Connection.tsx": ("connection", ["core", "network", "background"]),
    "sections/Skins.tsx": ("skins", ["themes", "skins"]),
    "sections/Home.tsx": ("home", ["status", "events", "notes", "calendar"]),
}
for rel, (page, ids) in PAGES.items():
    src = read(rel)
    check(f"{rel}: импорт PageTabs", "import PageTabs from '../components/PageTabs';" in src)
    check(f"{rel}: page=\"{page}\"", f'page="{page}"' in src)
    for i in ids:
        check(f"{rel}: вкладка {i}", f"id: '{i}'" in src)

# Подписи вкладок — в обоих словарях
used = set()
for rel in PAGES:
    used |= set(re.findall(r"t\('(pageTabs\.[\w.]+)'\)", read(rel)))
    used |= set(re.findall(r"'(pageTabs\.[\w.]+)'", read(rel)))
check("подписи вкладок нашлись", len(used) >= 10)
for key in sorted(used):
    check(f"ru: {key}", f"'{key}':" in ru)
    check(f"en: {key}", f"'{key}':" in en)

# «Настройки»: вкладки телефона — только в APK, «Место» — когда есть что показать
api = read("sections/ApiKeys.tsx")
for i in ("offline", "commands", "screen"):
    check(f"настройки: {i} — только в APK", re.search(r"isNativeApp\(\) && \{\s*id: '" + i + "'", api) is not None)
check(
    "настройки: место — офлайн-телефон или ядро",
    "((isNativeApp() && phoneOffline) || (apiOnline && !phoneOffline)) && {" in api,
)

print(f"Итого: {ok} OK, {fail} FAIL")
raise SystemExit(1 if fail else 0)
