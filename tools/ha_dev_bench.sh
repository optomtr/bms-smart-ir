#!/usr/bin/env bash
# Стенд: интеграция + 60 виртуальных Broadlink в локальном Home Assistant.
#
#   tools/ha_dev_bench.sh deploy   — залить интеграцию и перезапустить HA
#   tools/ha_dev_bench.sh sim      — поднять 60 виртуальных передатчиков
#   tools/ha_dev_bench.sh status   — что происходит: связь, ошибки, журнал
#   tools/ha_dev_bench.sh all      — deploy + sim
#
# ЛОВУШКА, стоившая часа отладки: симулятор нужно запускать как ГЛАВНЫЙ процесс
# docker exec (`docker exec -d ... python3 ...`). Вариант `sh -c "... &"`
# выглядит рабочим — печатает список устройств — но docker убивает его сразу
# после выхода оболочки, и Home Assistant видит сеть, где никого нет.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HA_CONFIG="${HA_CONFIG:-$HOME/ha-dev/config}"
CONTAINER="${HA_CONTAINER:-ha-dev}"
COUNT="${COUNT:-60}"
BASE_PORT="${BASE_PORT:-20000}"

deploy() {
  echo "→ копирую интеграцию в $HA_CONFIG/custom_components"
  rm -rf "$HA_CONFIG/custom_components/bms_smart_ir"
  cp -R "$REPO/custom_components/bms_smart_ir" "$HA_CONFIG/custom_components/"
  find "$HA_CONFIG/custom_components/bms_smart_ir" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
  cp "$REPO/tools/broadlink_sim.py" "$HA_CONFIG/broadlink_sim.py"
  echo "→ перезапускаю $CONTAINER"
  docker restart "$CONTAINER" >/dev/null
  echo "→ жду запуска Home Assistant"
  # По веб-интерфейсу, а не по строчке в журнале: уровень журнала на объекте
  # может быть выставлен в warning, и ожидаемой строчки там просто не будет.
  until curl -sf -o /dev/null "http://localhost:8123/manifest.json"; do
    sleep 4
  done
  echo "готово"
}

sim() {
  # Скобка в шаблоне обязательна: без неё pkill находит собственную оболочку
  # (её командная строка содержит имя файла) и убивает сам себя.
  docker exec "$CONTAINER" sh -c "pkill -f '[b]roadlink_sim.py'" || true
  docker exec -d "$CONTAINER" python3 /config/broadlink_sim.py \
    --count "$COUNT" --base-port "$BASE_PORT" --drift --json /config/sim_devices.json
  sleep 4
  local alive
  alive="$(docker exec "$CONTAINER" sh -c "ps aux | grep -c '[b]roadlink_sim'")"
  if [ "$alive" -lt 1 ]; then
    echo "симулятор не поднялся" >&2
    exit 1
  fi
  echo "→ работает $COUNT виртуальных Broadlink с порта $BASE_PORT"
}

status() {
  echo "── связь ──"
  docker exec "$CONTAINER" python3 - <<'PY'
import sqlite3
db = sqlite3.connect("/config/home-assistant_v2.db")
rows = db.execute("""
    select s.state, count(distinct sm.entity_id)
      from states s join states_meta sm on s.metadata_id = sm.metadata_id
     where sm.entity_id like 'binary_sensor.%sviaz%'
       and s.last_updated_ts > strftime('%s','now') - 300
     group by s.state
""").fetchall()
print("  за 5 минут:", dict(rows) or "нет записей")
rows = db.execute("""
    select sm.entity_id, s.state
      from states s join states_meta sm on s.metadata_id = sm.metadata_id
     where sm.entity_id like 'sensor.%temperatura%'
       and s.state not in ('unknown','unavailable')
       and s.last_updated_ts > strftime('%s','now') - 300
     limit 5
""").fetchall()
for entity_id, state in rows:
    print(f"  {entity_id}: {state}")
PY
  echo "── журнал интеграции ──"
  docker exec "$CONTAINER" sh -c \
    "grep -iE 'bms_smart_ir' /config/home-assistant.log | grep -viE 'not been tested' | tail -12"
}

case "${1:-all}" in
  deploy) deploy ;;
  sim) sim ;;
  status) status ;;
  all) deploy; sim ;;
  *) echo "используйте: deploy | sim | status | all" >&2; exit 1 ;;
esac
