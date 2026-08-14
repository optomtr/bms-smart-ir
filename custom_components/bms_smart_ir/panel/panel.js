/**
 * BMS ИК-пульты — панель управления Broadlink в сайдбаре Home Assistant.
 *
 * Обычный веб-компонент: ни сборщика, ни Lit, ни внутренних API фронтенда
 * Home Assistant — их обновление не должно гасить панель. Все данные приходят
 * только из команд bms_smart_ir/* по WebSocket; панель не читает ни файлы, ни
 * рантайм напрямую.
 *
 * Текст из сети (имя устройства, модель, ошибка) вставляется ТОЛЬКО через
 * textContent. Ни одной подстановки в innerHTML: объявление по UDP может
 * прислать что угодно, и это готовый XSS в панели администратора.
 */

const POLL_MS = 6000;
const HISTORY_HOURS = 24;

// ------------------------------------------------------------- палитра --
// Цвета берутся из темы Home Assistant, поэтому панель одинаково выглядит
// в светлой и тёмной теме; свои значения — только там, где у темы нет
// подходящей переменной.
const CSS = `
:host {
  --ir-bg: var(--primary-background-color, #f4f6f9);
  --ir-card: var(--card-background-color, #fff);
  --ir-line: var(--divider-color, #e3e7ec);
  --ir-ink: var(--primary-text-color, #16202b);
  --ir-mut: var(--secondary-text-color, #6b7681);
  --ir-accent: var(--primary-color, #0b76ef);
  --ir-ok: var(--success-color, #14934a);
  --ir-warn: var(--warning-color, #b97d00);
  --ir-bad: var(--error-color, #d8283c);
  --ir-shadow: 0 1px 2px rgba(16,32,48,.06), 0 6px 18px rgba(16,32,48,.05);
  --ir-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  display: block;
  background: var(--ir-bg);
  color: var(--ir-ink);
  min-height: 100vh;
  font-family: var(--paper-font-body1_-_font-family, Inter, system-ui, sans-serif);
}
* { box-sizing: border-box; }
.wrap { display: flex; min-height: 100vh; }

/* ----- боковое меню панели ----- */
.nav {
  width: 232px; flex: 0 0 232px; padding: 18px 12px; border-right: 1px solid var(--ir-line);
  background: var(--ir-card); position: sticky; top: 0; height: 100vh; overflow-y: auto;
}
.brand { display: flex; align-items: center; gap: 10px; padding: 4px 8px 18px; }
.brand b { font-size: 15px; letter-spacing: .2px; }
.brand span { display: block; font-size: 11px; color: var(--ir-mut); font-weight: 500; }
.nav button {
  display: flex; align-items: center; gap: 10px; width: 100%; padding: 9px 10px; margin-bottom: 2px;
  border: 0; border-radius: 9px; background: transparent; color: var(--ir-ink);
  font: inherit; font-size: 13.5px; text-align: left; cursor: pointer;
}
.nav button:hover { background: color-mix(in srgb, var(--ir-accent) 8%, transparent); }
.nav button[aria-current="page"] { background: color-mix(in srgb, var(--ir-accent) 14%, transparent); font-weight: 600; }
.nav .count { margin-left: auto; font-size: 11.5px; color: var(--ir-mut); font-variant-numeric: tabular-nums; }

/* ----- контент ----- */
.main { flex: 1; padding: 22px 26px 60px; min-width: 0; }
h1 { font-size: 20px; margin: 0 0 2px; font-weight: 650; }
.sub { color: var(--ir-mut); font-size: 13px; margin-bottom: 20px; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.spacer { flex: 1; }

.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 12px; margin-bottom: 22px; }
.tile { background: var(--ir-card); border: 1px solid var(--ir-line); border-radius: 14px; padding: 14px 16px; box-shadow: var(--ir-shadow); }
.tile .k { font-size: 12px; color: var(--ir-mut); margin-bottom: 6px; }
.tile .v { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; line-height: 1.1; }
.tile .v small { font-size: 13px; color: var(--ir-mut); font-weight: 500; }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.card {
  background: var(--ir-card); border: 1px solid var(--ir-line); border-radius: 14px;
  padding: 16px; box-shadow: var(--ir-shadow); cursor: pointer; transition: border-color .12s, transform .12s;
}
.card:hover { border-color: color-mix(in srgb, var(--ir-accent) 45%, var(--ir-line)); transform: translateY(-1px); }
.card.flat { cursor: default; }
.card.flat:hover { transform: none; border-color: var(--ir-line); }
.card h3 { margin: 0; font-size: 15px; font-weight: 620; }
.card .addr { font-family: var(--ir-mono); font-size: 12px; color: var(--ir-mut); }

.pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 600; white-space: nowrap; }
.dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 7px; }

.meta { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--ir-line); }
.meta div { font-size: 12px; color: var(--ir-mut); }
.meta b { display: block; font-size: 14px; color: var(--ir-ink); font-weight: 600; font-variant-numeric: tabular-nums; }

.list { background: var(--ir-card); border: 1px solid var(--ir-line); border-radius: 14px; box-shadow: var(--ir-shadow); overflow: hidden; }
.item { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid var(--ir-line); }
.item:last-child { border-bottom: 0; }
.item .grow { flex: 1; min-width: 0; }
.item .nm { font-size: 14px; font-weight: 570; }
.item .sm { font-size: 12px; color: var(--ir-mut); font-family: var(--ir-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

button.act {
  border: 1px solid var(--ir-line); background: var(--ir-card); color: var(--ir-ink);
  border-radius: 9px; padding: 8px 13px; font: inherit; font-size: 13px; font-weight: 550; cursor: pointer;
}
button.act:hover { border-color: var(--ir-accent); color: var(--ir-accent); }
button.act.primary { background: var(--ir-accent); border-color: var(--ir-accent); color: #fff; }
button.act.primary:hover { filter: brightness(1.06); color: #fff; }
button.act.danger:hover { border-color: var(--ir-bad); color: var(--ir-bad); }
button.act[disabled] { opacity: .5; cursor: default; }

input, select {
  border: 1px solid var(--ir-line); border-radius: 9px; padding: 9px 11px; font: inherit; font-size: 13.5px;
  background: var(--ir-card); color: var(--ir-ink); width: 100%; max-width: 340px;
}
input:focus, select:focus { outline: 2px solid color-mix(in srgb, var(--ir-accent) 40%, transparent); outline-offset: 1px; }
label.fld { display: block; margin-bottom: 14px; }
label.fld span { display: block; font-size: 12px; color: var(--ir-mut); margin-bottom: 5px; }

.tabs { display: flex; gap: 4px; margin: 18px 0 16px; border-bottom: 1px solid var(--ir-line); }
.tabs button {
  border: 0; background: transparent; padding: 9px 14px; font: inherit; font-size: 13.5px; cursor: pointer;
  color: var(--ir-mut); border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tabs button[aria-selected="true"] { color: var(--ir-accent); border-bottom-color: var(--ir-accent); font-weight: 600; }

.empty { padding: 44px 20px; text-align: center; color: var(--ir-mut); font-size: 13.5px; }
.empty b { display: block; color: var(--ir-ink); font-size: 15px; margin-bottom: 6px; }

.toast {
  position: fixed; right: 22px; bottom: 22px; z-index: 40; max-width: 380px;
  background: var(--ir-card); border: 1px solid var(--ir-line); border-left: 3px solid var(--ir-accent);
  border-radius: 10px; padding: 12px 16px; box-shadow: 0 8px 30px rgba(16,32,48,.18); font-size: 13.5px;
}
.toast.bad { border-left-color: var(--ir-bad); }
.toast.ok { border-left-color: var(--ir-ok); }

.steps { display: flex; gap: 6px; margin-bottom: 20px; flex-wrap: wrap; }
.step { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--ir-mut); }
.step .n { width: 21px; height: 21px; border-radius: 50%; display: grid; place-items: center; font-size: 11.5px; font-weight: 650; background: var(--ir-line); color: var(--ir-mut); }
.step.on .n { background: var(--ir-accent); color: #fff; }
.step.on { color: var(--ir-ink); font-weight: 600; }
.step .sep { color: var(--ir-line); }

.brands { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; max-height: 420px; overflow-y: auto; padding-right: 4px; }
.brands button { border: 1px solid var(--ir-line); background: var(--ir-card); border-radius: 10px; padding: 11px 12px; font: inherit; font-size: 13.5px; cursor: pointer; text-align: left; color: var(--ir-ink); }
.brands button:hover { border-color: var(--ir-accent); }
.brands button .hint { display: block; font-size: 11px; color: var(--ir-mut); margin-top: 2px; }
.section-title { font-size: 12px; text-transform: uppercase; letter-spacing: .6px; color: var(--ir-mut); margin: 18px 0 8px; font-weight: 650; }

svg.chart { width: 100%; height: 130px; display: block; }
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--ir-mut); margin-top: 6px; }

.map { background: var(--ir-card); border: 1px solid var(--ir-line); border-radius: 14px; padding: 18px; box-shadow: var(--ir-shadow); }
.map .hub-row { display: flex; gap: 16px; padding: 14px 0; border-bottom: 1px dashed var(--ir-line); }
.map .hub-row:last-child { border-bottom: 0; }
.map .hub-side { width: 250px; flex: 0 0 250px; }
.map .kids { display: flex; flex-wrap: wrap; gap: 8px; flex: 1; align-content: flex-start; }
.map .kid { border: 1px solid var(--ir-line); border-radius: 9px; padding: 7px 11px; font-size: 12.5px; display: flex; align-items: center; gap: 7px; }

@media (max-width: 880px) {
  .wrap { flex-direction: column; }
  .nav { width: auto; flex: none; height: auto; position: static; display: flex; gap: 4px; overflow-x: auto; padding: 10px; }
  .nav .brand { display: none; }
  .nav button { width: auto; white-space: nowrap; }
  .main { padding: 16px; }
}
`;

