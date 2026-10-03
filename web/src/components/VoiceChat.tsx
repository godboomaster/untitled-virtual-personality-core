import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useI18n } from '../i18n';
import type { ChatMessage, Persona } from '../mockData';
import Icon from './icons';

/* Голосовой режим чата: крупная аватарка персоны, общение голосом
   (SpeechRecognition → обычная отправка в чат → ответ бэкенда →
   speechSynthesis) либо текстом — персона в обоих случаях отвечает
   голосом. Полноэкранный режим скрывает весь остальной интерфейс
   фиксированным оверлеем. */

// Минимальные типы Web Speech API (в lib.dom их нет)
interface SpeechRecognitionAlternativeLike {
  transcript: string;
}
interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: SpeechRecognitionAlternativeLike;
}
interface SpeechRecognitionEventLike {
  results: ArrayLike<SpeechRecognitionResultLike>;
}
interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  start: () => void;
  stop: () => void;
}
type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

function getRecognitionCtor(): SpeechRecognitionCtor | null {
  const w = window as unknown as Record<string, unknown>;
  return (w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null) as SpeechRecognitionCtor | null;
}

interface VoiceChatProps {
  persona: Persona;
  messages: ChatMessage[];
  // Аватар персоны (data-URL), без него — первая буква имени
  avatar?: string;
  // Персона печатает ответ — озвучка ждёт конца ответа целиком
  typing: boolean;
  // Реплика оператора — тем же путём, что из поля ввода (на бэкенд)
  onSend: (text: string) => void;
  // Возврат к классическому чату
  onSwitchToClassic: () => void;
}

