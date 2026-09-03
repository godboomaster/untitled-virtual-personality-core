"""Живой прогон «пицца а-ля Болоньезе» — добавки «Добавить по вкусу»:
открыть продукт → «сыры чеддер и пармезан в добавить по вкусу» → дамп
состояния добавки (выбрана ли). Без LLM."""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

URL = ("https://dodopizza.ru/novosibirsk/geodezicheskaya41/product/"
       "picca-a-lya-boloneze?variation=11f176c0a8b8bf0d10ef8e88479dac30")


def dump_topping(ba, tid, tag):
    out = ba.eval_js(None, tid, """(function(){
var els=document.querySelectorAll('label,button'),out=[];
for(var i=0;i<els.length;i++){var e=els[i];
var t=(e.innerText||'').replace(/\\s+/g,' ').trim();
if(t.toLowerCase().indexOf('чеддер')<0&&t.toLowerCase().indexOf('пармезан')<0)continue;
var r=e.getBoundingClientRect();
var inp=e.tagName==='LABEL'?e.querySelector('input'):null;
out.push(e.tagName+' "'+t.slice(0,45)+'" x='+Math.round(r.x)+' y='+Math.round(r.y)
+' vis='+(r.width>5&&r.height>5)+(inp?' [inp:'+inp.type+':'+inp.checked+']':'')
+' cls='+String(e.className).slice(0,50));}
return out.join('\\n')||'добавка не найдена в DOM';
})()""")
    print(f"── состояние добавки ({tag}):\n{out}")


def main():
    from app.features import browser_actions as ba
    from app.features.computer_control import ComputerControlManager

    # Уникальный маркер в URL: дублей вкладок страницы много, целимся точно
    marker = f"dbg={int(time.time())}"
    mgr = ComputerControlManager(context="dbgbol", config={"click": True},
                                 base_dir=Path(tempfile.mkdtemp(prefix="vpc_bol_")))
    tid = ba.open_new_tab(URL + "&" + marker)
    mgr._last_tab_id = tid
    time.sleep(4)

    def total():
        return ba.eval_js(None, tid, "(function(){var m=document.body.innerText"
                          ".match(/В корзину за\\s*([\\d\\s]+)\\s*₽/);"
                          "return m?m[1].replace(/\\s/g,''):'?';})()")

    dump_topping(ba, tid, "до")
    print("total до:", total())

    goal = "сыры чеддер и пармезан в добавить по вкусу"
    act, err = mgr.resolve_click(goal, None, None, chat_id="dbg")
    if act is None:
        print(f"\n✗ «{goal}» — отказ: {err}")
        return
    ch = act.get("choose") or {}
    print(f"\n→ «{goal}» = idx {act['idx']} «{str(act.get('element'))[:45]}» "
          f"путь={ch.get('path')} llm={str(ch.get('llm_response'))[:30]!r}")
    for c in (ch.get("candidates") or [])[:5]:
        print(f"    кандидат [{c['idx']}] {c['score']:6.1f} {str(c.get('text'))[:50]}")
    ok, detail = mgr.execute(act, "dbg", router=None)
    print(f"  execute: {'OK' if ok else 'FAIL'} {detail[:100]}")
    time.sleep(1.0)
    dump_topping(ba, tid, "после")
    print("total после:", total())


if __name__ == "__main__":
    main()
