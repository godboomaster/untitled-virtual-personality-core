"""Живой прогон сценария «2 соуса» на dodo одним менеджером (один процесс —
один реестр вкладок): открыть продукт → «заменить барбекю» → дамп модалки →
«сырный в части слева» → дамп. Без LLM (--no-router эквивалент)."""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def dump_modal(ba, tid, tag):
    out = ba.eval_js(None, tid, """(function(){
var d=document.querySelector('[role=dialog]');
if(!d){var all=document.querySelectorAll('div');
for(var i=0;i<all.length;i++){var e=all[i];var r=e.getBoundingClientRect();
if(r.width>300&&r.width<900&&r.height>400){var cs=getComputedStyle(e);
if(cs.position==='fixed'||cs.position==='absolute'){d=e;break;}}}}
if(!d)return 'NO MODAL';
var vw=window.innerWidth;
var els=d.querySelectorAll('button,a,[role=button],label'),out=[];
for(var i=0;i<els.length;i++){var e=els[i];var r=e.getBoundingClientRect();
if(r.width<5||r.height<5)continue;
var t=(e.innerText||e.getAttribute('aria-label')||'')
.replace(/\\s+/g,' ').trim().slice(0,26);
var inp=e.tagName==='LABEL'?e.querySelector('input'):null;
out.push(e.tagName+' "'+t+'" x='+Math.round(r.x)+' w='+Math.round(r.width)
+(r.x+r.width/2<vw/2?' L':' R')+(inp?' [inp:'+inp.type+' '+inp.checked+']':''));}
return out.join('\\n');
})()""")
    print(f"\n── модалка ({tag}) ──")
    print(str(out)[:1800])


def main():
    from app.features import browser_actions as ba
    from app.features.computer_control import ComputerControlManager

    mgr = ComputerControlManager(context="dbg2s", config={"click": True},
                                 base_dir=Path(tempfile.mkdtemp(prefix="vpc_2s_")))
    tid = ba.open_new_tab(
        "https://dodopizza.ru/novosibirsk/geodezicheskaya41/product/2-sousa")
    mgr._last_tab_id = tid
    time.sleep(3.5)
    dump_modal(ba, tid, "открыта")

    for goal in ("заменить барбекю", "сырный в части слева"):
        act, err = mgr.resolve_click(goal, None, None, chat_id="dbg")
        if act is None:
            print(f"\n✗ «{goal}» — отказ: {err}")
            break
        ch = act.get("choose") or {}
        print(f"\n→ «{goal}» = idx {act['idx']} «{str(act.get('element'))[:40]}» "
              f"путь={ch.get('path')} llm={str(ch.get('llm_response'))[:30]!r}")
        for c in (ch.get("candidates") or [])[:4]:
            print(f"    кандидат [{c['idx']}] {c['score']:6.1f} "
                  f"{str(c.get('text'))[:45]}")
        ok, detail = mgr.execute(act, "dbg", router=None)
        print(f"  execute: {'OK' if ok else 'FAIL'} {detail[:80]}")
        time.sleep(1.2)
        dump_modal(ba, tid, f"после «{goal}»")


if __name__ == "__main__":
    main()
