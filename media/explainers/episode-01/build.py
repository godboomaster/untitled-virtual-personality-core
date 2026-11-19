#!/usr/bin/env python3
"""A fully procedural explainer: Pillow artwork, audio-driven character, NumPy music.

python3 build.py --preview  # storyboard + poster + audio + subtitles
python3 build.py            # also render the 1920x1080 MP4 with FFmpeg
Requires Python 3.11+, Pillow, NumPy, ffmpeg; uses the included speech WAVs.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import json
import math
from pathlib import Path
import shutil
import subprocess
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
W, H, FPS, SR = 1920, 1080, 30, 24000
BG = "#F7F5EF"
INK = "#243331"
MUTED = "#6A7671"
MINT = "#C7F0DC"
LILAC = "#DBD2F5"
YELLOW = "#F6DB78"
CORAL = "#F5B5A4"
WHITE = "#FFFFFF"
OUT = ROOT / "output"
SCRIPT = json.loads((ROOT / "script.json").read_text())


def find_font(bold=False):
    choices = [
        "/System/Library/Fonts/Avenir Next.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in choices:
        if Path(path).exists():
            return path, 0 if bold else (7 if path.endswith(".ttc") else 0)
    raise RuntimeError("Install Avenir Next or DejaVu Sans for Cyrillic text.")


@lru_cache(maxsize=128)
def font(size, bold=False):
    path, index = find_font(bold)
    return ImageFont.truetype(path, round(size), index=index)


def text(draw, xy, value, size=32, fill=INK, bold=False, anchor="lt", spacing=8):
    for i, row in enumerate(value.split("\n")):
        draw.text((xy[0], xy[1]+i*(round(size*1.12)+spacing)), row,
                  font=font(size, bold), fill=fill, anchor=anchor)


def rr(draw, box, fill=WHITE, radius=28, outline=None, width=2):
    draw.rounded_rectangle(tuple(round(v) for v in box), radius=radius,
                           fill=fill, outline=outline, width=width)


def line(draw, points, fill=INK, width=5):
    draw.line(points, fill=fill, width=width, joint="curve")
    r = width / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse((x-r, y-r, x+r, y+r), fill=fill)


def ease(v):
    v = min(1, max(0, v))
    return 1-(1-v)**3


def star(draw, x, y, r=20, color=YELLOW, angle=0):
    points = []
    for i in range(8):
        a = angle + math.pi*i/4
        radius = r if i % 2 == 0 else r*.3
        points.append((x+math.cos(a)*radius, y+math.sin(a)*radius))
    draw.polygon(points, fill=color)


def pill(draw, xy, value, fill=MINT, size=25, color=INK, padding=22):
    x, y = xy
    width = draw.textlength(value, font=font(size, True)) + padding*2
    height = size+26
    rr(draw, (x, y, x+width, y+height), fill, height//2)
    text(draw, (x+padding, y+12), value, size, color, True)
    return width


def icon(draw, kind, x, y, s=72, color=INK):
    """Small line icons with no external art, emoji or font dependency."""
    u = s/80
    def p(a, b): return (x+a*u, y+b*u)
    def ln(coords, w=4): line(draw, [p(a, b) for a, b in coords], color, max(2,round(w*u)))
    if kind == "book":
        for coords in [[(40,20),(12,12),(12,63),(40,70)],[(40,20),(68,12),(68,63),(40,70)]]:
            draw.polygon([p(*q) for q in coords], fill=WHITE, outline=color, width=max(2,round(4*u)))
        ln([(40,20),(40,70)])
        ln([(20,28),(31,31)],3); ln([(49,31),(60,28)],3)
    elif kind == "smile":
        draw.ellipse((*p(10,10),*p(70,70)), outline=color, width=max(2,round(4*u)))
        for a in (29,51): draw.ellipse((*p(a-3,28),*p(a+3,36)), fill=color)
        draw.arc((*p(24,27),*p(56,58)), 0, 180, fill=color, width=max(2,round(4*u)))
    elif kind == "sliders":
        for a, b in [(19,29),(40,51),(61,31)]:
            ln([(a,12),(a,68)])
            draw.ellipse((*p(a-7,b-7),*p(a+7,b+7)), fill=WHITE, outline=color, width=max(2,round(3*u)))
    elif kind == "bell":
        draw.arc((*p(18,12),*p(62,63)),180,360,fill=color,width=max(2,round(4*u)))
        ln([(18,37),(18,54),(12,61),(68,61),(62,54),(62,37)])
        draw.arc((*p(32,58),*p(48,73)),0,180,fill=color,width=max(2,round(4*u)))
        ln([(40,7),(40,13)])
    elif kind == "note":
        rr(draw, (*p(17,9),*p(64,72)),WHITE,round(5*u),color,round(4*u))
        for b, right in [(27,54),(41,54),(55,45)]: ln([(27,b),(right,b)],3)
    elif kind == "browser":
        rr(draw, (*p(6,12),*p(74,67)),WHITE,round(6*u),color,round(4*u))
        ln([(7,27),(73,27)],3)
        for a in (16,24,32): draw.ellipse((*p(a-1.7,19-1.7),*p(a+1.7,19+1.7)),fill=color)
        ln([(28,40),(20,47),(28,54)],3); ln([(52,40),(60,47),(52,54)],3)
    elif kind == "plane":
        draw.polygon([p(7,36),p(73,10),p(59,71),p(40,53),p(28,65),p(30,45)],fill=WHITE,outline=color,width=max(2,round(3*u)))
        ln([(30,45),(60,24),(40,53)],3)
    elif kind == "phone":
        rr(draw,(*p(20,5),*p(60,75)),WHITE,round(8*u),color,round(4*u))
        ln([(33,12),(47,12)],3); ln([(34,67),(46,67)],3)
        draw.ellipse((*p(31,30),*p(49,48)),fill=MINT)
    elif kind == "check":
        ln([(15,42),(32,59),(65,23)],6)
    elif kind == "brain":
        for a,b in [(28,25),(49,25),(25,43),(53,43),(31,57),(48,57)]:
            draw.ellipse((*p(a-13,b-13),*p(a+13,b+13)),fill=WHITE,outline=color,width=max(2,round(3*u)))
        ln([(40,14),(40,66)],3)
    elif kind == "mic":
        rr(draw,(*p(29,7),*p(51,47)),WHITE,round(11*u),color,round(4*u))
        draw.arc((*p(19,20),*p(61,59)),0,180,fill=color,width=max(2,round(4*u)))
        ln([(40,59),(40,72)],4); ln([(28,72),(52,72)],4)


def bezier(points, count=32):
    a,b,c,d = [np.array(p,dtype=float) for p in points]
    return [tuple((1-t)**3*a+3*(1-t)**2*t*b+3*(1-t)*t*t*c+t**3*d)
            for t in np.linspace(0,1,count)]


@lru_cache(maxsize=64)
def ghost(mouth=0, blink=False, hand=0):
    """Render the mascot at 2x; mouth follows the real narration envelope."""
    q=2
    im=Image.new("RGBA",(500*q,560*q))
    d=ImageDraw.Draw(im)
    def ellipse(box, fill, outline=None, width=1):
        d.ellipse(tuple(v*q for v in box),fill=fill,outline=outline,width=width*q)
    def stroke(points, fill=INK, width=5):
        line(d,[(a*q,b*q) for a,b in points],fill,width*q)
    # Arms: the right hand waves, the left points toward the explanation.
    h = [0,-13,-23,-13][hand]
    ellipse((35,258,151,317),WHITE,INK,5)
    ellipse((353,211+h,463,273+h),WHITE,INK,5)
    points=[]
    curves=[
        [(106,429),(115,351),(99,261),(117,179)],
        [(117,179),(136,85),(208,42),(273,60)],
        [(273,60),(353,67),(391,126),(392,215)],
        [(392,215),(389,293),(405,379),(418,436)],
        [(418,436),(394,414),(380,414),(360,444)],
        [(360,444),(340,421),(325,422),(305,449)],
        [(305,449),(286,424),(269,425),(249,452)],
        [(249,452),(226,428),(210,428),(190,450)],
        [(190,450),(169,428),(150,426),(132,447)],
        [(132,447),(122,438),(113,431),(106,429)],
    ]
    for c in curves: points.extend(bezier(c,20))
    pts=[(round(a*q),round(b*q)) for a,b in points]
    mask=Image.new("L",im.size)
    ImageDraw.Draw(mask).polygon(pts,fill=255)
    # Subtle mint tint along the lower half gives the ghost a soft paper feel.
    grad=np.zeros((560*q,500*q,4),dtype=np.uint8)
    y=np.linspace(0,1,560*q)[:,None]
    amount=np.clip((y-.37)*1.3,0,.75)
    for k,(a,b) in enumerate(zip((255,255,253),(198,239,219))):
        grad[:,:,k]=(a*(1-amount)+b*amount).astype(np.uint8)
    grad[:,:,3]=255
    im.paste(Image.fromarray(grad),(0,0),mask)
    d=ImageDraw.Draw(im)
    d.line(pts+[pts[0]],fill=INK,width=6*q,joint="curve")
    # Forehead reflection, cheeks, eyes, and an amiable little smile.
    stroke(bezier([(152,168),(158,130),(186,106),(210,100)],16),"#FFFFFF",11)
    ellipse((166,253,202,273),"#F4B5A9")
    ellipse((310,253,346,273),"#F4B5A9")
    if blink:
        stroke([(198,232),(221,232)],INK,6)
        stroke([(287,232),(310,232)],INK,6)
    else:
        for x in (198,287):
            ellipse((x,207,x+24,241),INK)
            ellipse((x+6,212,x+12,220),WHITE)
    if mouth:
        hh=[0,10,17,25,32][mouth]
        ellipse((239,253,274,253+hh),INK)
        if mouth>2: ellipse((245,253+hh-11,269,253+hh-3),CORAL)
    else:
        d.arc((237*q,242*q,276*q,275*q),0,180,fill=INK,width=4*q)
    # A violet bandana and a star are the recurring visual signature.
    scarf=[(321,318),(366,332),(347,359),(306,339)]
    d.polygon([(x*q,y*q) for x,y in scarf],fill=LILAC,outline=INK,width=3*q)
    d.polygon([(344*q,352*q),(365*q,349*q),(371*q,382*q),(344*q,370*q)],fill=LILAC,outline=INK,width=3*q)
    star(d,330*q,337*q,13*q,YELLOW,math.pi/4)
    return im.resize((500,560),Image.Resampling.LANCZOS)


def put_ghost(im,t,volume,scene=0,entry=1):
    x=1463
    y=544+math.sin(t*2.7)*14
    scale=1.27 if scene in (0,7) else 1.14
    scale*=.88+.12*entry
    blink=(t%4.7>4.52)
    mouth=0 if volume<.018 else min(4,1+round(volume*11))
    sprite=ghost(mouth,blink,int((t*3)%4))
    sprite=sprite.resize((round(500*scale),round(560*scale)),Image.Resampling.BICUBIC)
    sprite=sprite.rotate(math.sin(t*1.7)*2.8,resample=Image.Resampling.BICUBIC,expand=True)
    d=ImageDraw.Draw(im)
    # Grounding shadow is deliberately separate from the floating body.
    sw=142+math.sin(t*2.7)*8
    d.ellipse((x-sw,823,x+sw,845),fill="#E1E5DA")
    im.paste(sprite,(round(x-sprite.width/2),round(y-sprite.height/2)),sprite)


@lru_cache(maxsize=1)
def background():
    im=Image.new("RGB",(W,H),BG)
    d=ImageDraw.Draw(im)
    # Quiet dot grid, a pale disc behind Boo, and generous margins.
    for x in range(83,W,36):
        for y in range(154,880,36): d.ellipse((x,y,x+2,y+2),fill="#E3E4DA")
    d.ellipse((1125,193,1810,866),fill="#E6EEDA")
    d.ellipse((1186,256,1750,812),fill="#EEF2E4")
    line(d,[(90,104),(1830,104)],"#DDDCD4",2)
    text(d,(91,40),"Virtual Persona Core",30,bold=True)
    pill(d,(465,36),"БУ ОБЪЯСНЯЕТ",LILAC,18,padding=17)
    return im


def title(d,eyebrow,heading,size=83):
    text(d,(100,154),eyebrow,22,MUTED,True)
    text(d,(94,211),heading,size,bold=True,spacing=0)


def card(draw,x,y,w,h,fill=WHITE):
    rr(draw,(x+1,y+7,x+w+1,y+h+7),"#E8E7DF",27)
    rr(draw,(x,y,x+w,y+h),fill,27,"#D8DDD3",2)


def reveal_layer(im,layer,p,dx=0,dy=26):
    if p<=0:return
    p=ease(p)
    if p<.999:
        layer=layer.copy()
        layer.putalpha(layer.getchannel("A").point(lambda a:round(a*p)))
    im.paste(layer,(round(dx*(1-p)),round(dy*(1-p))),layer)


@lru_cache(maxsize=64)
def art(scene,part=0):
    im=Image.new("RGBA",(W,H));d=ImageDraw.Draw(im)
    if scene==0:
        if part==0:
            title(d,"01 / КОРОТКОЕ ЗНАКОМСТВО","Персонаж,\nкоторый рядом.",94)
            text(d,(100,456),"Общается · помнит · помогает",34,MUTED)
            pill(d,(100,550),"Твой цифровой напарник",MINT,28)
            text(d,(100,665),"Всё начинается с обычного «привет».",29)
        else:
            pill(d,(1310,201),"Буу! Я Бу.",YELLOW,32)
            text(d,(1286,850),"Твой проводник по проекту",24,MUTED)
    elif scene==1:
        if part==0:
            title(d,"02 / ТВОЙ ПЕРСОНАЖ","Характер —\nна твой вкус.")
        elif 1<=part<=3:
            idx=part-1;x=100+idx*310;y=455
            color,kind,head,sub=[(LILAC,"book","Наставник","Объяснит сложное"),(MINT,"smile","Напарник","Поддержит и поболтает"),(YELLOW,"sliders","Твой вариант","Ты задаёшь характер")][idx]
            card(d,x,y,285,263,color)
            icon(d,kind,x+27,y+26,77)
            text(d,(x+25,y+135),head,31,bold=True)
            text(d,(x+25,y+186),sub,20)
        else:
            pill(d,(100,764),"Манера общения",WHITE,23)
            pill(d,(386,764),"Умения",WHITE,23)
    elif scene==2:
        if part==0:
            title(d,"03 / ОБЩЕНИЕ И ПАМЯТЬ","Можно просто\nпоговорить.")
            card(d,100,447,945,321)
            text(d,(127,467),"ПРИМЕР РАЗГОВОРА",18,MUTED,True)
        elif part==1:
            rr(d,(339,513,1009,592),LILAC,22)
            text(d,(369,533),"Я учу английский.",31)
            text(d,(966,481),"ТЫ",16,MUTED,True)
        elif part==2:
            rr(d,(134,625,861,714),MINT,22)
            text(d,(163,649),"Тогда потренируемся?",31)
            text(d,(135,598),"ПЕРСОНАЖ",16,MUTED,True)
        else:
            pill(d,(100,802),"Память: учит английский",YELLOW,25)
    elif scene==3:
        if part==0:
            title(d,"04 / ПОЛЕЗНЫЕ УМЕНИЯ","Помощь на языке\nобычных людей.",79)
        elif part<=3:
            k=part-1;y=451+k*119
            kind,head,color=[("bell","Напомни завтра в 10:00",YELLOW),("note","Запиши: идея для проекта",LILAC),("book","Объясни времена в английском",MINT)][k]
            card(d,100,y,945,99)
            rr(d,(114,y+13,189,y+86),color,19)
            icon(d,kind,123,y+19,60)
            text(d,(211,y+33),head,30,bold=True)
            icon(d,"check",968,y+26,43,"#58856A")
        else:
            text(d,(103,828),"Умения включаются в настройках персоны.",24,MUTED)
    elif scene==4:
        if part==0:
            title(d,"05 / НЕМНОГО ЖИЗНИ","Может написать\nпервым.")
            pill(d,(100,433),"Когда включена инициатива",MINT,25)
        elif part==1:
            card(d,100,541,945,204)
            d.ellipse((126,568,186,628),fill=LILAC)
            icon(d,"smile",133,575,46)
            text(d,(207,568),"ТВОЙ ПЕРСОНАЖ",20,MUTED,True)
            text(d,(939,568),"18:30",20,MUTED)
            text(d,(207,624),"Ну как прошёл твой день?",37,bold=True)
        else:
            pill(d,(100,800),"Настроение",YELLOW,24)
            pill(d,(343,800),"Своя история",LILAC,24)
    elif scene==5:
        if part==0:
            title(d,"06 / ГДЕ ОБЩАТЬСЯ","Выбирай, где\nтебе удобно.")
        elif part<=3:
            k=part-1;x=100+k*310;y=455
            kind,head,color=[("browser","Браузер",MINT),("plane","Telegram",LILAC),("phone","Android",YELLOW)][k]
            card(d,x,y,285,254,color)
            icon(d,kind,x+93,y+36,100)
            text(d,(x+142,y+178),head,32,bold=True,anchor="mt")
        else:
            pill(d,(100,770),"Текст",WHITE,25)
            pill(d,(269,770),"Голос",WHITE,25)
            icon(d,"mic",424,766,57)
    elif scene==6:
        if part==0:
            title(d,"07 / С ЧЕГО НАЧАТЬ","Персонаж + модель\n= можно общаться.",76)
        elif part==1:
            card(d,100,467,404,225,MINT)
            text(d,(129,495),"ШАГ 1",20,MUTED,True)
            text(d,(129,541),"Выбери\nперсонажа",38,bold=True,spacing=2)
            icon(d,"smile",390,524,76)
        elif part==2:
            card(d,548,467,497,225,LILAC)
            text(d,(577,495),"ШАГ 2",20,MUTED,True)
            text(d,(577,541),"Подключи модель\nдля ответов",37,bold=True,spacing=2)
        else:
            text(d,(104,733),"ТРИ СПОСОБА ПОДКЛЮЧЕНИЯ МОДЕЛИ",20,MUTED,True)
            x=100
            for v,c in [("API-ключ",WHITE),("Веб-чат",WHITE),("Ollama · локально",YELLOW)]:
                x+=pill(d,(x,780),v,c,24)+16
    elif scene==7:
        if part==0:
            title(d,"08 / ВОТ И ВСЁ","Твой персонаж.\nТвои правила.",90)
        elif part==1:
            x=100
            for v,c in [("Характер",LILAC),("Память",MINT),("Умения",YELLOW)]:
                x+=pill(d,(x,478),v,c,25)+15
        elif part==2:
            card(d,100,603,945,198,LILAC)
            text(d,(132,631),"СЛЕДУЮЩИЙ РОЛИК",20,MUTED,True)
            text(d,(132,673),"Первый запуск",44,bold=True)
            text(d,(132,737),"и первое «привет»",29)
        else:
            pill(d,(1284,201),"До встречи!",YELLOW,31)
    return im


def load_voice():
    scenes=[];captions=[];cursor=0;voice_parts=[]
    for i,scene in enumerate(SCRIPT["scenes"]):
        start=cursor;cursor+=.42
        lines=[]
        for j,item in enumerate(scene["lines"]):
            path=ROOT/"audio"/f"{i:02d}-{j:02d}.wav"
            with wave.open(str(path)) as f:
                if f.getsampwidth()!=2 or f.getnchannels()!=1:
                    raise RuntimeError(f"Expected mono PCM16: {path}")
                raw=np.frombuffer(f.readframes(f.getnframes()),np.int16).astype(np.float32)/32768
                source_sr=f.getframerate()
            if source_sr!=SR:
                raw=np.interp(np.arange(round(len(raw)*SR/source_sr))*source_sr/SR,np.arange(len(raw)),raw).astype(np.float32)
            nonzero=np.flatnonzero(np.abs(raw)>.002)
            if len(nonzero)==0: raise RuntimeError(f"Silent narration: {path}")
            # Keep natural consonants and breath, remove long system TTS padding.
            raw=raw[max(0,nonzero[0]-int(.07*SR)):min(len(raw),nonzero[-1]+int(.13*SR))]
            peak=np.max(np.abs(raw))
            raw*=.67/max(.01,peak)
            duration=len(raw)/SR
            item_t={"start":cursor,"end":cursor+duration,"text":item["caption"]}
            captions.append(item_t);lines.append(item_t)
            voice_parts.append((round(cursor*SR),raw))
            cursor+=duration+.22
        cursor+=.38 if i<7 else 1.05
        scenes.append({"start":start,"end":cursor,"id":scene["id"],"lines":lines})
    total=math.ceil(cursor*FPS)/FPS
    voice=np.zeros(math.ceil(total*SR),np.float32)
    for at,raw in voice_parts:voice[at:at+len(raw)]+=raw
    return scenes,captions,total,voice


SCENES,CAPTIONS,DURATION,VOICE=load_voice()
_bins=np.pad(VOICE,(0,(-len(VOICE))%400)).reshape(-1,400)
ENVELOPE=np.sqrt((_bins**2).mean(axis=1))


def srt_time(t):
    ms=round(t*1000);s,ms=divmod(ms,1000);m,s=divmod(s,60);h,m=divmod(m,60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_subtitles():
    blocks=[]
    for i,c in enumerate(CAPTIONS,1):
        blocks.append(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end']+.13)}\n{c['text']}\n")
    (OUT/"intro.ru.srt").write_text("\n".join(blocks),encoding="utf-8")
    (OUT/"timing.json").write_text(json.dumps({"duration":DURATION,"scenes":SCENES},ensure_ascii=False,indent=2),encoding="utf-8")


def synth_audio():
    """Original quiet plucked melody, stereo bed, transitions; no stock music."""
    n=len(VOICE);left=np.zeros(n,np.float32);right=np.zeros(n,np.float32)
    def note(at, midi, length, gain, pan=0):
        start=round(at*SR);count=min(round(length*SR),n-start)
        if count<=0:return
        tt=np.arange(count,dtype=np.float32)/SR
        freq=440*2**((midi-69)/12)
        env=(1-np.exp(-tt*110))*np.exp(-tt*5.5/length)
        signal=(np.sin(2*np.pi*freq*tt)+.22*np.sin(2*np.pi*freq*2*tt)+.1*np.sin(2*np.pi*freq*3*tt))*env*gain
        left[start:start+count]+=signal*math.sqrt((1-pan)*.5)
        right[start:start+count]+=signal*math.sqrt((1+pan)*.5)
    beat=60/108
    chords=[(60,64,67,71),(57,60,64,67),(53,57,60,64),(55,59,62,67)]
    for k in range(math.ceil(DURATION/(beat*.5))):
        at=k*beat*.5
        chord=chords[(k//16)%4]
        if k%8 in (0,3,6):
            midi=chord[(k//3)%4]+12
            note(at,midi,.7,.032,(-.45 if k%2 else .45))
        if k%8==0:note(at,chord[0]-12,1.2,.023,-.12)
    for scene in SCENES:
        note(scene["start"]+.05,79,.35,.036,-.4)
        note(scene["start"]+.15,84,.45,.024,.4)
    # Gentle ducking makes the narration consistently easy to understand.
    envelope=np.repeat(ENVELOPE,400)[:n]
    duck=1-.38*np.clip(envelope/.09,0,1)
    fade=np.minimum(np.arange(n)/SR/1.2,(DURATION-np.arange(n)/SR)/1.4).clip(0,1)
    mix=np.stack([VOICE+left*duck*fade,VOICE+right*duck*fade],axis=1)
    mix=np.clip(mix,-.96,.96)
    with wave.open(str(OUT/"soundtrack.wav"),"wb") as wav:
        wav.setnchannels(2);wav.setsampwidth(2);wav.setframerate(SR)
        wav.writeframes((mix*32767).astype("<i2").tobytes())


def wrap_caption(value,width=1470,size=31):
    result=[];d=ImageDraw.Draw(Image.new("RGB",(1,1)))
    for paragraph in value.split("\n"):
        row=""
        for word in paragraph.split():
            test=(row+" "+word).strip()
            if d.textlength(test,font=font(size))>width and row:
                result.append(row);row=word
            else:row=test
        if row:result.append(row)
    return result


def frame(t,poster=False):
    scene_idx=next((i for i,s in enumerate(SCENES) if s["start"]<=t<s["end"]),7)
    scene=SCENES[scene_idx];local=t-scene["start"]
    im=background().copy()
    d=ImageDraw.Draw(im)
    for i in range(8):
        x=1399+i*53
        rr(d,(x,49,x+37,56),MINT if i<scene_idx else (INK if i==scene_idx else "#DEDED6"),4)
    text(d,(1830,72),"01 / ЧТО ЭТО ЗА ПРОЕКТ",16,MUTED,anchor="rt")
    # Staggered reveals follow the spoken explanation instead of rapid cuts.
    delays={0:[0,1.1],1:[0,.6,1.2,2.0,4.2],2:[0,.8,2.7,5.1],
            3:[0,.55,1.6,2.8,4.8],4:[0,3.5,1.5],
            5:[0,.6,1.3,2.0,4.0],6:[0,.8,2.5,4.2],7:[0,.6,3.3,5.4]}[scene_idx]
    for i,delay in enumerate(delays):
        reveal_layer(im,art(scene_idx,i),(local-delay)/.62)
    # Tiny orbiting sparkles and an audio-driven, continuously floating hero.
    d=ImageDraw.Draw(im)
    for k,(x,y) in enumerate([(1214,376),(1770,430),(1728,720),(1230,747)]):
        star(d,x+math.sin(t*1.4+k)*8,y+math.cos(t*1.3+k)*9,13+3*math.sin(t*2+k),LILAC if k%2 else YELLOW,t*.1)
    vi=min(len(ENVELOPE)-1,max(0,round(t*SR/400)))
    volume=float(ENVELOPE[vi])
    put_ghost(im,t,volume,scene_idx,ease(local/.8))
    d=ImageDraw.Draw(im)
    if scene_idx==1 and local>4.8:
        pill(d,(1301,224),"Серьёзно? Или весело!",YELLOW,23)
    if scene_idx==6 and local>4.8:
        pill(d,(1360,227),"Я готов!",YELLOW,30)
    # Narration subtitles are burned in; an optional SRT is exported as well.
    current=next((c for c in CAPTIONS if c["start"]<=t<=c["end"]+.16),None)
    rr(d,(80,905,1840,1030),INK,27)
    pill(d,(107,944),"БУ",YELLOW,20,padding=17)
    caption=current["text"] if current else ""
    if poster:caption="Знакомься с проектом за одну минуту с хвостиком."
    rows=wrap_caption(caption)
    y=951 if len(rows)<=1 else 930
    for row in rows:
        text(d,(1010,y),row,31,WHITE,anchor="mt");y+=44
    # A thin scene strip doubles as a quiet progress indicator.
    rr(d,(100,1050,1820,1054),"#DEDED6",2)
    rr(d,(100,1050,100+1720*t/DURATION,1054),"#8EAC99",2)
    if not poster:
        fade=min(1,local/.22,(scene["end"]-t)/.2)
        if fade<1:im=Image.blend(background(),im,max(0,fade))
    return im


def previews():
    thumbs=[]
    for i,scene in enumerate(SCENES):
        t=min(scene["end"]-.8,scene["start"]+max(5.8,(scene["end"]-scene["start"])*.72))
        im=frame(t)
        im.save(OUT/f"scene-{i+1:02d}.jpg",quality=92)
        thumb=im.resize((640,360),Image.Resampling.LANCZOS)
        thumbs.append(thumb)
    sheet=Image.new("RGB",(1280,1440),BG)
    for i,im in enumerate(thumbs):sheet.paste(im,((i%2)*640,(i//2)*360))
    sheet.save(OUT/"storyboard.jpg",quality=92)
    frame(3.1,poster=True).save(OUT/"poster.jpg",quality=96)


def render():
    if not shutil.which("ffmpeg"):raise RuntimeError("Install FFmpeg first.")
    output=OUT/"virtual-persona-core-01.mp4"
    cmd=["ffmpeg","-y","-hide_banner","-loglevel","warning",
         "-f","rawvideo","-vcodec","rawvideo","-pix_fmt","rgb24",
         "-s",f"{W}x{H}","-r",str(FPS),"-i","-",
         "-i",str(OUT/"soundtrack.wav"),
         "-map","0:v:0","-map","1:a:0","-c:v","libx264",
         "-preset","fast","-crf","19","-pix_fmt","yuv420p",
         "-c:a","aac","-b:a","192k","-ar","48000",
         "-af","loudnorm=I=-16:TP=-1.5:LRA=9",
         "-movflags","+faststart","-t",str(DURATION),
         "-metadata","title=Что такое Virtual Persona Core? | Бу объясняет — 01",
         "-metadata","comment=Procedural artwork, animation and music. Russian synthetic narration.",
         str(output)]
    process=subprocess.Popen(cmd,stdin=subprocess.PIPE)
    count=round(DURATION*FPS)
    try:
        for i in range(count):
            process.stdin.write(frame(i/FPS).tobytes())
            if i%(FPS*5)==0:print(f"render {i/FPS:5.1f}/{DURATION:.1f}s ({i/count:.0%})",flush=True)
        process.stdin.close()
        if process.wait()!=0:raise RuntimeError("FFmpeg encoding failed")
    except BaseException:
        process.kill();process.wait();raise
    print(f"Done: {output}",flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview",action="store_true")
    args=parser.parse_args()
    OUT.mkdir(exist_ok=True)
    write_subtitles();synth_audio();previews()
    print(f"Duration: {DURATION:.2f}s; 8 scenes; 16 narrated lines.",flush=True)
    if not args.preview:render()


if __name__=="__main__":main()
