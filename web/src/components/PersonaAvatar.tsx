import { usePersonaAvatars } from '../avatarStore';
import { LiveHead } from '../portrait/LiveHead';
import { usePersonaAvatarPreset } from '../room/personaAvatarPreset';
import { useInbox } from '../inboxStore';
import { EXPR } from '../room/avatarEyes';
import { useFlashExpression } from '../portrait/motion';

/* Аватар персоны: своя картинка из «Персон» или, без неё, живая голова её
   фигуры из «Комнаты» (portrait/LiveHead) — тот же облик (форма, глаза,
   аксессуар, оттенок), что собирается в конструкторе аватара. */

interface HeadCues {
  /** Смотреть на курсор, только пока он над ближайшим предком по селектору
   *  (список, шапка); без него — где бы курсор ни был */
  followWithin?: string;
  speaking?: boolean;
  listening?: boolean;
  thinking?: boolean;
  asleep?: boolean;
  smile?: boolean;
}

export default function PersonaAvatar({
  persona,
  decorative = false,
  ...cues
}: HeadCues & {
  persona: { id: string; name: string };
  /** Имя написано рядом — картинке подпись не нужна */
  decorative?: boolean;
}) {
  const src = usePersonaAvatars()[persona.id];
  const label = decorative ? '' : persona.name;
  if (src) return <img src={src} alt={label} />;
  return <PersonaHead personaId={persona.id} label={label} {...cues} />;
}

// Живая голова без своей картинки (голосовой режим берёт её напрямую)
export function PersonaHead({ personaId, ...rest }: HeadCues & { personaId: string; label: string }) {
  const preset = usePersonaAvatarPreset(personaId);
  // Пришло новое от персоны (непрочитанных стало больше) — на миг удивляется
  const { unread } = useInbox();
  const surprise = useFlashExpression(unread[personaId] ?? 0, EXPR.surprise);
  return <LiveHead preset={preset} expression={surprise} {...rest} />;
}
