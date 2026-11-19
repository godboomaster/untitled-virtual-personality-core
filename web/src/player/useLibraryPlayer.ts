import { useCallback, useEffect, useRef, type RefObject } from 'react';
import { updateLibraryPlayer, type PlayerCommand } from '../api';

const clientId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const finite = (n: number) => Number.isFinite(n) ? Math.min(864000, Math.max(0, n)) : 0;

/** The tab keeps the audio; the core relays bot and portrait controls to it. */
export function useLibraryPlayer(audio: RefObject<HTMLAudioElement | null>, title: string,
  move: (delta: number) => Promise<void>, close: () => void) {
  const latest = useRef({ title, move, close });
  latest.current = { title, move, close };
  const link = useRef({ client: clientId(), active: false, activate: false, revision: 0 });
  const inFlight = useRef<Promise<unknown> | null>(null);
  const deactivate = useCallback(() => {
    link.current.active = false; link.current.activate = false; link.current.revision++;
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: number;
    let ack: string | undefined;
    let error = '';
    let stopping = false;
    async function execute(command: PlayerCommand, value?: number) {
      const el = audio.current;
      if (!el) return;
      if (command === 'pause' || command === 'stop') { el.pause(); if (command === 'stop') { el.currentTime = 0; latest.current.close(); } }
      else if (command === 'resume') await el.play();
      else if (command === 'next' || command === 'previous') await latest.current.move(command === 'next' ? 1 : -1);
      else if (command === 'seek') el.currentTime = Math.min(value ?? 0, finite(el.duration));
      else {
        el.volume = Math.max(0, Math.min(1, command === 'volume' ? value ?? el.volume : el.volume + (command === 'volume_up' ? .1 : -.1)));
        el.muted = el.volume === 0;
      }
    }
    async function tick() {
      const el = audio.current;
      let claiming = false;
      const revision = link.current.revision;
      try {
        if (el && ((el.src && (link.current.active || link.current.activate)) || ack)) {
          const activate = link.current.activate;
          claiming = activate;
          if (activate) stopping = false;
          link.current.activate = false;
          const request = updateLibraryPlayer(link.current.client, {
            title: latest.current.title.slice(0, 300), state: stopping ? 'stopped' : el.error ? 'error' : el.paused ? 'paused' : el.readyState < 3 ? 'buffering' : 'playing',
            volume: el.volume, position: finite(el.currentTime), duration: finite(el.duration),
          }, activate, ack, error);
          inFlight.current = request;
          const next = await request;
          if (!alive) return;
          if (revision === link.current.revision) {
            ack = undefined; error = '';
            link.current.active = next.active && !stopping;
            if (!next.active) el.pause();
            for (const row of next.commands) {
              try { await execute(row.command, row.value); }
              catch (e) { error = e instanceof Error ? e.message.slice(0, 300) : 'Playback failed'; }
              ack = row.id;
              stopping = row.command === 'stop';
            }
          }
        }
      } catch {
        // Retry registration and acknowledgments without interrupting local audio.
        if (claiming && revision === link.current.revision) link.current.activate = true;
      }
      if (alive) timer = window.setTimeout(() => { void tick(); }, ack ? 0 : 750);
    }
    void tick();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [audio]);

  return {
    deactivate,
    activate: () => {
      if (!link.current.active) {
        link.current = { client: clientId(), active: true, activate: true, revision: link.current.revision + 1 };
      }
    },
    finish: () => {
      const el = audio.current;
      if (!el) return;
      const client = link.current.client;
      const state = { title: latest.current.title.slice(0, 300), state: 'stopped' as const,
        volume: el.volume, position: finite(el.currentTime), duration: finite(el.duration) };
      link.current.active = false; link.current.activate = false; link.current.revision++;
      // Finish after any activation already in flight. A new song gets a new
      // owner token, so a late completion cannot close its player.
      void Promise.resolve(inFlight.current).catch(() => {}).then(() => updateLibraryPlayer(client, state, false)).catch(() => {});
    },
  };
}
