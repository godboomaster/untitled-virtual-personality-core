import { useEffect, useState } from 'react';
import type { LearningSession } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import { api } from '../api';
import type { LearningSessionApi } from '../api';
import { useApiOnline } from '../apiData';
import FormModal from './FormModal';
import Select from './Select';

/* Вкладка «Обучение» в досье персоны: активный курс «Научи меня»,
   форма запуска нового курса и история курсов (learning_manager). */

const COURSE_TOTAL = 20; // моковая длительность курса для прогресс-бара

// Материалы курса (мок): файлы уроков и служебные сообщения бота
interface CourseMaterial {
  kind: 'file' | 'message';
  title: string;
  date: string;
  content?: string; // для файлов — содержимое (скачивается)
}

// Скачивание файла-материала через Blob (как во вкладке «Файлы»)
function downloadMaterial(m: CourseMaterial) {
  const blob = new Blob([m.content ?? ''], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = m.title;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export default function LearningPanel({ personaId }: { personaId: string }) {
  const { lang, t } = useI18n();
  const { learningByPersona } = useMockData();
  const apiOnline = useApiOnline();

  // Варианты частоты уроков нового курса и дни недели — из словаря текущей локали
  const frequencyOptions = t('learn.freqOptions').split('|');
  const weekdays = t('tasks.weekdays').split('|');
  const customDaysLabel = frequencyOptions[4];

  // Интервал уроков в секундах по индексу опции (локаленезависимо):
  // каждый час | каждый день | по будням | по выходным | по дням недели | раз в неделю
  const FREQ_INTERVALS = [3600, 86400, 86400, 86400, 86400, 604800];

  // unix-секунды → «09.08, 10:00»
  const fmtNext = (ts: number | null): string =>
    ts
      ? new Date(ts * 1000).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', {
          day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
        })
      : '—';

  // Человекочитаемая частота из интервала в секундах
  const fmtInterval = (sec: number): string => {
    const idx = FREQ_INTERVALS.indexOf(sec);
    if (idx >= 0) return frequencyOptions[idx];
    return `~${Math.round(sec / 3600)} ч`;
  };

  // Сессия бэкенда → курс UI
  const mapSession = (s: LearningSessionApi, i: number): LearningSession => ({
    id: parseInt(s.session_id.slice(0, 8), 16) || i + 1,
    subject: s.subject,
    status: 'active',
    lessonCount: s.lesson_count,
    coveredTopics: s.covered_topics,
    vocabulary: s.learned_vocabulary,
    frequency: fmtInterval(s.interval_seconds),
    nextLesson: fmtNext(s.next_lesson_at),
    quizPending: s.quiz_pending ? 1 : 0,
  });

  const statusLabels: Record<LearningSession['status'], string> = {
    active: t('learn.statusActive'),
    paused: t('learn.statusPaused'),
    finished: t('learn.statusFinished'),
  };

  // Человекочитаемая частота: «по дням недели» раскрывается в дни
  const composeFrequency = (freq: string, days: string[]): string => {
    if (freq === customDaysLabel) return days.length ? days.join(', ') : customDaysLabel;
    return freq;
  };

  const courseMaterials = (c: LearningSession): CourseMaterial[] => {
    const materials: CourseMaterial[] = [];
    // Файлы уроков: по одному на каждые ~3 пройденных урока
    const fileCount = Math.max(2, Math.min(4, Math.ceil(c.lessonCount / 3)));
    for (let i = 1; i <= fileCount; i++) {
      const lessonNo = Math.min(c.lessonCount, i * 3);
      materials.push({
        kind: 'file',
        title: `lesson_${String(lessonNo).padStart(2, '0')}_${c.subject.toLowerCase().replace(/\s+/g, '_')}.md`,
        date: `${25 + i}.07, 10:00`,
        content: t('learn.lessonFile', { n: lessonNo, subject: c.subject, topic: c.coveredTopics[0] ?? t('learn.intro') }),
      });
    }
    // Служебные сообщения: тест и дайджест
    materials.push({ kind: 'message', title: t('learn.quizTitle', { topic: c.coveredTopics[0] ?? c.subject }), date: '27.07, 10:05' });
    materials.push({ kind: 'message', title: t('learn.digest'), date: '27.07, 20:00' });
    return materials;
  };

  const mockCourses = learningByPersona[personaId] ?? [];
  // Курсы, запущенные через форму в этой сессии (не в mockData)
  const [localCourses, setLocalCourses] = useState<LearningSession[]>([]);
  // Курсы с бэкенда (learning_manager) и их session_id по числовому id UI
  const [apiCourses, setApiCourses] = useState<LearningSessionApi[] | null>(null);
  const [subject, setSubject] = useState('');
  const [frequency, setFrequency] = useState(() => frequencyOptions[1]);
  const [frequencyDays, setFrequencyDays] = useState<string[]>([]);
  // Открытый в модалке курс из истории (материалы)
  const [openedCourse, setOpenedCourse] = useState<LearningSession | null>(null);

  const courses = apiOnline
    ? (apiCourses ?? []).map(mapSession)
    : [...mockCourses, ...localCourses];
  // Активных курсов может быть несколько — карточка переключается чипами
  const activeCourses = courses.filter((c) => c.status === 'active');
  // В истории — только поставленные на паузу и завершённые
  const historyCourses = courses.filter((c) => c.status !== 'active');
  const [courseIdx, setCourseIdx] = useState(0);
  const mainCourse = activeCourses.length ? activeCourses[Math.min(courseIdx, activeCourses.length - 1)] : undefined;

  // При смене персоны переключатель курса — на первый
  useEffect(() => {
    setCourseIdx(0);
  }, [personaId]);

  // Подтягиваем курсы персоны с бэкенда
  useEffect(() => {
    setApiCourses(null);
    if (!apiOnline) return;
    let stale = false;
    api.getLearning(personaId).then((r) => !stale && setApiCourses(r.sessions)).catch(() => {});
    return () => {
      stale = true;
    };
  }, [apiOnline, personaId]);

  const startCourse = () => {
    const s = subject.trim();
    if (!s) return;
    if (apiOnline) {
      const interval = FREQ_INTERVALS[frequencyOptions.indexOf(frequency)] ?? 86400;
      api.startLearning(personaId, s, interval)
        .then((r) => setApiCourses(r.sessions))
        .catch(() => {});
      setSubject('');
      return;
    }
    setLocalCourses((prev) => [
      ...prev,
      {
        id: Date.now(),
        subject: s,
        status: 'active',
        lessonCount: 0,
        coveredTopics: [],
        vocabulary: [],
        frequency: composeFrequency(frequency, frequencyDays),
        nextLesson: t('learn.defaultNext'),
        quizPending: 0,
      },
    ]);
    setSubject('');
  };

  // Остановить (завершить) активный курс.
  // В API-режиме все курсы активны, порядок совпадает с apiCourses — по индексу.
  const stopCourse = (c: LearningSession) => {
    const session = (apiCourses ?? [])[activeCourses.indexOf(c)];
    if (!session) return;
    api.stopLearning(personaId, session.session_id)
      .then((r) => setApiCourses(r.sessions))
      .catch(() => {});
  };

  const toggleDay = (d: string) => {
    setFrequencyDays((prev) => (prev.includes(d) ? prev.filter((x) => x !== d) : [...prev, d]));
  };

  return (
    <div>
      {/* Пустое состояние: курсов нет вообще */}
      {courses.length === 0 && (
        <div className="learn-empty">
          {t('learn.empty')}
        </div>
      )}

      {/* Переключатель активных курсов (если их несколько) */}
      {activeCourses.length > 1 && (
        <div className="day-chips learn-course-switch">
          {activeCourses.map((c, i) => (
            <button
              key={c.id}
              type="button"
              className={`day-chip ${mainCourse === c ? 'day-chip--on' : ''}`}
              onClick={() => setCourseIdx(i)}
            >
              {c.subject}
            </button>
          ))}
        </div>
      )}

      {/* Активный курс */}
      {mainCourse && (
        <div className="card">
          <div className="card-title-row">
            <h3 className="card-title">{mainCourse.subject}</h3>
            <span className={`badge ${mainCourse.status === 'active' ? 'badge--active' : 'badge--muted'}`}>
              {statusLabels[mainCourse.status]}
            </span>
            {apiOnline && (
              <button
                type="button"
                className="btn btn--danger"
                title={t('common.delete')}
                onClick={() => stopCourse(mainCourse)}
              >
                ✕
              </button>
            )}
          </div>

          {/* Материалы активного курса — та же модалка, что и у истории */}
          <div style={{ marginBottom: 14 }}>
            <button type="button" className="btn btn--ghost" onClick={() => setOpenedCourse(mainCourse)}>
              {t('learn.materials')}
            </button>
          </div>

          <div className="stats-row learn-stats">
            <div className="stat-card">
              <div className="stat-value">{mainCourse.lessonCount}</div>
              <div className="stat-label">{t('learn.statLessons')}</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{mainCourse.coveredTopics.length}</div>
              <div className="stat-label">{t('learn.statTopics')}</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{mainCourse.vocabulary.length}</div>
              <div className="stat-label">{t('learn.statWords')}</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{mainCourse.quizPending}</div>
              <div className="stat-label">{t('learn.statQuiz')}</div>
            </div>
          </div>

          <div className="field">
            <div className="field-label-row">
              <label className="field-label">{t('learn.progress')}</label>
              <span className="field-value">
                {mainCourse.lessonCount}/{COURSE_TOTAL}
              </span>
            </div>
            <div className="progress-bar">
              <div
                className="progress-fill"
                style={{ width: `${Math.min(100, (mainCourse.lessonCount / COURSE_TOTAL) * 100)}%` }}
              />
            </div>
          </div>

          {/* Плашка неотвеченного теста */}
          {mainCourse.quizPending > 0 && (
            <div className="learn-quiz-alert">
              {t('learn.quizAlert', { n: mainCourse.quizPending })}
            </div>
          )}

          {mainCourse.coveredTopics.length > 0 && (
            <div className="field">
              <label className="field-label">{t('learn.coveredTopics')}</label>
              <div className="badge-row">
                {mainCourse.coveredTopics.map((t) => (
                  <span key={t} className="badge">{t}</span>
                ))}
              </div>
            </div>
          )}

          {mainCourse.vocabulary.length > 0 && (
            <div className="field">
              <label className="field-label">{t('learn.vocab')}</label>
              <div className="learn-vocab">
                {mainCourse.vocabulary.slice(0, 6).join('  ·  ')}
                {mainCourse.vocabulary.length > 6 ? '  ·  …' : ''}
              </div>
            </div>
          )}

          <div className="learn-schedule">
            {t('learn.schedule', { freq: mainCourse.frequency, next: mainCourse.nextLesson })}
          </div>
        </div>
      )}

      {/* Форма нового курса */}
      <div className="card">
        <h3 className="card-title">{t('learn.newCourse')}</h3>
        <div className="learn-form">
          <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 180 }}>
            <label className="field-label" htmlFor="learn-subject">{t('learn.subjectLabel')}</label>
            <input
              id="learn-subject"
              className="input"
              placeholder={t('learn.subjectPh')}
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && startCourse()}
            />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label className="field-label" htmlFor="learn-freq">{t('learn.frequency')}</label>
            <Select
              id="learn-freq"
              value={frequency}
              options={frequencyOptions.map((f) => ({ value: f, label: f }))}
              onChange={setFrequency}
            />
          </div>
          <button type="button" className="btn btn--primary" disabled={!subject.trim()} onClick={startCourse}>
            {t('learn.start')}
          </button>
        </div>
        {frequency === customDaysLabel && (
          <div className="day-chips">
            {weekdays.map((d) => (
              <button
                key={d}
                type="button"
                className={`day-chip ${frequencyDays.includes(d) ? 'day-chip--on' : ''}`}
                onClick={() => toggleDay(d)}
              >
                {d}
              </button>
            ))}
          </div>
        )}
        <div className="field-hint">
          {t('learn.hint')}
        </div>
      </div>

      {/* История курсов: клик открывает материалы */}
      {historyCourses.length > 0 && (
        <div className="card">
          <h3 className="card-title">{t('learn.historyTitle')}</h3>
          <ul className="memory-list">
            {historyCourses.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  className="learn-history-item learn-history-item--link"
                  title={t('learn.openMaterials')}
                  onClick={() => setOpenedCourse(c)}
                >
                  <span className="memory-item-text">{c.subject}</span>
                  <span className="badge badge--muted">{statusLabels[c.status]}</span>
                  <span className="learn-history-meta">{t('learn.lessonsArrow', { n: c.lessonCount })}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Модалка «Материалы курса»: файлы (со скачиванием) и сообщения бота */}
      {openedCourse && (
        <FormModal
          title={t('learn.modalTitle', { subject: openedCourse.subject })}
          badge="LEARN_LOG"
          submitLabel={t('common.close')}
          onSubmit={() => setOpenedCourse(null)}
          onClose={() => setOpenedCourse(null)}
        >
          <ul className="memory-list" style={{ marginBottom: 0 }}>
            {courseMaterials(openedCourse).map((m, i) => (
              <li key={i} className="files-item">
                <div className="files-icon">{m.kind === 'file' ? '▤' : '◱'}</div>
                <div className="files-main">
                  <div className="files-name">{m.title}</div>
                  <div className="files-meta">
                    <span className="badge">{m.kind === 'file' ? t('learn.kindLesson') : t('learn.kindMessage')}</span>
                    <span>{m.date}</span>
                  </div>
                </div>
                {m.kind === 'file' && (
                  <div className="files-actions">
                    <button type="button" className="btn btn--ghost" onClick={() => downloadMaterial(m)}>
                      {t('common.download')}
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </FormModal>
      )}
    </div>
  );
}
