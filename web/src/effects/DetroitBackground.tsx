import { useEffect, useRef } from 'react';

/* Canvas-фон в HUD-стиле: диагональная геометричная сетка, горизонтальные
   линии, плавающие LED-точки, пульсирующие угловые скобки и вертикальные
   LED-бары. Палитра чисто чёрно-бело-серая, цвета подстраиваются под
   html[data-theme] на каждом кадре. */
export function DetroitBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animId: number;
    let time = 0;

    function resize() {
      if (!canvas) return;
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    }

    function draw() {
      if (!ctx || !canvas) return;
      const w = canvas.width;
      const h = canvas.height;
      time += 0.008;

      // Палитра текущей темы: чистый монохром (чернила/бумага)
      const dark = document.documentElement.getAttribute('data-theme') === 'dark';
      const line = dark ? 'rgba(232, 232, 232, 0.06)' : 'rgba(18, 18, 18, 0.06)';
      const lineSoft = dark ? 'rgba(232, 232, 232, 0.04)' : 'rgba(18, 18, 18, 0.04)';
      const led = (a: number) =>
        dark ? `rgba(208, 208, 208, ${a})` : `rgba(64, 64, 64, ${a})`;

      ctx.clearRect(0, 0, w, h);

      // Геометричная сетка диагоналей
      ctx.strokeStyle = line;
      ctx.lineWidth = 1;

      const spacing = 180;
      for (let i = -h; i < w + h; i += spacing) {
        ctx.beginPath();
        ctx.moveTo(i, 0);
        ctx.lineTo(i + h * 0.7, h);
        ctx.stroke();
      }
      for (let i = -h; i < w + h; i += spacing) {
        ctx.beginPath();
        ctx.moveTo(i + h * 0.7, 0);
        ctx.lineTo(i, h);
        ctx.stroke();
      }

      // Горизонтальные линии-сканлайны
      ctx.strokeStyle = lineSoft;
      for (let y = 60; y < h; y += 120) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }

      // Плавающие LED-точки
      ctx.fillStyle = led(0.2);
      for (let i = 0; i < 20; i++) {
        const x = (Math.sin(time * 0.3 + i * 2.5) * 0.5 + 0.5) * w;
        const y = (Math.cos(time * 0.2 + i * 1.7) * 0.5 + 0.5) * h;
        const size = 1.5 + Math.sin(time + i) * 0.5;
        ctx.beginPath();
        ctx.arc(x, y, Math.max(size, 0.5), 0, Math.PI * 2);
        ctx.fill();
      }

      // Пульсирующие угловые скобки
      const pulse = Math.sin(time * 2) * 0.3 + 0.7;
      ctx.strokeStyle = led(0.12 * pulse);
      ctx.lineWidth = 1.5;
      // Верхний левый
      ctx.beginPath(); ctx.moveTo(20, 35); ctx.lineTo(20, 20); ctx.lineTo(35, 20); ctx.stroke();
      // Верхний правый
      ctx.beginPath(); ctx.moveTo(w - 35, 20); ctx.lineTo(w - 20, 20); ctx.lineTo(w - 20, 35); ctx.stroke();
      // Нижний левый
      ctx.beginPath(); ctx.moveTo(20, h - 35); ctx.lineTo(20, h - 20); ctx.lineTo(35, h - 20); ctx.stroke();
      // Нижний правый
      ctx.beginPath(); ctx.moveTo(w - 35, h - 20); ctx.lineTo(w - 20, h - 20); ctx.lineTo(w - 20, h - 35); ctx.stroke();

      // Вертикальные LED-бары
      ctx.fillStyle = led(0.06 * pulse);
      const barX = w - 80;
      for (let i = 0; i < 6; i++) {
        const barH = 60 + Math.sin(time * 1.5 + i * 0.8) * 30;
        ctx.fillRect(barX + i * 12, h * 0.15, 3, barH);
      }

      // Статусная точка и подпись
      ctx.fillStyle = led(0.35 * pulse);
      ctx.beginPath(); ctx.arc(w - 100, h - 30, 3, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = dark ? 'rgba(238, 236, 226, 0.25)' : 'rgba(19, 19, 17, 0.25)';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.fillText('VPC // ONLINE', w - 165, h - 26);

      animId = requestAnimationFrame(draw);
    }

    resize();
    window.addEventListener('resize', resize);
    draw();

    return () => {
      window.removeEventListener('resize', resize);
      cancelAnimationFrame(animId);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="detroit-bg"
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        width: '100%',
        height: '100%',
        pointerEvents: 'none',
        zIndex: 0,
      }}
    />
  );
}