export default function VoiceChat({ persona, messages, avatar, typing, onSend, onSwitchToClassic }: VoiceChatProps) {
  const { lang, t } = useI18n();
  // Как говорит оператор: голосом (микрофон) или текстом
  const [inputMode, setInputMode] = useState<'voice' | 'text'>(() => (getRecognitionCtor() ? 'voice' : 'text'));
  const [fullscreen, setFullscreen] = useState(false);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  // Живая расшифровка (промежуточные результаты распознавания)
  const [interim, setInterim] = useState('');
  const [draft, setDraft] = useState('');
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const micSupported = getRecognitionCtor() != null;

  // Озвучка реплики персоны: голос подбирается под текущий язык интерфейса
  const speak = (text: string) => {
    if (!('speechSynthesis' in window)) return;
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(text);
    utter.lang = lang === 'ru' ? 'ru-RU' : 'en-US';
    const voice = window.speechSynthesis.getVoices().find((v) => v.lang.toLowerCase().startsWith(lang));
    if (voice) utter.voice = voice;
    setSpeaking(true);
    utter.onend = () => setSpeaking(false);
    utter.onerror = () => setSpeaking(false);
    window.speechSynthesis.speak(utter);
  };

  // Отправка реплики оператора (общий путь для голоса и текста)
  const send = (raw: string) => {
    const text = raw.trim();
    if (!text) return;
    onSend(text);
    setDraft('');
    setInterim('');
  };

  // Озвучка ответов: всё, что персона написала после входа в режим (или
  // смены персоны), зачитывается, когда ответ закончен — части ответа
  // (extra_messages) склеиваются в одну реплику. История до входа не звучит.
  // Уже озвученное помним по id и тексту: перечитка истории может заменить
  // пузырь копией с другим id — второй раз он не звучит
  const spoken = useRef<{ ids: Set<number>; texts: Set<string> } | null>(null);
  const botKey = messages.filter((m) => m.role === 'bot').map((m) => `${m.id}:${m.text.length}`).join(',');
  useEffect(() => {
    spoken.current = null;
  }, [persona.id]);
  useEffect(() => {
    const bots = messages.filter((m) => m.role === 'bot' && m.text.trim());
    if (spoken.current === null) {
      spoken.current = { ids: new Set(bots.map((m) => m.id)), texts: new Set(bots.map((m) => m.text)) };
      return;
    }
    if (typing) return;
    const seen = spoken.current;
    const fresh = bots.filter((m) => !seen.ids.has(m.id) && !seen.texts.has(m.text));
    bots.forEach((m) => seen.ids.add(m.id));
    if (!fresh.length) return;
    fresh.forEach((m) => seen.texts.add(m.text));
    speak(fresh.map((m) => m.text).join(' '));
    // speak зависит только от lang — повторять эффект на его смену не нужно
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typing, botKey, persona.id]);

  // Микрофон: старт/стоп распознавания речи
  const toggleMic = () => {
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }
    const Ctor = getRecognitionCtor();
    if (!Ctor) return;
    const rec = new Ctor();
    rec.lang = lang === 'ru' ? 'ru-RU' : 'en-US';
    rec.interimResults = true;
    rec.continuous = false;
    rec.onresult = (e) => {
      let finalText = '';
      let interimText = '';
      for (let i = 0; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finalText += r[0].transcript;
        else interimText += r[0].transcript;
      }
      setInterim(interimText);
      if (finalText.trim()) send(finalText);
    };
    rec.onend = () => {
      setListening(false);
      setInterim('');
    };
    rec.onerror = () => {
      setListening(false);
      setInterim('');
    };
    recognitionRef.current = rec;
    setListening(true);
    rec.start();
  };

  // Остановка синтеза/распознавания при уходе с экрана или смене персоны
  useEffect(() => {
    return () => {
      recognitionRef.current?.stop();
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    };
  }, [persona.id]);

  // Последняя реплика персоны — «субтитр» под аватаркой
  const lastBotLine = [...messages].reverse().find((m) => m.role === 'bot');
  const statusLine = listening
    ? t('chat.listening')
    : speaking
      ? t('chat.speaking', { name: persona.name })
      : t(`status.${persona.status}`);

  // Фулскрин рендерится порталом в <body> (у анимаций-предков есть transform,
  // из-за него position: fixed скроллится вместе со страницей) + блокируем скролл
  useEffect(() => {
    if (!fullscreen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, [fullscreen]);

  const root = (
    <div className={`voice-chat ${fullscreen ? 'voice-chat--fullscreen' : ''}`}>
      <div className="voice-chat-topbar">
        <div className="voice-chat-title">
          {persona.name} · <span className="voice-chat-status">{statusLine}</span>
        </div>
        <div className="voice-chat-actions">
          <button type="button" className="btn btn--ghost" title={t('chat.modeClassicTitle')} onClick={onSwitchToClassic}>
            <Icon name="chat" size={13} />
            {t('chat.modeClassic')}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            title={fullscreen ? t('chat.exitFullscreenTitle') : t('chat.fullscreenTitle')}
            onClick={() => setFullscreen((v) => !v)}
          >
            <Icon name={fullscreen ? 'fullscreenExit' : 'fullscreen'} size={13} />
            {fullscreen ? t('chat.exitFullscreen') : t('chat.fullscreen')}
          </button>
        </div>
      </div>

      <div className="voice-chat-stage">
        <div
          className={`voice-avatar ${listening ? 'voice-avatar--listening' : ''} ${speaking ? 'voice-avatar--speaking' : ''}`}
        >
          <img className="voice-avatar-img" src={avatar || '/avatar-placeholder.png'} alt={persona.name} />
        </div>

        {/* Живая расшифровка во время прослушивания, иначе последняя реплика персоны */}
        <div className="voice-chat-line">
          {listening ? interim || t('chat.listening') : (lastBotLine?.text ?? '')}
        </div>
      </div>

      <div className="voice-chat-controls">
        {/* Переключатель способа ввода оператора: голос / текст */}
        <div className="voice-input-toggle" role="group" aria-label={t('chat.inputModeTitle')}>
          <button
            type="button"
            className={inputMode === 'voice' ? 'active' : ''}
            disabled={!micSupported}
            title={micSupported ? t('chat.inputVoiceTitle') : t('chat.micUnsupported')}
            onClick={() => setInputMode('voice')}
          >
            {t('chat.inputVoice')}
          </button>
          <button
            type="button"
            className={inputMode === 'text' ? 'active' : ''}
            title={t('chat.inputTextTitle')}
            onClick={() => setInputMode('text')}
          >
            {t('chat.inputText')}
          </button>
        </div>

        {inputMode === 'voice' ? (
          <button
            type="button"
            className={`voice-mic-btn ${listening ? 'voice-mic-btn--active' : ''}`}
            title={listening ? t('chat.micStop') : t('chat.micStart')}
            onClick={toggleMic}
          >
            {listening ? '■' : '◉'}
          </button>
        ) : (
          <div className="voice-text-bar">
            <input
              className="chat-input"
              type="text"
              placeholder={t('chat.voiceInputPh', { name: persona.name })}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && send(draft)}
            />
            <button type="button" className="btn btn--primary" disabled={!draft.trim()} onClick={() => send(draft)}>
              {t('chat.send')}
            </button>
          </div>
        )}
        {!micSupported && <div className="voice-chat-note">{t('chat.micUnsupported')}</div>}
      </div>
    </div>
  );

  return fullscreen ? createPortal(root, document.body) : root;
}
