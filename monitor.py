#!/usr/bin/env python3
"""Мониторинг свободных окон на практику (B-механика, 1 час) в автошколе Антарес.

Читает расписание, ничего не бронирует. О новых окнах пишет в Telegram.
Настройки берутся из переменных окружения (в GitHub это Secrets / workflow env).
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ROOT = "https://practice.avtoshkolaantares.ua/"
BASE = "https://practice.avtoshkolaantares.ua/StudBooking/StudBookingView"
TZ = ZoneInfo("Europe/Kyiv")

EMAIL = os.environ.get("SITE_EMAIL", "")
PASSWORD = os.environ.get("SITE_PASSWORD", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "14"))
DEBUG = os.environ.get("DEBUG") == "1"
TEST = os.environ.get("TEST") == "1"

# Порядок = приоритет. (фрагмент текста в списке адресов, как показывать в сообщении)
ADDRESSES = [
    ("Сінна", "вул. Сінна, 1  ⭐"),
    ("Вишневецького", "вул. Байди Вишневецького, 34"),
    ("Перемоги", "пр. Перемоги, 25/2"),
]
# Фрагменты текста нужной категории (оба должны встретиться)
CATEGORY_PARTS = ("механіка", "1 година")

STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})")


LOGBUF = []
API_LOGGED = 0


def log(*a):
    print(*a, flush=True)
    LOGBUF.append(" ".join(str(x) for x in a))


def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram не настроен, сообщение не отправлено:\n" + text)
        return
    data = urllib.parse.urlencode(
        {"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"slots": [], "heartbeat": "", "last_error_notify": ""}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), "utf-8"
    )


# ---------- работа со страницей ----------

def not_logged_in(page):
    """Незалогиненного сайт кидает на /Auth/Login (GET там даёт 405)."""
    path = urllib.parse.urlparse(page.url).path.lower()
    return "/auth/login" in path or "405" in (page.title() or "")


def login(page):
    # Форма входа находится на главной странице сайта, а не на странице записи
    page.goto(ROOT, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    pwd = page.locator("input[type=password]:visible").first
    if pwd.count() > 0:
        form = pwd.locator("xpath=ancestor::form[1]")
        scope = form if form.count() else page
        user = scope.locator(
            "input[type=email], input[name*=mail i], input[name*=login i], "
            "input[name*=user i], input[type=text], input:not([type])"
        ).first
        user.fill(EMAIL)
        pwd.fill(PASSWORD)
        btn = scope.locator("button[type=submit], input[type=submit], button:not([type])").first
        if btn.count():
            btn.click()
        else:
            pwd.press("Enter")
        # вход идёт асинхронно: ждём, пока форма исчезнет (или появится ошибка)
        try:
            page.wait_for_selector("input[type=password]", state="hidden", timeout=20000)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        if page.locator("input[type=password]:visible").count() > 0:
            raise RuntimeError("Не удалось войти: проверьте email/пароль (или на сайте появилась капча)")
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    if not_logged_in(page):
        raise RuntimeError("Вход не выполнен: сайт возвращает на страницу входа")


JS_DUMP = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const items = [...document.querySelectorAll('button, a, input, select')].filter(vis).slice(0, 40)
    .map(e => e.tagName.toLowerCase() + (e.type ? '[' + e.type + ']' : '') + ':' +
      (e.tagName === 'INPUT' && !['submit', 'button', 'checkbox', 'radio'].includes(e.type)
        ? (e.placeholder || e.name || '')   // значения полей никогда не печатаем
        : (e.innerText || e.value || e.name || e.placeholder || '')).trim().slice(0, 40));
  return {title: document.title, text: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 700), items};
}"""


def dump_page(page, label):
    """Для отладки: что сейчас на странице (адрес, заголовок, текст, кнопки)."""
    try:
        info = page.evaluate(JS_DUMP)
        log(f"--- {label} ---")
        log("url:", urllib.parse.urlparse(page.url).path)
        log("title:", info["title"])
        log("text:", info["text"])
        log("elements:", " | ".join(info["items"]))
    except Exception as e:  # noqa: BLE001
        log("dump_page не удалось:", e)


