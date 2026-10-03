// Кулдаун тычка по аватару — не чаще раза в 2 минуты на персону. Общий для
// сцены (RoomScene) и скина комнаты: тычок в скине не обходит кулдаун сцены
// и наоборот
const POKE_COOLDOWN_MS = 2 * 60_000;
const lastPokeAt = new Map<string, number>();

// Тычок разрешён (кулдаун персоны прошёл) — и сразу засчитан
export function claimPoke(personaId: string): boolean {
  const now = Date.now();
  if (now - (lastPokeAt.get(personaId) ?? 0) < POKE_COOLDOWN_MS) return false;
  lastPokeAt.set(personaId, now);
  return true;
}
