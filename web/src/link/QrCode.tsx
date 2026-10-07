import { useMemo } from 'react';
import qrcode from 'qrcode-generator';

/* QR-код строкой SVG-пути (без вставки готового HTML): тёмные модули —
   квадраты одного path, светлое поле с отступом в 4 модуля — по стандарту,
   иначе камеры читают хуже. Цвета фиксированы (чёрное на белом) в любой теме. */

export default function QrCode({ text, size = 240 }: { text: string; size?: number }) {
  const { d, n } = useMemo(() => {
    const qr = qrcode(0, 'M');
    qr.addData(text, 'Byte');
    qr.make();
    const count = qr.getModuleCount();
    let path = '';
    for (let r = 0; r < count; r++) {
      for (let c = 0; c < count; c++) {
        if (qr.isDark(r, c)) path += `M${c + 4} ${r + 4}h1v1h-1z`;
      }
    }
    return { d: path, n: count + 8 };
  }, [text]);
  return (
    <svg className="link-qr" width={size} height={size} viewBox={`0 0 ${n} ${n}`} shapeRendering="crispEdges" role="img">
      <rect width={n} height={n} fill="#fff" />
      <path d={d} fill="#000" />
    </svg>
  );
}