def open_booking_form(page):
    if page.locator("select").count() >= 2:
        return
    try:
        page.get_by_text("Забронювати", exact=False).first.wait_for(timeout=20000)
    except Exception:
        pass
    btn = page.get_by_role("button", name=re.compile("Забронювати")).first
    if btn.count() == 0:
        btn = page.get_by_text("Забронювати", exact=False).first
    btn.click()
    page.wait_for_selector("select", timeout=15000)
    page.wait_for_load_state("networkidle")


def find_selects(page):
    """Определяем <select> адреса и категории по их содержимому."""
    sels = page.locator("select")
    addr = cat = None
    for i in range(sels.count()):
        opts = sels.nth(i).locator("option").evaluate_all(
            "els => els.map(e => e.textContent)"
        )
        joined = " | ".join(opts).lower()
        if any(k.lower() in joined for k, _ in ADDRESSES):
            addr = sels.nth(i)
        elif "механіка" in joined or "автомат" in joined:
            cat = sels.nth(i)
    if addr is None or cat is None:
        raise RuntimeError("Не нашёл выпадающие списки адреса/категории (вёрстка изменилась?)")
    return addr, cat


JS_PICK = """(sel, needles) => {
  const opt = [...sel.options].find(o => {
    const t = o.textContent.toLowerCase();
    return needles.every(n => t.includes(n.toLowerCase()));
  });
  if (!opt) return null;
  sel.value = opt.value;
  sel.dispatchEvent(new Event('input', {bubbles: true}));
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return opt.textContent.trim();
}"""


def pick(select_loc, needles):
    return select_loc.evaluate(JS_PICK, list(needles))


JS_SET_DATE = """(s) => {
  const [dd, mm, yy] = s.split('.');
  const d = new Date(+yy, +mm - 1, +dd);
  const fp = [...document.querySelectorAll('input')].find(i => i._flatpickr);
  if (fp) { fp._flatpickr.setDate(d, true); return 'flatpickr'; }
  const inp = [...document.querySelectorAll('input')].find(
    i => /^\\d{2}\\.\\d{2}\\.\\d{4}$/.test(i.value));
  if (!inp) return 'noinput';
  inp.value = s;
  inp.dispatchEvent(new Event('input', {bubbles: true}));
  inp.dispatchEvent(new Event('change', {bubbles: true}));
  return 'plain';
}"""

JS_READ_RESULTS = """() => {
  // Заголовок таблицы результатов («Час / Уроки») в вёрстке приходит в обычном регистре,
  // а капсом он только из-за CSS, поэтому сравниваем без учёта регистра.
  const norm = s => s.replace(/\\s+/g, ' ').trim().toUpperCase();
  const h = [...document.querySelectorAll('th,td,div,span,p,h3,h4')].find(
    e => e.children.length === 0 && norm(e.textContent) === 'УРОКИ');
  if (!h) return {found: false, text: '', rows: []};
  let n = h;
  for (let i = 0; i < 6 && n.parentElement; i++) {
    n = n.parentElement;
    if (n.tagName === 'TABLE' || (n.innerText.toUpperCase().includes('ЧАС') && n.innerText.length > 40)) break;
  }
  const rows = [...n.querySelectorAll('tr')].map(tr => ({
    t: tr.innerText.replace(/\\s+/g, ' ').trim(),
    i: !!tr.querySelector('button, a, input[type=checkbox], input[type=radio]'),
  }));
  return {found: true, text: n.innerText, rows};
}"""


def set_date(page, d):
    s = d.strftime("%d.%m.%Y")
    how = page.evaluate(JS_SET_DATE, s)
    if how == "noinput":
        raise RuntimeError("Не нашёл поле даты")
    return s