// ------------------------------------------------------------- иконки --
const ICONS = {
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  map: "M4 6h5v5H4zM15 3h5v5h-5zM15 16h5v5h-5zM9 8.5h6M9 8.5v10h6",
  plus: "M12 5v14M5 12h14",
  chip: "M7 7h10v10H7zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4",
  wifi: "M5 12.5a10 10 0 0 1 14 0M8 16a5.5 5.5 0 0 1 8 0M12 19.5h.01",
  therm: "M14 14V5a2 2 0 1 0-4 0v9a4 4 0 1 0 4 0z",
  drop: "M12 3s6 6.5 6 10a6 6 0 0 1-12 0c0-3.5 6-10 6-10z",
  tv: "M3 5h18v11H3zM8 20h8",
  snow: "M12 3v18M4.5 7.5l15 9M19.5 7.5l-15 9",
  back: "M15 18l-6-6 6-6",
  check: "M4 12.5l5 5L20 6.5",
  alert: "M12 4l9 16H3zM12 10v4M12 17h.01",
  trash: "M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13",
};

function icon(name, size = 16, color = "currentColor") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", color);
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name] || ICONS.chip);
  svg.appendChild(path);
  return svg;
}

// ------------------------------------------------------------ хелперы --
function h(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === "text") node.textContent = value;          // единственный путь для текста
    else if (key === "class") node.className = value;
    else if (key === "style") node.setAttribute("style", value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

const STATUS = {
  online: { label: "В сети", color: "var(--ir-ok)" },
  connecting: { label: "Подключение", color: "var(--ir-accent)" },
  reconnecting: { label: "Переподключение", color: "var(--ir-warn)" },
  unavailable: { label: "Недоступен", color: "var(--ir-bad)" },
};

function statusPill(status) {
  const meta = STATUS[status] || STATUS.unavailable;
  return h("span", {
    class: "pill",
    style: `background:color-mix(in srgb, ${meta.color} 13%, transparent);color:${meta.color}`,
  }, [
    h("span", { class: "dot", style: `background:${meta.color}` }),
    meta.label,
  ]);
}

function ago(timestamp) {
  if (!timestamp) return "—";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
  if (seconds < 60) return `${seconds} с назад`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин назад`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} ч назад`;
  return `${Math.round(seconds / 86400)} дн назад`;
}

function plural(count, forms) {
  /** Русские окончания: 1 модель, 2 модели, 5 моделей. */
  const n = Math.abs(count) % 100;
  const last = n % 10;
  if (n > 10 && n < 20) return forms[2];
  if (last > 1 && last < 5) return forms[1];
  if (last === 1) return forms[0];
  return forms[2];
}

const MODELS_FORMS = ["модель", "модели", "моделей"];
const DEVICES_FORMS = ["прибор", "прибора", "приборов"];

const STATE_LABELS = {
  off: "Выключен", cool: "Охлаждение", heat: "Обогрев", auto: "Авто",
  dry: "Осушение", fan_only: "Вентиляция", heat_cool: "Авто",
  on: "Включён", playing: "Работает", idle: "Ожидание",
  unavailable: "Недоступен", unknown: "Неизвестно",
};

// ------------------------------------------------------------- график --
function lineChart(points, { unit = "", color = "var(--ir-accent)", fill = true } = {}) {
  const width = 640;
  const height = 130;
  const pad = { top: 12, right: 8, bottom: 18, left: 34 };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "chart");

  const usable = points.filter((point) => Number.isFinite(point.value));
  if (usable.length < 2) {
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", width / 2);
    text.setAttribute("y", height / 2);
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("fill", "var(--ir-mut)");
    text.setAttribute("font-size", "12");
    text.textContent = "Данных пока нет";
    svg.appendChild(text);
    return svg;
  }

  const values = usable.map((point) => point.value);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (max - min < 0.5) {
    // Ровная линия — обычное дело для комнаты: раздвигаем шкалу, иначе
    // график лежит на нижней границе и выглядит как «данных нет».
    const middle = (max + min) / 2;
    min = middle - 1;
    max = middle + 1;
  }
  const span = max - min;
  const times = usable.map((point) => point.at);
  const first = times[0];
  const last = times[times.length - 1] || first + 1;
  const scaleX = (at) => pad.left + ((at - first) / (last - first || 1)) * (width - pad.left - pad.right);
  const scaleY = (value) => pad.top + (1 - (value - min) / span) * (height - pad.top - pad.bottom);

  for (const level of [max, (max + min) / 2, min]) {
    const y = scaleY(level);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", pad.left); line.setAttribute("x2", width - pad.right);
    line.setAttribute("y1", y); line.setAttribute("y2", y);
    line.setAttribute("stroke", "var(--ir-line)"); line.setAttribute("stroke-width", "1");
    svg.appendChild(line);

    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", 4); label.setAttribute("y", y + 3.5);
    label.setAttribute("fill", "var(--ir-mut)"); label.setAttribute("font-size", "10");
    label.textContent = `${level.toFixed(1)}${unit}`;
    svg.appendChild(label);
  }

  // Подписи времени: без них непонятно, показан ли час или сутки — особенно
  // когда записей всего за несколько минут, а окно запрошено суточное.
  for (const [at, anchor] of [[first, "start"], [(first + last) / 2, "middle"], [last, "end"]]) {
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", scaleX(at).toFixed(1));
    label.setAttribute("y", height - 4);
    label.setAttribute("text-anchor", anchor);
    label.setAttribute("fill", "var(--ir-mut)");
    label.setAttribute("font-size", "10");
    label.textContent = new Date(at * 1000).toLocaleTimeString("ru-RU", {
      hour: "2-digit", minute: "2-digit",
    });
    svg.appendChild(label);
  }

  const d = usable.map((point, index) => `${index ? "L" : "M"}${scaleX(point.at).toFixed(1)},${scaleY(point.value).toFixed(1)}`).join(" ");
  if (fill) {
    const area = document.createElementNS("http://www.w3.org/2000/svg", "path");
    area.setAttribute("d", `${d} L${scaleX(last).toFixed(1)},${height - pad.bottom} L${scaleX(first).toFixed(1)},${height - pad.bottom} Z`);
    area.setAttribute("fill", color);
    area.setAttribute("opacity", "0.10");
    svg.appendChild(area);
  }
  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  line.setAttribute("d", d);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", color);
  line.setAttribute("stroke-width", "1.8");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(line);
  return svg;
}

