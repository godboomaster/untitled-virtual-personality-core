"""Диагностика kimi, шаг 22: список чатов сайдбара + содержимое свежего чата."""
import logging
import time

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.WARNING)

from app.features import browser_actions as ba
from app.features.browser_actions import _RAW_TABS, _raw_call, _pool_of_tab

tab = ba.open_new_tab("https://www.kimi.ai/", background=True, pool="h")
time.sleep(7)

def raw(js, label, limit=1500):
    t = _RAW_TABS[tab]
    try:
        res = _raw_call("Runtime.evaluate",
                        {"expression": js, "returnByValue": True,
                         "awaitPromise": True},
                        session_id=t["sessionId"], pool=_pool_of_tab(t))
    except Exception as e:
        print(f"{label}: EXC {str(e)[:100]}")
        return None
    exc = res.get("exceptionDetails")
    if exc:
        print(f"{label}: JS-EXC {str(exc.get('text'))[:100]}")
        return None
    val = str((res.get("result") or {}).get("value") or "")
    print(f"{label}: {val[:limit]}")
    return val

# Все ссылки сайдбара на чаты
links = raw("""
JSON.stringify([...document.querySelectorAll('a[href*="/chat/"]')]
  .map(a => ({href: a.href, txt: (a.innerText||'').replace(/\\n/g,' ').slice(0,50)})))
""", "CHAT LINKS", 2000)

# Откроем первый (свежий) чат из списка и прочитаем целиком
if links:
    import json as _json
    chats = _json.loads(links)
    if chats:
        href = chats[0]["href"]
        print("Открываю:", href)
        ba.navigate_tab(href, tab_id=tab)
        time.sleep(6)
        print("URL:", ba.tab_url(tab_id=tab))
        raw("(document.querySelector('.chat-box')||document.body).innerText.slice(0,1500)",
            "CHAT CONTENT", 1600)
