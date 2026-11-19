import { useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';
import { rand, viewOf } from './motion';
import type { Mouth } from './motion';
import type { Vowel } from './portraitTypes';

/* Рот по гласным: у портрета есть листы «рот на гласную» (VA…VO) — пока
   персона говорит, рот идёт по гласным её реплики, слог за слогом. Звука
   синтез речи наружу не отдаёт, поэтому темп — свой, как у обычного рта
   (motion.useMouth); где движок говорит, какое слово звучит (браузерный
   синтез — onboundary, облачко на рабочем столе — открытое слово), рот
   догоняет это слово. Текста нет — гласные случайные.

   Гласные из текста: русские а/я → a, и/ы → i, у/ю → u, э/е → e, о/ё → o;
   латиница — по буквам, частые английские пары одной гласной (oo → u,
   ee/ea → i…), немая e на конце слова — без слога. Точность — уровня
   «рот похож»: это не фонетика. */

export interface SpeechCue {
  /** Что говорит (реплика целиком или часть облачка) */
  text: string;
  /** С какого знака текста начинается слово, которое звучит сейчас */
  at?: number;
}

// Слог — гласная; пауза — между словами, на запятой, в конце фразы
type Token = { v: Vowel; at: number } | { pause: 'word' | 'comma' | 'phrase'; at: number };

const RU: Record<string, Vowel> = { а: 'a', я: 'a', о: 'o', ё: 'o', у: 'u', ю: 'u', ы: 'i', и: 'i', э: 'e', е: 'e' };
const LAT: Record<string, Vowel> = { a: 'a', e: 'e', i: 'i', o: 'o', u: 'u', y: 'i' };
const PAIRS: Record<string, Vowel> = {
  oo: 'u', ee: 'i', ea: 'i', ie: 'i', ai: 'e', ay: 'e', ei: 'e', ey: 'e',
  oa: 'o', ou: 'u', ow: 'o', au: 'o', aw: 'o', ue: 'u', ui: 'u', oi: 'o', oy: 'o',
};
const LATIN_VOWEL = /[aeiouy]/;
const LATIN = /[a-z]/;

function wordVowels(word: string, at: number, out: Token[]) {
  const w = word.toLowerCase();
  // Немая e на конце английского слова (make, here): гласная до неё есть
  const silentE = w.length > 3 && /[a-z]e$/.test(w) && !LATIN_VOWEL.test(w[w.length - 2]) && LATIN_VOWEL.test(w.slice(0, -2));
  const end = silentE ? w.length - 1 : w.length;
  for (let i = 0; i < end; i++) {
    const ch = w[i];
    const ru = RU[ch];
    if (ru) {
      out.push({ v: ru, at });
      continue;
    }
    if (!LATIN.test(ch) || !LATIN_VOWEL.test(ch)) continue;
    // y в начале слова и перед гласной — согласная (yes, you)
    if (ch === 'y' && (i === 0 || LATIN_VOWEL.test(w[i + 1] ?? ''))) continue;
    const pair = PAIRS[w.slice(i, i + 2)];
    if (pair && i + 1 < end) {
      out.push({ v: pair, at });
      i++;
      continue;
    }
    out.push({ v: LAT[ch], at });
  }
}

/** Текст → слоги и паузы. */
export function speechScript(text: string): Token[] {
  const out: Token[] = [];
  const re = /\S+/g;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    const before = out.length;
    wordVowels(m[0], m.index, out);
    if (out.length === before) continue;
    const tail = m[0].slice(-1);
    out.push({ pause: /[.!?…;:]/.test(tail) ? 'phrase' : /[,—–-]/.test(tail) ? 'comma' : 'word', at: m.index });
  }
  return out;
}

const OPENNESS: Record<Vowel, Mouth> = { a: 2, o: 2, e: 1, i: 1, u: 1 };
const ALL: Vowel[] = ['a', 'o', 'e', 'i', 'u'];

export interface MouthShape {
  /** Насколько открыт: по нему — листы B и C, если листа гласной нет */
  open: Mouth;
  /** Какая гласная сейчас; null — согласная или пауза */
  vowel: Vowel | null;
}

const CLOSED: MouthShape = { open: 0, vowel: null };

/** Рот по гласным, пока говорит; speaking=false — закрыт. */
export function useVowelMouth(elRef: RefObject<Element | null>, speaking: boolean, cue: SpeechCue | null): MouthShape {
  const [shape, setShape] = useState<MouthShape>(CLOSED);
  const text = cue?.text ?? '';
  const at = cue?.at;
  // Куда перескочить (знак начала звучащего слова); цикл слогов читает его сам
  const jump = useRef<number | null>(null);
  useEffect(() => {
    if (at != null) jump.current = at;
  }, [at]);

  useEffect(() => {
    if (!speaking) {
      setShape(CLOSED);
      return;
    }
    const view = viewOf(elRef.current);
    const script = speechScript(text);
    let alive = true;
    let timer = 0;
    let i = 0;
    const later = (ms: number, fn: () => void) => {
      timer = view.setTimeout(() => alive && fn(), ms);
    };
    const step = () => {
      if (jump.current != null) {
        const want = jump.current;
        jump.current = null;
        const k = script.findIndex((t) => t.at >= want);
        if (k >= 0) i = k;
      }
      if (!script.length) {
        // Текста нет — гласные случайные, ритм как у обычного рта
        if (Math.random() < 0.12) {
          setShape(CLOSED);
          later(rand(150, 300), step);
          return;
        }
        const v = ALL[Math.floor(Math.random() * ALL.length)];
        setShape({ open: OPENNESS[v], vowel: v });
        later(rand(85, 150), () => {
          setShape(Math.random() < 0.3 ? { open: 1, vowel: null } : CLOSED);
          later(rand(45, 90), step);
        });
        return;
      }
      // Текст кончился, а голос ещё звучит — по кругу, после паузы фразы
      if (i >= script.length) i = 0;
      const t = script[i++];
      if ('pause' in t) {
        setShape(CLOSED);
        later(t.pause === 'phrase' ? rand(250, 420) : t.pause === 'comma' ? rand(150, 250) : rand(30, 80), step);
        return;
      }
      setShape({ open: OPENNESS[t.v], vowel: t.v });
      later(rand(90, 150), () => {
        // Согласная между гласными: рот прикрыт или закрыт
        setShape(Math.random() < 0.4 ? { open: 1, vowel: null } : CLOSED);
        later(rand(40, 80), step);
      });
    };
    step();
    return () => {
      alive = false;
      view.clearTimeout(timer);
    };
  }, [speaking, text, elRef]);
  return speaking ? shape : CLOSED;
}
