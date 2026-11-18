// Швидка перевірка логіки board.html на підробленому DOM (запуск: node tests/board_js_check.js).
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const html = fs.readFileSync(path.join(__dirname, "..", "bot", "web", "board.html"), "utf8");
const source = html.match(/<script>([\s\S]*)<\/script>/)[1];

function el() {
  return {
    textContent: "", className: "", style: {}, children: [],
    classList: {
      set: new Set(),
      add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); },
      contains(c) { return this.set.has(c); },
    },
    appendChild(c) { this.children.push(c); },
  };
}
// textContent = "" очищає дітей, як у браузері
function withClear(e) {
  let text = "";
  Object.defineProperty(e, "textContent", {
    get() { return text; },
    set(v) { text = String(v); if (text === "") e.children = []; }, // як у браузері: завжди рядок
  });
  return e;
}

const els = {};
const document = {
  title: "",
  getElementById(id) { return els[id] || (els[id] = withClear(el())); },
  createElement() { return withClear(el()); },
};
const timers = [];
let fetchResult = () => new Promise(() => {}); // перший poll при завантаженні: відповіді ще нема
const sandbox = {
  document, location: { search: "?token=abc%20def" },
  URLSearchParams, encodeURIComponent, Date, Math, String,
  setInterval(fn, ms) { timers.push([fn, ms]); },
  fetch(url) { sandbox.lastUrl = url; return fetchResult(); },
};
new Function(...Object.keys(sandbox), source)(...Object.values(sandbox));

(async () => {
  assert.deepStrictEqual(timers.map((t) => t[1]).sort((a, b) => a - b), [1000, 3000]);
  assert.ok(sandbox.lastUrl.includes("token=abc%20def"), "токен береться з URL і екранується");

  const now = Date.now();
  const payload = {
    title: "IT-квест", phase: "RUNNING", registered: 12, playing: 5, finished: 7, total_questions: 55,
    closes_at: new Date(now + 125000).toISOString(), server_time: new Date(now).toISOString(),
    top: [
      { place: 1, name: "<img src=x onerror=alert(1)> П.", score: 50, time: "12:34.5" },
      { place: 2, name: "Оля К.", score: 49, time: "11:00.0" },
    ],
  };
  fetchResult = () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
  // poll за інтервалом 3000
  timers.find((t) => t[1] === 3000)[0]();
  await new Promise((r) => setTimeout(r, 20));

  assert.strictEqual(els.registered.textContent, "12");
  assert.strictEqual(els.playing.textContent, "5");
  assert.strictEqual(els.finished.textContent, "7");
  assert.strictEqual(els.phase.textContent, "Квест іде");
  assert.ok(els.phase.className.includes("live"));
  assert.strictEqual(els.top.children.length, 2);
  const firstName = els.top.children[0].children[1];
  assert.strictEqual(firstName.textContent, payload.top[0].name, "ім'я лишається текстом, а не HTML");
  assert.strictEqual(els.top.children[0].children[2].textContent, "50/55");
  assert.strictEqual(els.empty.style.display, "none");
  assert.ok(!els.timer.classList.contains("hidden"));
  assert.ok(/^0[12]:\d\d$/.test(els.left.textContent), "таймер " + els.left.textContent);

  // порожній топ і закритий квест: підказка видна, таймер прихований
  payload.top = []; payload.phase = "CLOSED";
  timers.find((t) => t[1] === 3000)[0]();
  await new Promise((r) => setTimeout(r, 20));
  assert.strictEqual(els.empty.style.display, "block");
  assert.ok(els.timer.classList.contains("hidden"));
  assert.strictEqual(els.phase.textContent, "Квест закрито");

  // збій мережі: показуємо індикатор, дані не зникають
  fetchResult = () => Promise.reject(new Error("offline"));
  timers.find((t) => t[1] === 3000)[0]();
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(els.offline.classList.contains("show"));
  assert.strictEqual(els.registered.textContent, "12");

  // 403 теж вважається збоєм
  fetchResult = () => Promise.resolve({ ok: false, status: 403 });
  timers.find((t) => t[1] === 3000)[0]();
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(els.offline.classList.contains("show"));
  console.log("board.html JS: OK");
})().catch((e) => { console.error(e); process.exit(1); });