function uptimeBar(points) {
  /** Полоса «в сети / не в сети» за сутки: отрезок на каждый интервал. */
  const width = 640;
  const height = 26;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "chart");
  svg.setAttribute("style", "height:26px");

  if (!points.length) return svg;
  const first = points[0].at;
  const last = Date.now() / 1000;
  const total = last - first || 1;

  points.forEach((point, index) => {
    const next = points[index + 1] ? points[index + 1].at : last;
    const x = ((point.at - first) / total) * width;
    const w = Math.max(1, ((next - point.at) / total) * width);
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", x.toFixed(1));
    rect.setAttribute("y", "6");
    rect.setAttribute("width", w.toFixed(1));
    rect.setAttribute("height", "14");
    rect.setAttribute("rx", "2");
    rect.setAttribute("fill", point.online ? "var(--ir-ok)" : "var(--ir-bad)");
    svg.appendChild(rect);
  });
  return svg;
}

// ============================================================== панель ==
class BmsIrPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = "overview";
    this._tab = "status";
    this._host = null;
    this._data = { hubs: [], totals: {} };
    this._detail = null;
    this._history = {};
    this._areas = [];
    this._add = null;
    this._busy = false;
    this._toast = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._start();
  }

  set narrow(_narrow) {}
  set route(_route) {}
  set panel(_panel) {}

  connectedCallback() {
    if (this._hass && !this._timer) this._start();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
  }

  _start() {
    this._render();
    this._refresh();
    clearInterval(this._timer);
    this._timer = setInterval(() => this._refresh(), POLL_MS);
    this._call("areas").then((result) => { this._areas = result.areas || []; }).catch(() => {});
  }

  _call(command, payload = {}) {
    return this._hass.callWS({ type: `bms_smart_ir/${command}`, ...payload });
  }

  async _refresh() {
    try {
      this._data = await this._call("overview");
      if (this._view === "device" && this._host) {
        this._detail = await this._call("hub", { host: this._host });
      }
      // Опрос каждые 6 секунд не имеет права стереть то, что человек
      // печатает: пока фокус в поле, перерисовку откладываем.
      if (!this._typing()) this._render();
    } catch (err) {
      this._note(err.message || "Не удалось получить данные", "bad");
    }
  }

  _typing() {
    const active = this.shadowRoot.activeElement;
    return !!active && ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName);
  }

  _note(message, kind = "") {
    // Когда сам Home Assistant переподключается, он показывает свою плашку —
    // дублировать её своей ошибкой незачем.
    const connection = this._hass && this._hass.connection;
    if (kind === "bad" && connection && connection.connected === false) return;
    this._toast = { message, kind };
    this._render();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => { this._toast = null; this._render(); }, 4500);
  }

  _go(view, host = null) {
    this._view = view;
    this._host = host || this._host;
    if (view === "device") { this._tab = "status"; this._detail = null; this._history = {}; }
    if (view === "add") this._add = { step: 1, device_type: "climate", found: [] };
    this._render();
    if (view === "device") this._refresh();
  }

  // ---------------------------------------------------------- рендер --
  _render() {
    const root = this.shadowRoot;
    root.textContent = "";
    root.appendChild(h("style", { text: CSS }));

    const views = {
      overview: () => this._viewOverview(),
      map: () => this._viewMap(),
      device: () => this._viewDevice(),
      add: () => this._viewAdd(),
    };
    const main = h("div", { class: "main" }, [(views[this._view] || views.overview)()]);
    root.appendChild(h("div", { class: "wrap" }, [this._nav(), main]));

    if (this._toast) {
      root.appendChild(h("div", { class: `toast ${this._toast.kind}`, text: this._toast.message }));
    }
  }

  _nav() {
    const totals = this._data.totals || {};
    const button = (view, label, iconName, count) => {
      const node = h("button", { onclick: () => this._go(view) }, [
        icon(iconName, 17),
        h("span", { text: label }),
        count !== undefined ? h("span", { class: "count", text: String(count) }) : null,
      ]);
      if (this._view === view) node.setAttribute("aria-current", "page");
      return node;
    };
    return h("div", { class: "nav" }, [
      h("div", { class: "brand" }, [
        icon("wifi", 22, "var(--ir-accent)"),
        h("div", {}, [h("b", { text: "BMS ИК-пульты" }), h("span", { text: "Broadlink" })]),
      ]),
      button("overview", "Обзор", "grid", totals.hubs),
      button("map", "Карта связей", "map"),
      button("add", "Добавить", "plus"),
      this._view === "device" ? button("device", "Устройство", "chip") : null,
    ]);
  }

  // ------------------------------------------------------------ обзор --
  _viewOverview() {
    const totals = this._data.totals || {};
    const hubs = this._data.hubs || [];

    const tiles = h("div", { class: "tiles" }, [
      this._tile("Broadlink всего", totals.hubs ?? 0),
      this._tile("В сети", `${totals.online ?? 0}`, `из ${totals.hubs ?? 0}`),
      this._tile("Приборов подключено", totals.appliances ?? 0),
      this._tile("С датчиком климата", totals.with_sensor ?? 0),
    ]);

    const body = hubs.length
      ? h("div", { class: "grid" }, hubs.map((hub) => this._hubCard(hub)))
      : h("div", { class: "list" }, [
          h("div", { class: "empty" }, [
            h("b", { text: "Пока ни одного Broadlink" }),
            h("div", { text: "Нажмите «Добавить» — панель сама найдёт устройства в сети." }),
            h("div", { style: "margin-top:16px" }, [
              h("button", { class: "act primary", text: "Найти устройства", onclick: () => this._go("add") }),
            ]),
          ]),
        ]);

    return h("div", {}, [
      h("h1", { text: "Обзор" }),
      h("div", { class: "sub", text: "ИК-передатчики Broadlink и приборы, которыми они управляют." }),
      tiles,
      body,
    ]);
  }

  _tile(label, value, small) {
    return h("div", { class: "tile" }, [
      h("div", { class: "k", text: label }),
      h("div", { class: "v" }, [String(value), small ? h("small", { text: ` ${small}` }) : null]),
    ]);
  }

  _hubCard(hub) {
    const sensors = hub.sensors || {};
    const card = h("div", { class: "card", onclick: () => this._go("device", hub.hub_id) }, [
      h("div", { class: "row" }, [
        h("div", {}, [
          h("h3", { text: hub.name || `Broadlink ${hub.host}` }),
          h("div", { class: "addr", text: `${hub.host}${hub.port && hub.port !== 80 ? ":" + hub.port : ""} · ${hub.model || "—"}` }),
        ]),
        h("div", { class: "spacer" }),
        statusPill(hub.status),
      ]),
      h("div", { class: "meta" }, [
        h("div", {}, [
          h("b", { text: String((hub.appliances || []).length) }),
          plural((hub.appliances || []).length, DEVICES_FORMS),
        ]),
        sensors.temperature !== undefined
          ? h("div", {}, [h("b", { text: `${sensors.temperature.toFixed(1)} °C` }), "температура"])
          : null,
        sensors.humidity !== undefined
          ? h("div", {}, [h("b", { text: `${Math.round(sensors.humidity)} %` }), "влажность"])
          : null,
        h("div", {}, [h("b", { text: String(hub.stats?.sent ?? 0) }), "команд"]),
        hub.stats?.failed
          ? h("div", {}, [h("b", { text: String(hub.stats.failed), style: "color:var(--ir-bad)" }), "потеряно"])
          : null,
      ]),
    ]);
    return card;
  }

  // ------------------------------------------------------------ карта --
  _viewMap() {
    const hubs = this._data.hubs || [];
    return h("div", {}, [
      h("h1", { text: "Карта связей" }),
      h("div", { class: "sub", text: "Что к какому передатчику привязано." }),
      hubs.length
        ? h("div", { class: "map" }, hubs.map((hub) => h("div", { class: "hub-row" }, [
            h("div", { class: "hub-side" }, [
              h("div", { class: "row" }, [icon("wifi", 16), h("b", { text: hub.name || `Broadlink ${hub.host}` })]),
              h("div", { class: "addr", text: hub.hub_id }),
              h("div", { style: "margin-top:6px" }, [statusPill(hub.status)]),
            ]),
            h("div", { class: "kids" }, (hub.appliances || []).length
              ? hub.appliances.map((appliance) => h("div", { class: "kid" }, [
                  icon(appliance.device_type === "media_player" ? "tv" : "snow", 14),
                  h("span", { text: appliance.name }),
                  h("span", { style: "color:var(--ir-mut)", text: STATE_LABELS[appliance.state] || appliance.state || "—" }),
                ]))
              : [h("div", { class: "kid", style: "color:var(--ir-mut)", text: "приборов нет" })]),
          ])))
        : h("div", { class: "list" }, [h("div", { class: "empty", text: "Пока нечего показывать." })]),
    ]);
  }

  // -------------------------------------------------------- устройство --
  _viewDevice() {
    const hub = this._detail;
    if (!hub) return h("div", { class: "empty", text: "Загрузка…" });

    const tabs = h("div", { class: "tabs" }, [
      ["status", "Статус"],
      ["appliances", "Приборы"],
      ["charts", "Графики"],
      ["service", "Обслуживание"],
    ].map(([id, label]) => {
      const node = h("button", { text: label, onclick: () => { this._tab = id; this._render(); if (id === "charts") this._loadHistory(); } });
      node.setAttribute("aria-selected", String(this._tab === id));
      return node;
    }));

    const panels = {
      status: () => this._deviceStatus(hub),
      appliances: () => this._deviceAppliances(hub),
      charts: () => this._deviceCharts(hub),
      service: () => this._deviceService(hub),
    };

    return h("div", {}, [
      h("div", { class: "row", style: "margin-bottom:10px" }, [
        h("button", { class: "act", onclick: () => this._go("overview") }, [icon("back", 15), " Назад"]),
      ]),
      h("h1", { text: hub.name || `Broadlink ${hub.host}` }),
      h("div", { class: "sub" }, [
        h("span", { class: "addr", text: `${hub.host} · ${hub.model || "—"} · ${hub.mac || "MAC неизвестен"}` }),
      ]),
      h("div", {}, [statusPill(hub.status)]),
      tabs,
      (panels[this._tab] || panels.status)(),
    ]);
  }

  _deviceStatus(hub) {
    const stats = hub.stats || {};
    const sensors = hub.sensors || {};
    return h("div", {}, [
      h("div", { class: "tiles" }, [
        this._tile("Команд отправлено", stats.sent ?? 0),
        this._tile("Команд потеряно", stats.failed ?? 0),
        this._tile("Склеено повторов", stats.coalesced ?? 0),
        this._tile("Переподключений", stats.reconnects ?? 0),
      ]),
      h("div", { class: "card flat" }, [
        h("div", { class: "section-title", text: "Характеристики" }),
        h("div", { class: "meta", style: "border-top:0;padding-top:0" }, [
          h("div", {}, [h("b", { text: hub.model || "—" }), "модель"]),
          h("div", {}, [h("b", { text: hub.mac || "—" }), "MAC"]),
          h("div", {}, [h("b", { text: `${hub.host}:${hub.port}` }), "адрес"]),
          h("div", {}, [h("b", { text: hub.has_sensor ? "есть" : "нет" }), "датчик климата"]),
          h("div", {}, [h("b", { text: String(hub.queue ?? 0) }), "в очереди"]),
          h("div", {}, [h("b", { text: ago(stats.last_ok) }), "последний обмен"]),
        ]),
        sensors.temperature !== undefined || sensors.humidity !== undefined
          ? h("div", { class: "meta" }, [
              sensors.temperature !== undefined ? h("div", {}, [h("b", { text: `${sensors.temperature.toFixed(1)} °C` }), "температура"]) : null,
              sensors.humidity !== undefined ? h("div", {}, [h("b", { text: `${Math.round(sensors.humidity)} %` }), "влажность"]) : null,
            ])
          : null,
        stats.last_error
          ? h("div", { class: "meta" }, [
              h("div", {}, [h("b", { text: stats.last_error, style: "color:var(--ir-bad);font-size:12.5px" }), `последняя ошибка · ${ago(stats.last_error_at)}`]),
            ])
          : null,
      ]),
    ]);
  }

  _deviceAppliances(hub) {
    const appliances = hub.appliances || [];
    if (!appliances.length) {
      return h("div", { class: "list" }, [h("div", { class: "empty" }, [
        h("b", { text: "К этому Broadlink ничего не привязано" }),
        h("div", { text: "Добавьте кондиционер или телевизор — он будет управляться через этот передатчик." }),
        h("div", { style: "margin-top:16px" }, [
          h("button", { class: "act primary", text: "Добавить прибор", onclick: () => this._startAddFor(hub.hub_id) }),
        ]),
      ])]);
    }
    return h("div", {}, [
      h("div", { class: "list" }, appliances.map((appliance) => h("div", { class: "item" }, [
        icon(appliance.device_type === "media_player" ? "tv" : "snow", 18, "var(--ir-accent)"),
        h("div", { class: "grow" }, [
          h("div", { class: "nm", text: appliance.name }),
          h("div", { class: "sm", text: `${appliance.device_type_label} · ${appliance.manufacturer || "—"} · код ${appliance.code} · ${appliance.entity_id || "нет сущности"}` }),
        ]),
        h("span", { class: "pill", style: "background:color-mix(in srgb,var(--ir-accent) 10%,transparent);color:var(--ir-accent)", text: STATE_LABELS[appliance.state] || appliance.state || "—" }),
        h("button", { class: "act", text: "Изменить", onclick: () => this._editAppliance(appliance) }),
      ]))),
      h("div", { style: "margin-top:14px" }, [
        h("button", { class: "act primary", text: "Добавить прибор", onclick: () => this._startAddFor(hub.hub_id) }),
      ]),
    ]);
  }

  _deviceCharts(hub) {
    const history = this._history || {};
    const temperature = history.temperature || [];
    const humidity = history.humidity || [];
    // Записанная история точнее журнала в памяти: он живёт только с запуска.
    const uptime = (history.online && history.online.length)
      ? history.online
      : (hub.uptime_log || []).map((point) => ({ at: point.at, online: point.online }));

    return h("div", {}, [
      h("div", { class: "card flat" }, [
        h("div", { class: "section-title", text: `Температура за ${HISTORY_HOURS} ч` }),
        lineChart(temperature, { unit: " °C", color: "var(--ir-accent)" }),
      ]),
      h("div", { class: "card flat", style: "margin-top:14px" }, [
        h("div", { class: "section-title", text: `Влажность за ${HISTORY_HOURS} ч` }),
        lineChart(humidity, { unit: " %", color: "var(--ir-ok)" }),
      ]),
      h("div", { class: "card flat", style: "margin-top:14px" }, [
        h("div", { class: "section-title", text: "Связь" }),
        uptime.length
          ? h("div", {}, [uptimeBar(uptime), h("div", { class: "legend" }, [
              h("span", { text: "■ в сети", style: "color:var(--ir-ok)" }),
              h("span", { text: "■ нет связи", style: "color:var(--ir-bad)" }),
            ])])
          : h("div", { class: "empty", text: "С момента запуска обрывов не было." }),
      ]),
    ]);
  }

  async _loadHistory() {
    const hub = this._detail;
    if (!hub) return;
    // Какие сущности принадлежат передатчику, говорит сервер: угадывание по
    // имени ломается, как только устройство переименовали.
    const entities = hub.entities || {};
    const wanted = [entities.temperature, entities.humidity, entities.online].filter(Boolean);
    if (!wanted.length) { this._history = {}; this._render(); return; }
    try {
      const result = await this._call("history", { entity_ids: wanted, hours: HISTORY_HOURS });
      const buckets = { temperature: [], humidity: [], online: [] };
      for (const [entityId, points] of Object.entries(result.history || {})) {
        if (entityId === entities.online) {
          buckets.online = points.map((point) => ({ at: point.at, online: point.state === "on" }));
          continue;
        }
        const bucket = entityId === entities.humidity ? "humidity" : "temperature";
        for (const point of points) {
          const value = Number.parseFloat(point.state);
          if (Number.isFinite(value)) buckets[bucket].push({ at: point.at, value });
        }
      }
      buckets.temperature.sort((a, b) => a.at - b.at);
      buckets.humidity.sort((a, b) => a.at - b.at);
      this._history = buckets;
    } catch (err) {
      this._note(err.message || "История недоступна", "bad");
    }
    this._render();
  }

  _deviceService(hub) {
    const replaceHost = h("input", {
      type: "text", placeholder: "192.168.1.51", value: this._replaceHost || "",
      oninput: (event) => { this._replaceHost = event.target.value; },
    });
    return h("div", {}, [
      h("div", { class: "card flat" }, [
        h("div", { class: "section-title", text: "Замена передатчика" }),
        h("div", { class: "sub", text: "Сломанный Broadlink меняется на новый: все кондиционеры и телевизоры остаются на месте — с теми же именами, историей и сценариями." }),
        h("label", { class: "fld" }, [h("span", { text: "Адрес нового Broadlink" }), replaceHost]),
        h("button", {
          class: "act primary",
          text: "Перенести приборы",
          onclick: async () => {
            const value = replaceHost.value.trim();
            if (!value) return;
            try {
              const result = await this._call("replace_hub", { host: hub.hub_id, new_host: value });
              this._note(`Перенесено приборов: ${result.moved}`, "ok");
              const device = result.device;
              this._go("device", device.port === 80 ? device.host : `${device.host}:${device.port}`);
            } catch (err) {
              this._note(err.message || "Не удалось перенести", "bad");
            }
          },
        }),
      ]),
    ]);
  }

  async _editAppliance(appliance) {
    const name = h("input", { type: "text", value: appliance.name });
    const area = h("select", {}, [
      h("option", { value: "", text: "— без комнаты —" }),
      ...this._areas.map((item) => {
        const option = h("option", { value: item.area_id, text: item.name });
        if (item.area_id === appliance.area_id) option.setAttribute("selected", "selected");
        return option;
      }),
    ]);
    const dialog = h("div", { class: "card flat", style: "margin-top:14px" }, [
      h("div", { class: "section-title", text: "Настройки прибора" }),
      h("label", { class: "fld" }, [h("span", { text: "Название" }), name]),
      h("label", { class: "fld" }, [h("span", { text: "Комната" }), area]),
      h("div", { class: "row" }, [
        h("button", {
          class: "act primary", text: "Сохранить",
          onclick: async () => {
            try {
              await this._call("update_appliance", {
                entry_id: appliance.entry_id,
                name: name.value.trim() || appliance.name,
                area_id: area.value || null,
              });
              this._note("Сохранено", "ok");
              this._refresh();
            } catch (err) { this._note(err.message || "Не удалось сохранить", "bad"); }
          },
        }),
        h("button", {
          class: "act danger", text: "Удалить прибор",
          onclick: async () => {
            try {
              await this._call("remove_appliance", { entry_id: appliance.entry_id });
              this._note("Прибор удалён", "ok");
              this._refresh();
            } catch (err) { this._note(err.message || "Не удалось удалить", "bad"); }
          },
        }),
      ]),
    ]);
    const main = this.shadowRoot.querySelector(".main");
    main.appendChild(dialog);
    dialog.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  _startAddFor(host) {
    this._add = { step: 2, host, device_type: "climate", found: [] };
    this._view = "add";
    this._render();
  }

  // --------------------------------------------------------- добавление --
  _viewAdd() {
    if (!this._add) this._add = { step: 1, device_type: "climate", found: [] };
    const state = this._add;
    const steps = [
      [1, "Передатчик"],
      [2, "Тип прибора"],
      [3, "Марка и модель"],
      [4, "Проверка"],
      [5, "Название"],
    ];
    return h("div", {}, [
      h("h1", { text: "Добавить прибор" }),
      h("div", { class: "sub", text: "Панель найдёт Broadlink в сети, подберёт код и проверит его на живом приборе." }),
      h("div", { class: "steps" }, steps.flatMap(([number, label], index) => {
        const node = h("div", { class: `step ${state.step >= number ? "on" : ""}` }, [
          h("span", { class: "n", text: String(number) }), label,
        ]);
        return index < steps.length - 1 ? [node, h("span", { class: "sep", text: "›" })] : [node];
      })),
      this[`_add${state.step}`](state),
    ]);
  }

  _add1(state) {
    const manual = h("input", { type: "text", placeholder: "192.168.1.50" });
    return h("div", {}, [
      h("div", { class: "card flat" }, [
        h("div", { class: "section-title", text: "Поиск в сети" }),
        h("div", { class: "row" }, [
          h("button", {
            class: "act primary", text: this._busy ? "Ищу…" : "Найти Broadlink",
            onclick: async () => {
              this._busy = true; this._render();
              try {
                const result = await this._call("discover", { timeout: 5 });
                state.found = result.devices || [];
                if (!state.found.length) this._note("Никто не ответил. Введите адрес вручную.", "bad");
              } catch (err) { this._note(err.message || "Поиск не удался", "bad"); }
              this._busy = false; this._render();
            },
          }),
          h("span", { style: "color:var(--ir-mut);font-size:12.5px", text: "Устройство должно быть в одной сети с Home Assistant." }),
        ]),
        h("div", { class: "section-title", text: "Или по адресу" }),
        h("div", { class: "row" }, [
          manual,
          h("button", {
            class: "act", text: "Проверить",
            onclick: async () => {
              const value = manual.value.trim();
              if (!value) return;
              try {
                const result = await this._call("probe", { host: value });
                state.found = result.devices || [];
              } catch (err) { this._note(err.message || "Не отвечает", "bad"); }
              this._render();
            },
          }),
        ]),
      ]),
      state.found.length
        ? h("div", { style: "margin-top:14px" }, [
            h("div", { class: "section-title", text: "Найдено" }),
            h("div", { class: "list" }, state.found.map((device) => h("div", { class: "item" }, [
              icon("wifi", 18, device.known ? "var(--ir-ok)" : "var(--ir-accent)"),
              h("div", { class: "grow" }, [
                h("div", { class: "nm", text: device.name || device.model }),
                h("div", { class: "sm", text: `${device.host}:${device.port} · ${device.model} · ${device.mac}` }),
              ]),
              device.known ? h("span", { class: "pill", style: "background:color-mix(in srgb,var(--ir-ok) 12%,transparent);color:var(--ir-ok)", text: `уже добавлен · ${device.appliances} ${plural(device.appliances, DEVICES_FORMS)}` }) : null,
              h("button", {
                class: "act primary", text: "Выбрать",
                onclick: () => { state.host = `${device.host}:${device.port}`; state.step = 2; this._render(); },
              }),
            ]))),
          ])
        : null,
    ]);
  }

  _add2(state) {
    const choose = (type) => { state.device_type = type; state.step = 3; state.catalog = null; this._render(); this._loadCatalog(); };
    return h("div", { class: "grid" }, [
      h("div", { class: "card", onclick: () => choose("climate") }, [
        h("div", { class: "row" }, [icon("snow", 22, "var(--ir-accent)"), h("h3", { text: "Кондиционер" })]),
        h("div", { class: "sub", style: "margin:8px 0 0", text: "Режимы, температура, скорость вентилятора, жалюзи." }),
      ]),
      h("div", { class: "card", onclick: () => choose("media_player") }, [
        h("div", { class: "row" }, [icon("tv", 22, "var(--ir-accent)"), h("h3", { text: "Телевизор" })]),
        h("div", { class: "sub", style: "margin:8px 0 0", text: "Включение, громкость, каналы, источники." }),
      ]),
    ]);
  }

  async _loadCatalog() {
    try {
      const result = await this._call("catalog", { device_type: this._add.device_type });
      this._add.catalog = result.catalog || [];
      this._render();
    } catch (err) {
      this._note(err.message || "Каталог недоступен", "bad");
    }
  }

  _add3(state) {
    if (!state.catalog) { this._loadCatalogOnce(); return h("div", { class: "empty", text: "Загружаю каталог…" }); }

    if (state.manufacturer) {
      const brand = state.catalog.find((item) => item.manufacturer === state.manufacturer);
      const models = brand ? brand.models : [];
      return h("div", {}, [
        h("div", { class: "row", style: "margin-bottom:12px" }, [
          h("button", { class: "act", onclick: () => { state.manufacturer = null; this._render(); } }, [icon("back", 15), " Другая марка"]),
          h("b", { text: state.manufacturer }),
        ]),
        h("div", { class: "list" }, models.map((model) => h("div", { class: "item" }, [
          h("div", { class: "grow" }, [
            h("div", { class: "nm", text: model.model }),
            h("div", { class: "sm", text: `код ${model.code}` }),
          ]),
          h("button", {
            class: "act primary", text: "Выбрать",
            onclick: () => { state.code = model.code; state.model = model.model; state.step = 4; this._render(); },
          }),
        ]))),
      ]);
    }

    const popular = state.catalog.filter((item) => item.popular);
    const rest = state.catalog.filter((item) => !item.popular);
    const brandButton = (item) => h("button", {
      onclick: () => { state.manufacturer = item.manufacturer; this._render(); },
    }, [
      h("span", { text: item.manufacturer }),
      h("span", { class: "hint", text: `${item.models.length} ${plural(item.models.length, MODELS_FORMS)}` }),
    ]);

    // Список перерисовывается на месте: полная перерисовка панели убивала бы
    // поле поиска вместе с фокусом на каждой букве.
    const results = h("div", {});
    const paint = (query) => {
      results.textContent = "";
      if (query) {
        const found = state.catalog.filter((item) => item.manufacturer.toLowerCase().includes(query));
        results.appendChild(found.length
          ? h("div", { class: "brands" }, found.map(brandButton))
          : h("div", { class: "empty", text: "Такой марки в базе нет" }));
        return;
      }
      results.appendChild(h("div", { class: "section-title", text: "Частые марки" }));
      results.appendChild(h("div", { class: "brands" }, popular.map(brandButton)));
      results.appendChild(h("div", { class: "section-title", text: "Все остальные" }));
      results.appendChild(h("div", { class: "brands" }, rest.map(brandButton)));
    };

    const search = h("input", {
      type: "text", placeholder: "Поиск марки…", value: state.query || "",
      oninput: (event) => {
        state.query = event.target.value.trim().toLowerCase();
        paint(state.query);
      },
    });
    paint(state.query || "");

    return h("div", {}, [
      h("div", { style: "max-width:340px;margin-bottom:16px" }, [search]),
      results,
    ]);
  }

  _loadCatalogOnce() {
    if (this._catalogLoading) return;
    this._catalogLoading = true;
    this._loadCatalog().finally(() => { this._catalogLoading = false; });
  }

  _add4(state) {
    return h("div", { class: "card flat" }, [
      h("div", { class: "section-title", text: "Проверка кода" }),
      h("div", { class: "sub", text: `${state.manufacturer} · ${state.model} · код ${state.code}` }),
      h("div", { class: "sub", text: "Направьте Broadlink на прибор и нажмите «Отправить сигнал». Кондиционер должен пикнуть и включиться, телевизор — отреагировать." }),
      h("div", { class: "row" }, [
        h("button", {
          class: "act", text: "Отправить сигнал",
          onclick: async () => {
            try {
              const result = await this._call("test_code", {
                host: state.host, device_type: state.device_type, code: state.code,
              });
              this._note(result.delivered ? "Сигнал отправлен — прибор отреагировал?" : "Команда не дошла до Broadlink", result.delivered ? "ok" : "bad");
            } catch (err) { this._note(err.message || "Не удалось отправить", "bad"); }
          },
        }),
        h("button", { class: "act primary", text: "Сработало", onclick: () => { state.step = 5; this._render(); } }),
        h("button", { class: "act", text: "Не сработало — другой код", onclick: () => { state.step = 3; this._render(); } }),
      ]),
    ]);
  }

  _add5(state) {
    const name = h("input", {
      type: "text", value: state.name || "",
      placeholder: state.device_type === "climate" ? "Кондиционер в спальне" : "Телевизор в зале",
      oninput: (event) => { state.name = event.target.value; },
    });
    const area = h("select", { onchange: (event) => { state.area = event.target.value; } }, [
      h("option", { value: "", text: "— без комнаты —" }),
      ...this._areas.map((item) => {
        const option = h("option", { value: item.area_id, text: item.name });
        if (item.area_id === state.area) option.setAttribute("selected", "selected");
        return option;
      }),
    ]);
    return h("div", { class: "card flat" }, [
      h("div", { class: "section-title", text: "Как назвать прибор" }),
      h("label", { class: "fld" }, [h("span", { text: "Название" }), name]),
      h("label", { class: "fld" }, [h("span", { text: "Комната" }), area]),
      h("button", {
        class: "act primary", text: "Добавить",
        onclick: async () => {
          const value = name.value.trim();
          if (!value) { this._note("Введите название", "bad"); return; }
          try {
            await this._call("add_appliance", {
              host: state.host,
              device_type: state.device_type,
              code: state.code,
              name: value,
              manufacturer: state.manufacturer,
              model: state.model,
              area_id: area.value || null,
            });
            this._note("Прибор добавлен", "ok");
            this._add = null;
            this._go("device", state.host);
          } catch (err) {
            this._note(err.message || "Не удалось добавить", "bad");
          }
        },
      }),
    ]);
  }
}

customElements.define("bms-ir-panel", BmsIrPanel);