def show_schedule(page, date_str):
    before = page.evaluate(JS_READ_RESULTS)["text"]
    btn = page.get_by_role("button", name=re.compile("Відобразити розклад")).first
    if btn.count() == 0:
        btn = page.get_by_text("Відобразити розклад").first
    clicked = False
    api = ""
    try:
        with page.expect_response(lambda r: "GetLessons" in r.url, timeout=15000) as ri:
            btn.click()
            clicked = True
        api = ri.value.text()
    except Exception:
        if not clicked:
            raise
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    # ждём, пока таблица обновится
    deadline = time.time() + 8
    res = page.evaluate(JS_READ_RESULTS)
    while time.time() < deadline:
        res = page.evaluate(JS_READ_RESULTS)
        if res["found"] and res["text"] != before and "Оберіть дату" not in res["text"]:
            break
        time.sleep(0.4)
    res["api"] = api
    return res


def parse_slots(res):
    if not res.get("found"):
        # лучше громко упасть, чем принять за окна что-то постороннее на странице
        raise RuntimeError("не нашёл таблицу расписания («Час / Уроки») на странице")
    text = res["text"]
    if "відсутні" in text.lower() and not TIME_RE.search(text):
        return []
    rows = [r for r in res["rows"] if TIME_RE.search(r["t"])]
    if any(r["i"] for r in rows):  # если есть кнопки, берём только строки с кнопкой
        rows = [r for r in rows if r["i"]]
    slots = []
    for r in rows:
        m = TIME_RE.search(r["t"])
        slots.append(f"{m.group(1)}–{m.group(2)}")
    if not rows:  # таблицы нет, но времена в тексте есть
        slots = [f"{a}–{b}" for a, b in TIME_RE.findall(text)]
    return sorted(set(slots))


def scan(page, state_prev):
    """Возвращает (найденные слоты как dict, адреса со сбоями)."""
    found = {}
    failed = set()
    today = dt.datetime.now(TZ).date()
    dates = [today + dt.timedelta(days=i) for i in range(DAYS_AHEAD)]

    for needle, title in ADDRESSES:
        try:
            addr_sel, _ = find_selects(page)
            chosen = pick(addr_sel, [needle])
            if not chosen:
                raise RuntimeError(f"адрес '{needle}' не найден в списке")
            page.wait_for_load_state("networkidle")
            time.sleep(0.7)
            _, cat_sel = find_selects(page)  # список категорий мог обновиться
            if not pick(cat_sel, CATEGORY_PARTS):
                log(f"[{title}] категория 'B-механика 1 час' недоступна на этом адресе")
                continue
            time.sleep(0.5)
            for d in dates:
                ds = set_date(page, d)
                res = show_schedule(page, ds)
                slots = parse_slots(res)
                api = (res.get("api") or "").strip()
                if not slots and api not in ("", "[]", "{}", "null"):
                    # сервер вернул данные, но разобрать таблицу не вышло: лучше лишний раз сообщить
                    slots = ["окно есть (формат не распознан, откройте сайт)"]
                if DEBUG:
                    log(f"[{title}] {ds}: {slots}")
                    global API_LOGGED
                    if (slots or d == today) and API_LOGGED < 4:
                        API_LOGGED += 1
                        log("   таблица:", res["text"][:200].replace("\n", " / "))
                        log("   ответ GetLessons:", (res.get("api") or "")[:700])
                for s in slots:
                    found[f"{needle}|{d.isoformat()}|{s}"] = (title, d, s)
                time.sleep(0.6)
            log(f"[{title}] проверено дней: {len(dates)}")
        except Exception as e:  # noqa: BLE001
            log(f"[{title}] ОШИБКА: {e}")
            failed.add(needle)
            if DEBUG:
                DEBUG_DIR.mkdir(exist_ok=True)
                page.screenshot(path=str(DEBUG_DIR / f"error_{needle}.png"), full_page=True)
    return found, failed


