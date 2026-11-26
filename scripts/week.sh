#!/bin/zsh
# Недельный замер видимости в ответах ИИ (FlyMyAI). Вручную, раз в неделю:
#   scripts/week.sh             — замер с сегодняшней датой
#   scripts/week.sh 2026-11-30  — с указанной (повторный запуск с той же датой продолжает с того места, где остановился)
# Переменные: PY — интерпретатор (по умолчанию python3, нужен 3.11+); LIMIT=5 — только первые N вопросов (пробный замер);
# BRAND=0 — без оценки бренда.
# Цены — в README.md, раздел «Сколько стоит». Перед первым запуском — scripts/setup.py.
set -e
cd "${0:A:h}/.."
PY=${PY:-python3}
RUN=${1:-$(date +%F)}
mkdir -p out/logs
LIM=()
[[ -n "$LIMIT" ]] && LIM=(--limit "$LIMIT")
set -x
"$PY" -u scripts/flymy_measure.py --run "$RUN" "${LIM[@]}" 2>&1 | tee "out/logs/measure_$RUN.log"
set +x
# Съём платный. Если после добора остались несостоявшиеся вызовы — останавливаемся и разбираемся, а не запускаем заново:
# автоматические повторы упавших частей однажды сожгли около $19 (запись результата не проходила, а съём оплачивался снова).
if grep -q "после добора осталось\|не завершена" "out/logs/measure_$RUN.log"; then
  echo "СТОП: съём прошёл не целиком, см. out/logs/measure_$RUN.log. Причину искать в результате запуска (get_run), досъём — флагом --only-chunks."
  exit 1
fi
set -x
"$PY" -u scripts/flymy_entities.py --run "$RUN"                        # компании и тон в каждом ответе
"$PY" -u scripts/flymy_entities.py --run "$RUN"                        # второй проход: только то, что не разобралось
"$PY" -u scripts/enrich.py --run "$RUN"                                # редиректы Google, контекст цитат, проверка своих страниц
if [[ "$BRAND" != "0" ]]; then
  "$PY" -u scripts/flymy_brand.py --run "$RUN"                         # оценка бренда: модели из project.json, через агента FlyMyAI
  "$PY" -u scripts/brand_openrouter.py --run "$RUN"                    # Claude через OpenRouter (без ключа — пропускается)
fi
"$PY" scripts/flymy_site.py --run "$RUN"                               # сводка и публикация страницы