def format_message(new_items):
    by_addr = {}
    for key, (title, d, s) in new_items.items():
        by_addr.setdefault(title, {}).setdefault(d, []).append(s)
    order = [t for _, t in ADDRESSES]
    lines = ["🚗 Появились окна: практика B-механика, 1 час", ""]
    for title in order:
        if title not in by_addr:
            continue
        lines.append(f"📍 {title}")
        for d in sorted(by_addr[title]):
            times = ", ".join(sorted(by_addr[title][d]))
            lines.append(f"  {WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}: {times}")
        lines.append("")
    lines.append(BASE)
    return "\n".join(lines)


def main():
    """Запускает проверку; в режиме DEBUG присылает весь лог в Telegram (в ваш личный чат с ботом)."""
    code = 1
    try:
        code = run()
    finally:
        if DEBUG and TG_TOKEN and TG_CHAT:
            text = "\n".join(LOGBUF) or "(лог пуст)"
            try:
                for i in range(0, len(text), 3800):
                    tg_send("🛠 debug-лог:\n" + text[i:i + 3800])
            except Exception as e:  # noqa: BLE001
                print(f"Не удалось отправить debug-лог: {e}", flush=True)
    return code


def run():
    if TEST:
        tg_send("✅ Тест: бот мониторинга окон на связи.")
        log("Тестовое сообщение отправлено")
        return 0
    if not (EMAIL and PASSWORD):
        log("Не заданы SITE_EMAIL / SITE_PASSWORD")
        return 2

    state = load_state()
    prev_keys = set(state.get("slots", []))
    now = dt.datetime.now(TZ)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="uk-UA", timezone_id="Europe/Kyiv",
                                  viewport={"width": 430, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(15000)
        if DEBUG:
            DEBUG_DIR.mkdir(exist_ok=True)

            def on_resp(r):
                if r.request.resource_type in ("xhr", "fetch") or r.request.method == "POST":
                    path = urllib.parse.urlparse(r.url).path
                    log(f"   net: {r.request.method} {path} -> {r.status}")

            page.on("response", on_resp)
        try:
            login(page)
            open_booking_form(page)
            if DEBUG:
                page.screenshot(path=str(DEBUG_DIR / "form.png"), full_page=True)
                (DEBUG_DIR / "form.html").write_text(page.content(), "utf-8")
            found, failed = scan(page, prev_keys)
        except Exception as e:  # noqa: BLE001
            log(f"ФАТАЛЬНАЯ ОШИБКА: {e}")
            if DEBUG:
                dump_page(page, "страница при ошибке")
                page.screenshot(path=str(DEBUG_DIR / "fatal.png"), full_page=True)
                (DEBUG_DIR / "fatal.html").write_text(page.content(), "utf-8")
            last = state.get("last_error_notify", "")
            if not last or (now - dt.datetime.fromisoformat(last)).total_seconds() > 12 * 3600:
                try:
                    tg_send(f"⚠️ Мониторинг окон сломался: {e}")
                    state["last_error_notify"] = now.isoformat(timespec="seconds")
                    save_state(state)
                except Exception as e2:  # noqa: BLE001
                    log(f"Не удалось отправить в Telegram: {e2}")
            browser.close()
            return 1
        browser.close()

    # состояние: новые = то, чего не было в прошлый раз
    cur_keys = set(found)
    # для адресов со сбоем сохраняем прежние записи, чтобы не слать дубли
    for k in prev_keys:
        if k.split("|", 1)[0] in failed:
            cur_keys.add(k)
    new_keys = {k: found[k] for k in found if k not in prev_keys}

    log(f"Найдено окон: {len(found)}, новых: {len(new_keys)}")
    if new_keys:
        tg_send(format_message(new_keys))
        log("Уведомление отправлено")

    state["slots"] = sorted(cur_keys)
    # раз в ~10 дней обновляем отметку, чтобы в репозитории был коммит (иначе cron отключится через 60 дней)
    hb = state.get("heartbeat", "")
    today_s = now.date().isoformat()
    if not hb or (now.date() - dt.date.fromisoformat(hb)).days >= 10:
        state["heartbeat"] = today_s
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
