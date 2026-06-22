"""
Модуль анализа прогрева: статистика Телеграм-канала, продажи, бублик выполнения плана.

Флоу:
1. /warmup или кнопка → меню
2. «Добавить статистику» → собираем ВСЕ скрины пачкой (несколько фото)
3. Пользователь пишет «готово» → бот читает все скрины вместе, группирует по датам
4. Для каждой даты (по очереди) спрашивает: кол-во продаж → сумму → комиссию
5. Сохраняет каждую дату отдельной строкой → генерирует бублик
"""
import io
import json
import logging
import datetime
import numpy as np
import re
import base64
import aiohttp
from collections import defaultdict
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import plotly.graph_objects as go

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command

logger = logging.getLogger(__name__)

warmup_router = Router()

_claude = None
_gc = None
_bot = None
_owner_id: int = 0
_spreadsheet_id: str = ""
_model: str = "claude-haiku-4-5-20251001"
_gemini_key: str = ""
_gemini_model: str = "gemini-2.5-flash"

warmup_states: dict[int, dict] = {}

SHEET_WARMUPS = "Прогревы"
SHEET_WARMUP_DAYS = "Прогрев_дни"


def init_warmup(claude, gc, bot, owner_id: int, spreadsheet_id: str, model: str, gemini_key: str = ""):
    global _claude, _gc, _bot, _owner_id, _spreadsheet_id, _model, _gemini_key
    _claude = claude; _gc = gc; _bot = bot
    _owner_id = owner_id; _spreadsheet_id = spreadsheet_id; _model = model
    _gemini_key = gemini_key
    _ensure_sheets()
    logger.info(f"warmup_module: init (owner={owner_id})")


def _ensure_sheets():
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        titles = [ws.title for ws in ss.worksheets()]
        if SHEET_WARMUPS not in titles:
            ws = ss.add_worksheet(SHEET_WARMUPS, rows=200, cols=10)
            ws.append_row(["ID", "Название", "Дата старта", "План сумма", "План кол-во", "Подписчики", "Статус"])
        if SHEET_WARMUP_DAYS not in titles:
            ws = ss.add_worksheet(SHEET_WARMUP_DAYS, rows=1000, cols=13)
            ws.append_row(["ID прогрева", "Название прогрева", "Дата",
                           "Постов", "Сумма охватов", "Ср.охват", "Лайки", "Репосты", "Вовлечённость%",
                           "Продажи шт", "Продажи руб", "Комиссия руб", "% плана (нараст.)"])
        else:
            # Обновляем заголовок если нет "Сумма охватов" (старый лист)
            ws = ss.worksheet(SHEET_WARMUP_DAYS)
            headers = ws.row_values(1)
            if "Сумма охватов" not in headers:
                # Вставляем столбец после "Постов" (позиция 5 = индекс 4)
                ws.insert_cols([[]], col=5)
                ws.update_cell(1, 5, "Сумма охватов")
                logger.info("warmup: добавлен столбец 'Сумма охватов'")
    except Exception as e:
        logger.error(f"warmup _ensure_sheets: {e}")


def _get_warmups(only_active=True) -> list[dict]:
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        ws = ss.worksheet(SHEET_WARMUPS)
        rows = ws.get_all_records()
        return [r for r in rows if not only_active or r.get("Статус") == "активный"]
    except Exception as e:
        logger.error(f"warmup _get_warmups: {e}"); return []


def _create_warmup(name: str, plan_sum: float, plan_count: int, subscribers: int = 0) -> str:
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        ws = ss.worksheet(SHEET_WARMUPS)
        wid = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        ws.append_row([wid, name, datetime.date.today().strftime("%d.%m.%Y"), plan_sum, plan_count, subscribers, "активный"])
        return wid
    except Exception as e:
        logger.error(f"warmup _create_warmup: {e}"); return ""


def _save_day(warmup_id, warmup_name, date, posts, avg_views, likes, reposts,
              engagement, sales_count, sales_sum, commission, plan_pct, total_views: float = 0):
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        ws = ss.worksheet(SHEET_WARMUP_DAYS)
        sum_views = total_views if total_views > 0 else round(avg_views * posts, 1)
        ws.append_row([warmup_id, warmup_name, date, posts, round(sum_views, 1), round(avg_views, 1),
                       likes, reposts, round(engagement, 2),
                       sales_count, sales_sum, commission, round(plan_pct, 1)])
    except Exception as e:
        logger.error(f"warmup _save_day: {e}")


def _save_subscribers(warmup_id: str, subs: int):
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        ws = ss.worksheet(SHEET_WARMUPS)
        rows = ws.get_all_values()
        headers = rows[0]
        if "Подписчики" not in headers:
            ws.update_cell(1, len(headers) + 1, "Подписчики")
            headers.append("Подписчики")
        col = headers.index("Подписчики") + 1
        for i, row in enumerate(rows[1:], start=2):
            if row and str(row[0]) == str(warmup_id):
                ws.update_cell(i, col, subs); break
    except Exception as e:
        logger.error(f"warmup _save_subscribers: {e}")


def _delete_days_by_dates(warmup_id: str, dates: set):
    """Удаляет строки по warmup_id + конкретным датам (чтобы не было дублей)."""
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        ws = ss.worksheet(SHEET_WARMUP_DAYS)
        all_rows = ws.get_all_values()
        to_delete = [
            i + 2 for i, row in enumerate(all_rows[1:])
            if row and str(row[0]) == str(warmup_id) and row[2] in dates
        ]
        for row_idx in reversed(to_delete):
            ws.delete_rows(row_idx)
        if to_delete:
            logger.info(f"warmup: удалено {len(to_delete)} старых строк для дат {dates}")
    except Exception as e:
        logger.error(f"warmup _delete_days_by_dates: {e}")


def _get_warmup_days(warmup_id: str) -> list[dict]:
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        ws = ss.worksheet(SHEET_WARMUP_DAYS)
        return [r for r in ws.get_all_records() if str(r.get("ID прогрева")) == str(warmup_id)]
    except Exception as e:
        logger.error(f"warmup _get_warmup_days: {e}"); return []


# ── Парсинг скринов ────────────────────────────────────────────────

def _parse_stats_images(images_bytes: list[bytes], subscribers: int = 0) -> dict[str, dict]:
    """Читает ВСЕ скрины за раз через Gemini Vision."""
    import asyncio

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    today_str = today.strftime("%d.%m.%y")
    yesterday_str = yesterday.strftime("%d.%m.%y")

    date_hint = (f'ДАТЫ: сегодня {today_str}. Если в скрине написано "сегодня" → дата {today_str}. '
                 f'Если написано "вчера" → дата {yesterday_str}.')

    prompt_text = """Перед тобой скриншоты раздела статистики Telegram-канала (Statistics → Posts).

""" + date_hint + """

ЗАДАЧА: для каждого поста выпиши 5 цифр. Потом сгруппируй по датам.

ШАГ 1 — читай каждый пост по одному сверху вниз:

Каждый пост выглядит так:
  [превью текста]   NNN просмотров
  ДД.ММ.ГГ в ЧЧ:ММ   ♡ N   ↩ N

Для каждого поста:
  - ВРЕМЯ: из строки "ДД.ММ.ГГ в ЧЧ:ММ" — только "ЧЧ:ММ"
  - ДАТА: из той же строки — только "ДД.ММ.ГГ"
  - ПРОСМОТРЫ: число перед словом "просмотров" или "views"
  - ЛАЙКИ: число после иконки-сердечка ♡. Если сердечка НЕТ → 0
  - РЕПОСТЫ: число после иконки-стрелки ↩. Если стрелки НЕТ → 0

ВАЖНО про иконки:
  - Иконка ♡ (сердечко) = ЛАЙКИ
  - Иконка ↩ (стрелка пересылки) = РЕПОСТЫ
  - Иконки могут ОТСУТСТВОВАТЬ — это значит 0, не придумывай числа
  - Порядок иконок: сначала ♡ лайки, потом ↩ репосты
  - Если видишь только одну иконку — определи по виду что это: сердечко или стрелка

ШАГ 2 — дедупликация:
  Если один и тот же пост (дата + время совпадают) встречается на нескольких скринах — включи его ОДИН раз.

ШАГ 3 — верни JSON (ТОЛЬКО JSON, без текста вокруг):
{
  "посты": [
    {"дата": "ДД.ММ.ГГ", "время": "ЧЧ:ММ", "просмотры": ЧИСЛО, "лайки": ЧИСЛО, "репосты": ЧИСЛО}
  ],
  "по_датам": {
    "ДД.ММ.ГГ": {"постов": ЧИСЛО, "сумма_просмотров": ЧИСЛО, "лайки": ЧИСЛО, "репосты": ЧИСЛО}
  }
}

Лайки и репосты в "по_датам" — это СУММА соответствующих полей из "посты" для той же даты."""

    async def _call_gemini():
        parts = []
        for img in images_bytes:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.standard_b64encode(img).decode()
                }
            })
        parts.append({"text": prompt_text})
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"maxOutputTokens": 4000, "temperature": 0.1}
        }
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{_gemini_model}:generateContent?key={_gemini_key}")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=90)) as resp:
                data = await resp.json()
        if data.get("error"):
            raise RuntimeError(f"Gemini error: {data['error'].get('message','')[:200]}")
        candidates = data.get("candidates", [{}])
        return "".join(
            p.get("text", "")
            for p in candidates[0].get("content", {}).get("parts", [])
        ).strip()

    # Функция вызывается через asyncio.to_thread — новый поток, нет event loop
    text = asyncio.run(_call_gemini())
    m = re.search(r'\{[\s\S]+\}', text)
    if not m:
        raise ValueError(f"JSON не найден: {text[:300]}")
    data = json.loads(m.group())

    # Если есть список постов — пересчитываем по_датам из него (точнее чем агрегат Claude)
    raw_posts = data.get("посты", [])
    if raw_posts:
        from collections import defaultdict
        # Дедупликация по дата+время+просмотры — убираем дубли от перекрывающихся скринов.
        # Просмотры включены в ключ, т.к. два разных поста в одну минуту имеют разные просмотры.
        seen = set()
        unique_posts = []
        for p in raw_posts:
            key = (p.get("дата", ""), p.get("время", ""), int(p.get("просмотры", 0)))
            if key not in seen:
                seen.add(key)
                unique_posts.append(p)
        agg = defaultdict(lambda: {"постов": 0, "сумма_просмотров": 0, "лайки": 0, "репосты": 0})
        for p in unique_posts:
            dt = p.get("дата", "")
            agg[dt]["постов"] += 1
            agg[dt]["сумма_просмотров"] += float(p.get("просмотры", 0))
            agg[dt]["лайки"] += int(p.get("лайки", 0))
            agg[dt]["репосты"] += int(p.get("репосты", 0))
        by_date_src = dict(agg)
    else:
        by_date_src = data.get("по_датам", {})

    result = {}
    for date_str, d in by_date_src.items():
        posts = int(d.get("постов", 0))
        total_views = float(d.get("сумма_просмотров", 0))
        avg_views = round(total_views / posts, 1) if posts > 0 else 0.0
        likes = int(d.get("лайки", 0))
        reposts = int(d.get("репосты", 0))
        engagement = (likes + reposts) / subscribers * 100 if subscribers > 0 else 0.0
        result[date_str] = {
            "posts": posts, "total_views": total_views, "avg_views": avg_views,
            "likes": likes, "reposts": reposts,
            "engagement": round(engagement, 2)
        }
    # Сортируем по дате
    def _parse_date(s):
        try:
            return datetime.datetime.strptime(s, "%d.%m.%y")
        except Exception:
            return datetime.datetime.min
    return dict(sorted(result.items(), key=lambda x: _parse_date(x[0])))


# ── Бублик ─────────────────────────────────────────────────────────

def _render_donut_pil(pct_sum: float, grad_stops, p_empty: str, bg: str,
                      size: int = 600, ssaa: int = 4):
    """Pixel-perfect donut: smooth boundary, correct torus lighting, no border."""
    from PIL import Image

    S = size * ssaa
    R_OUT = S * 0.43
    R_IN  = S * 0.255

    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    cx = cy = S / 2.0
    dx, dy = xx - cx, yy - cy
    r = np.sqrt(dx * dx + dy * dy)

    in_ring = (r >= R_IN) & (r <= R_OUT)
    r_norm  = np.where(in_ring, (r - R_IN) / (R_OUT - R_IN), 0.5)

    ang_cw     = (90.0 - np.degrees(np.arctan2(-dy, dx))) % 360.0
    filled_deg = pct_sum / 100.0 * 360.0

    def _hex(h): return np.array([int(h[1:3],16), int(h[3:5],16), int(h[5:7],16)]) / 255.0
    bgc = _hex(bg)
    ec  = _hex(p_empty)

    # ── Радиальное затенение: выпуклый тор, пик ~30% от внутр. края ──
    rad = np.clip(1.0 - 2.5 * (r_norm - 0.30) ** 2, 0.28, 1.35)

    # ── Угловое затенение: свет сверху → sin(math_angle) ────────────
    # sin=+1 at top (12h), sin=0 at sides, sin=-1 at bottom (6h)
    ang_math = np.degrees(np.arctan2(-dy, dx))
    sin_up   = np.sin(np.radians(ang_math))          # +1=top, -1=bottom
    ang_sh   = 0.80 + 0.28 * (sin_up * 0.5 + 0.5)  # range [0.80, 1.08]
    shading  = np.clip(rad * ang_sh, 0.0, 1.4)

    # ── Градиент для ВСЕХ пикселей кольца ────────────────────────────
    # t=0 у начала, t=1 у конца заполненной части
    t_all = np.clip(ang_cw / max(filled_deg, 1e-6), 0, 1)
    r_grad = np.zeros((S, S), np.float32)
    g_grad = np.zeros((S, S), np.float32)
    b_grad = np.zeros((S, S), np.float32)
    for i in range(len(grad_stops) - 1):
        t0, c0 = grad_stops[i]; t1, c1 = grad_stops[i + 1]
        seg = in_ring & (t_all >= t0) & (t_all <= t1)
        f   = np.where(seg, (t_all - t0) / max(t1 - t0, 1e-9), 0.0)
        r_grad += np.where(seg, c0[0]*(1-f) + c1[0]*f, 0.0)
        g_grad += np.where(seg, c0[1]*(1-f) + c1[1]*f, 0.0)
        b_grad += np.where(seg, c0[2]*(1-f) + c1[2]*f, 0.0)

    # ── Плавная граница заполненного/пустого (blend 3°) ──────────────
    FADE = 3.0
    # w=1 → полностью заполненный цвет, w=0 → полностью пустой
    w = np.where(
        in_ring,
        np.clip((filled_deg - ang_cw) / FADE + 0.5, 0.0, 1.0),
        0.0,
    )

    # Пустой цвет — тот же шейдинг что у заполненного (единый материал)
    # Чуть приглушённый, чтобы отличался но не серый кирпич
    empty_tint = ec * 0.85 + np.array([0.15, 0.12, 0.20])  # лёгкий фиолетовый подтон
    er = np.clip(empty_tint[0] * shading * 0.90, 0, 1)
    eg = np.clip(empty_tint[1] * shading * 0.90, 0, 1)
    eb = np.clip(empty_tint[2] * shading * 0.90, 0, 1)

    r_fill = np.clip(r_grad * shading, 0, 1)
    g_fill = np.clip(g_grad * shading, 0, 1)
    b_fill = np.clip(b_grad * shading, 0, 1)

    # Сборка: blend заполненного и пустого через w
    rgb = np.empty((S, S, 3), np.float32)
    rgb[..., 0] = np.where(in_ring, w*r_fill + (1-w)*er, bgc[0])
    rgb[..., 1] = np.where(in_ring, w*g_fill + (1-w)*eg, bgc[1])
    rgb[..., 2] = np.where(in_ring, w*b_fill + (1-w)*eb, bgc[2])

    img_ss = Image.fromarray((rgb * 255).clip(0, 255).astype(np.uint8), "RGB")
    img    = img_ss.resize((size, size), Image.LANCZOS)
    return np.array(img) / 255.0


def _interp_rgb(t, stops):
    """Линейная интерполяция цвета, возвращает numpy array [r,g,b] 0-1."""
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]; t1, c1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return c0 * (1 - f) + c1 * f
    return stops[-1][1]


def _interp_color(t, stops):
    """Для обратной совместимости — возвращает строку rgb(...)."""
    c = _interp_rgb(t, stops)
    return f"rgb({int(c[0]*255)},{int(c[1]*255)},{int(c[2]*255)})"


def _generate_donut(warmup_name, plan_sum, plan_count,
                    total_sum, total_count, avg_views, avg_engagement, days,
                    subscribers: int = 0) -> bytes:
    from matplotlib.patches import Wedge, FancyBboxPatch, Ellipse

    # ── Дизайн-токены ──────────────────────────────────────────────
    BG         = "#F8F7FF"   # фон всей фигуры
    CARD_BG    = "#FFFFFF"   # фон карточек
    DIVIDER    = "#EAE7F8"   # разделитель
    P_PURPLE   = "#6C3FE8"
    P_GREEN    = "#1BA87A"
    P_ORANGE   = "#E07B20"
    P_RED      = "#D63B3B"
    P_CYAN     = "#0099CC"
    P_TEXT     = "#1A1A2E"
    P_MUTED    = "#9090B8"
    P_EMPTY    = "#DDD8F8"

    grad_stops = [
        (0.0,  np.array([0x6C, 0x3F, 0xE8]) / 255),
        (0.45, np.array([0xBC, 0x3F, 0xBE]) / 255),
        (1.0,  np.array([0x00, 0xAA, 0xDD]) / 255),
    ]

    # ── Данные ─────────────────────────────────────────────────────
    pct_sum     = min(total_sum / plan_sum * 100, 100) if plan_sum > 0 else 0
    total_posts = sum(int(d.get("Постов", 0)) for d in days) if days else 0
    total_comm  = sum(float(d.get("Комиссия руб", 0)) for d in days) if days else 0
    net_profit  = total_sum - total_comm

    shown = days[-10:]
    running = 0.0; cum_pcts = []
    for d in days:
        running += float(d.get("Продажи руб", 0))
        cum_pcts.append(running / plan_sum * 100 if plan_sum > 0 else 0)
    shown_pcts = cum_pcts[-10:]

    def pct_color(v):
        return P_GREEN if v >= 100 else P_ORANGE if v >= 50 else P_RED

    # Таблица: 6 колонок (убрали Вовл.% и Прод.шт — слишком мелко на телефоне)
    TBL_COLS   = ["Дата", "Постов", "Охват", "Продаж ₽", "Комис.", "% плана"]
    # ширины в data-единицах (total = 6.0)
    TBL_CW     = [1.3, 0.7, 0.8, 1.1, 1.0, 1.1]

    rows_data, row_pct_vals = [], []
    for day, pv in zip(shown, shown_pcts):
        comm = float(day.get("Комиссия руб", 0))
        rows_data.append([
            str(day.get("Дата", "")),
            str(day.get("Постов", 0)),
            str(round(float(day.get("Ср.охват", 0)))),
            f"{int(day.get('Продажи руб', 0)):,}".replace(",", " "),
            f"{int(comm):,}".replace(",", " ") if comm else "—",
            f"{pv:.1f}%",
        ])
        row_pct_vals.append(pv)

    tot_r = sum(float(d.get("Продажи руб", 0)) for d in shown)
    tot_c = sum(float(d.get("Комиссия руб", 0)) for d in shown)
    rows_data.append([
        "ИТОГО",
        str(sum(int(d.get("Постов", 0)) for d in shown)),
        "—",
        f"{int(tot_r):,}".replace(",", " "),
        f"{int(tot_c):,}".replace(",", " ") if tot_c else "—",
        f"{shown_pcts[-1]:.1f}%" if shown_pcts else "—",
    ])
    row_pct_vals.append(None)  # итого

    # ── Размеры ────────────────────────────────────────────────────
    n_data_rows = len(rows_data)   # включая итого
    DPI       = 150
    FIG_W     = 9.6                # дюймы
    TOP_H     = 4.6                # зона бублик + карточки
    HDR_H     = 0.36               # шапка таблицы
    ROW_H     = 0.28               # строка таблицы
    TBL_H     = HDR_H + n_data_rows * ROW_H + 0.12
    TTL_H     = 0.42               # зона заголовка
    FIG_H     = TTL_H + TOP_H + TBL_H

    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=DPI, facecolor=BG)

    # Доли фигуры (bottom → top)
    tbl_bot  = 0.0
    tbl_h_f  = TBL_H / FIG_H
    top_bot  = tbl_h_f
    top_h_f  = TOP_H / FIG_H
    ttl_bot  = top_bot + top_h_f
    ttl_h_f  = TTL_H / FIG_H

    # ── ЗАГОЛОВОК ──────────────────────────────────────────────────
    ax_ttl = fig.add_axes([0.0, ttl_bot, 1.0, ttl_h_f])
    ax_ttl.set_facecolor(BG); ax_ttl.axis("off")
    ax_ttl.text(0.5, 0.72, warmup_name,
                ha="center", va="center",
                fontsize=16, fontweight="bold", color=P_TEXT)
    plan_txt = f"план  {int(plan_sum):,} ₽  ·  {plan_count} продаж".replace(",", " ")
    ax_ttl.text(0.5, 0.24, plan_txt,
                ha="center", va="center",
                fontsize=9.5, color=P_MUTED)

    # ── БУБЛИК (левая половина) ─────────────────────────────────────
    pad = 0.025
    ax_d = fig.add_axes([pad, top_bot + pad, 0.46, top_h_f - 2 * pad])
    ax_d.set_xlim(0, 1); ax_d.set_ylim(0, 1)
    # НЕ используем set_aspect: он расширяет data limits за [0,1]×[0,1],
    # image не заполняет оси целиком → зазор facecolor → тёмная обводка
    ax_d.axis("off"); ax_d.set_facecolor(BG)

    cx, cy   = 0.5, 0.5
    R_OUT    = 0.43
    R_IN     = 0.25   # толстое кольцо — визуально объёмнее

    # Тень: несколько слоёв эллипсов снизу кольца — мягкая, заметная
    # Тень: только снизу, через matplotlib Ellipse — никакого PIL-composite
    for s_alpha, s_w, s_h, s_cy in [
        (0.10, R_OUT * 2.10, R_OUT * 0.18, cy - R_OUT + 0.02),
        (0.15, R_OUT * 1.85, R_OUT * 0.12, cy - R_OUT + 0.03),
        (0.20, R_OUT * 1.60, R_OUT * 0.07, cy - R_OUT + 0.04),
    ]:
        ax_d.add_patch(Ellipse(
            (cx, s_cy), width=s_w, height=s_h,
            facecolor=(0.35, 0.18, 0.65, s_alpha),
            edgecolor="none", zorder=1,
        ))

    # PIL-бублик: size×size, непрозрачный RGB, без тени внутри
    donut_img = _render_donut_pil(pct_sum, grad_stops, P_EMPTY, BG,
                                  size=500, ssaa=4)
    ax_d.imshow(donut_img, extent=[0, 1, 0, 1],
                origin="upper", zorder=2, interpolation="none")

    # % — главный герой (поверх imshow)
    ax_d.text(cx, cy + 0.07, f"{pct_sum:.0f}%",
              ha="center", va="center",
              fontsize=34, fontweight="bold", color=P_PURPLE, zorder=5)
    ax_d.text(cx, cy - 0.065, "выполнение плана",
              ha="center", va="center",
              fontsize=9.5, color=P_MUTED, zorder=5)

    # ── КАРТОЧКИ (правая половина) ─────────────────────────────────
    ax_c = fig.add_axes([0.50, top_bot + pad, 0.485, top_h_f - 2 * pad])
    ax_c.set_xlim(0, 1); ax_c.set_ylim(0, 1)
    ax_c.axis("off"); ax_c.set_facecolor(BG)

    # Единая панель
    ax_c.add_patch(FancyBboxPatch(
        (0.0, 0.0), 1.0, 1.0,
        boxstyle="round,pad=0.018",
        facecolor=CARD_BG, edgecolor=DIVIDER, linewidth=1.0, zorder=1,
    ))

    metrics = [
        ("Всего постов",   str(total_posts),                                   "за период",           P_CYAN),
        ("Всего продаж",   f"{int(total_sum):,} ₽".replace(",", " "),     f"{total_count} шт",   P_GREEN),
        ("Чистая прибыль", f"{int(net_profit):,} ₽".replace(",", " "),    "продажи − комиссия",  P_ORANGE),
    ]
    sec_h = 1.0 / 3
    for k, (label, val, sub, color) in enumerate(metrics):
        y_top = 1.0 - k * sec_h
        y_mid = y_top - sec_h / 2
        y_bot = y_top - sec_h
        if k > 0:
            ax_c.plot([0.06, 0.94], [y_top, y_top],
                      color=DIVIDER, linewidth=0.9, zorder=2)
        ax_c.text(0.5, y_top - 0.04, label,
                  ha="center", va="top", fontsize=9,
                  color=P_MUTED, zorder=3)
        # Размер числа адаптивный: длинные строки мельче
        val_fs = 22 if len(val) <= 7 else 18 if len(val) <= 11 else 15
        ax_c.text(0.5, y_mid + 0.01, val,
                  ha="center", va="center",
                  fontsize=val_fs, fontweight="bold", color=color, zorder=3)
        ax_c.text(0.5, y_bot + 0.04, sub,
                  ha="center", va="bottom", fontsize=8,
                  color=P_MUTED, zorder=3)

    # ── ТАБЛИЦА ────────────────────────────────────────────────────
    n_tbl_cols = len(TBL_COLS)
    total_cw   = sum(TBL_CW)   # = 6.0
    total_rows = n_data_rows    # data + итого

    ax_t = fig.add_axes([0.01, tbl_bot + 0.004,
                         0.98, tbl_h_f - 0.008])
    ax_t.set_xlim(0, total_cw)
    ax_t.set_ylim(0, total_rows + 1)
    ax_t.invert_yaxis()
    ax_t.axis("off"); ax_t.set_facecolor(BG)
    # фон таблицы = фон страницы, без резкой границы
    fig.patch.set_facecolor(BG)

    # Шапка (строка 0)
    x = 0.0
    for col, cw in zip(TBL_COLS, TBL_CW):
        ax_t.add_patch(plt.Rectangle(
            (x, 0), cw, 1.0, facecolor=P_PURPLE, edgecolor="none",
        ))
        ax_t.text(x + cw / 2, 0.5, col,
                  ha="center", va="center",
                  fontsize=8.5, fontweight="bold", color="white")
        x += cw

    # Строки данных
    for ri, (row, pv) in enumerate(zip(rows_data, row_pct_vals)):
        is_total = pv is None
        row_h = 1.3 if is_total else 1.0   # итого чуть выше
        if is_total:
            row_bg = "#DDD6F5"
        else:
            row_bg = "#EDEAF8" if ri % 2 == 0 else BG

        x = 0.0
        for ci, (cell, cw) in enumerate(zip(row, TBL_CW)):
            ax_t.add_patch(plt.Rectangle(
                (x, ri + 1), cw, row_h, facecolor=row_bg, edgecolor="none",
            ))
            # цвет текста
            if ci == 3:   # Продаж ₽
                fc = P_PURPLE if is_total else P_GREEN
            elif ci == 5: # % плана
                if is_total:
                    fc = P_PURPLE
                else:
                    fc = pct_color(pv)
            else:
                fc = P_TEXT

            fw = "bold" if is_total else "normal"
            fs = 9.5 if is_total else 8.5
            ax_t.text(x + cw / 2, ri + 1 + row_h / 2, cell,
                      ha="center", va="center",
                      fontsize=fs, color=fc, fontweight=fw)
            x += cw

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI,
                bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Клавиатуры ─────────────────────────────────────────────────────

def _kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый прогрев", callback_data="wu_new")],
        [InlineKeyboardButton(text="📊 Добавить статистику", callback_data="wu_add_stats")],
        [InlineKeyboardButton(text="📈 Отчёт", callback_data="wu_report")],
        [InlineKeyboardButton(text="📋 Данные по дням", callback_data="wu_view_days")],
        [InlineKeyboardButton(text="👥 Обновить подписчиков", callback_data="wu_upd_subs"),
         InlineKeyboardButton(text="🗑 Сбросить дни", callback_data="wu_clear_days")],
    ])

def _kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="wu_cancel")]
    ])

def _kb_collecting():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово, читай!", callback_data="wu_photos_done")],
        [InlineKeyboardButton(text="📝 Без постов (ввести вручную)", callback_data="wu_no_posts")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="wu_cancel")],
    ])

def _kb_warmups(warmups, action):
    rows = [[InlineKeyboardButton(text=f"🔥 {w['Название']}", callback_data=f"{action}:{w['ID']}")] for w in warmups]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="wu_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Команда /warmup ─────────────────────────────────────────────────

@warmup_router.message(Command("warmup"))
async def cmd_warmup(msg: Message):
    if msg.from_user.id != _owner_id: return
    warmup_states.pop(msg.from_user.id, None)
    await msg.answer("🔥 <b>Анализ прогрева</b>\n\nЧто делаем?", reply_markup=_kb_main())


# ── Новый прогрев ───────────────────────────────────────────────────

@warmup_router.callback_query(F.data == "wu_new")
async def cb_wu_new(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    warmup_states[cb.from_user.id] = {"step": "new_name"}
    await cb.message.answer("Как называется прогрев?\n<i>Например: Июнь 2026</i>", reply_markup=_kb_cancel())
    await cb.answer()


# ── Добавить статистику (пачка фото) ───────────────────────────────

@warmup_router.callback_query(F.data == "wu_add_stats")
async def cb_wu_add_stats(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    warmups = _get_warmups()
    if not warmups:
        await cb.message.answer("Нет активных прогревов. Создай новый.", reply_markup=_kb_main())
        return await cb.answer()
    if len(warmups) == 1:
        await _start_stats_flow(cb.message, cb.from_user.id, warmups[0])
    else:
        await cb.message.answer("Выбери прогрев:", reply_markup=_kb_warmups(warmups, "wu_sel_stats"))
    await cb.answer()


@warmup_router.callback_query(F.data.startswith("wu_sel_stats:"))
async def cb_wu_sel_stats(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    warmups = _get_warmups(only_active=False)
    w = next((x for x in warmups if str(x["ID"]) == wid), None)
    if not w: return await cb.answer("Не найден")
    await _start_stats_flow(cb.message, cb.from_user.id, w)
    await cb.answer()


async def _start_stats_flow(msg, uid, warmup):
    """Начинает добавление статистики. Если подписчики не заданы — спрашивает сначала."""
    subs = int(warmup.get("Подписчики", 0) or 0)
    warmup_states[uid] = {
        "step": "collecting_photos" if subs > 0 else "set_subscribers",
        "warmup_id": str(warmup["ID"]),
        "warmup_name": warmup["Название"],
        "plan_sum": float(warmup["План сумма"]),
        "plan_count": int(warmup["План кол-во"]),
        "subscribers": subs,
        "photos": [],
    }
    if subs == 0:
        await msg.answer(
            f"Прогрев: <b>{warmup['Название']}</b>\n\n"
            "👥 Сколько подписчиков в канале?\n"
            "<i>Нужно для расчёта вовлечённости. Вводится один раз.</i>",
            reply_markup=_kb_cancel()
        )
    else:
        await msg.answer(
            f"Прогрев: <b>{warmup['Название']}</b>\n\n"
            "📸 Скидывай скрины статистики Телеграма — один за другим.\n"
            "Когда закончишь — нажми кнопку <b>Готово</b>.\n\n"
            "Если постов не было — нажми <b>Без постов</b>.",
            reply_markup=_kb_collecting()
        )


def _start_collect_photos(uid, warmup):
    warmup_states[uid] = {
        "step": "collecting_photos",
        "warmup_id": str(warmup["ID"]),
        "warmup_name": warmup["Название"],
        "plan_sum": float(warmup["План сумма"]),
        "plan_count": int(warmup["План кол-во"]),
        "subscribers": int(warmup.get("Подписчики", 0) or 0),
        "photos": [],
    }


# ── Отчёт ───────────────────────────────────────────────────────────

@warmup_router.callback_query(F.data == "wu_report")
async def cb_wu_report(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    warmups = _get_warmups()
    if not warmups:
        await cb.message.answer("Нет активных прогревов.", reply_markup=_kb_main()); return await cb.answer()
    if len(warmups) == 1:
        await _send_report(cb.message, warmups[0])
    else:
        await cb.message.answer("Выбери прогрев:", reply_markup=_kb_warmups(warmups, "wu_sel_report"))
    await cb.answer()


@warmup_router.callback_query(F.data.startswith("wu_sel_report:"))
async def cb_wu_sel_report(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    warmups = _get_warmups(only_active=False)
    w = next((x for x in warmups if str(x["ID"]) == wid), None)
    if w: await _send_report(cb.message, w)
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_upd_subs")
async def cb_wu_upd_subs(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    warmups = _get_warmups()
    if not warmups:
        await cb.message.answer("Нет активных прогревов.", reply_markup=_kb_main())
        return await cb.answer()
    if len(warmups) == 1:
        warmup_states[cb.from_user.id] = {
            "step": "upd_subscribers",
            "warmup_id": str(warmups[0]["ID"]),
            "warmup_name": warmups[0]["Название"],
        }
        await cb.message.answer(
            f"Прогрев: <b>{warmups[0]['Название']}</b>\n\n"
            "👥 Введи актуальное количество подписчиков:",
            reply_markup=_kb_cancel()
        )
    else:
        await cb.message.answer("Выбери прогрев:", reply_markup=_kb_warmups(warmups, "wu_sel_updsubs"))
    await cb.answer()


@warmup_router.callback_query(F.data.startswith("wu_sel_updsubs:"))
async def cb_wu_sel_updsubs(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    warmups = _get_warmups(only_active=False)
    w = next((x for x in warmups if str(x["ID"]) == wid), None)
    if not w: return await cb.answer("Не найден")
    warmup_states[cb.from_user.id] = {"step": "upd_subscribers", "warmup_id": wid, "warmup_name": w["Название"]}
    await cb.message.answer(
        f"Прогрев: <b>{w['Название']}</b>\n\n👥 Введи актуальное количество подписчиков:",
        reply_markup=_kb_cancel()
    )
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_clear_days")
async def cb_wu_clear_days(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    warmups = _get_warmups()
    if not warmups:
        await cb.message.answer("Нет активных прогревов.", reply_markup=_kb_main())
        return await cb.answer()
    if len(warmups) == 1:
        await _clear_warmup_days(cb.message, str(warmups[0]["ID"]), warmups[0]["Название"])
    else:
        await cb.message.answer("Выбери прогрев для сброса:", reply_markup=_kb_warmups(warmups, "wu_sel_clear"))
    await cb.answer()


@warmup_router.callback_query(F.data.startswith("wu_sel_clear:"))
async def cb_wu_sel_clear(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    warmups = _get_warmups(only_active=False)
    w = next((x for x in warmups if str(x["ID"]) == wid), None)
    if w: await _clear_warmup_days(cb.message, wid, w["Название"])
    await cb.answer()


async def _clear_warmup_days(msg, warmup_id: str, warmup_name: str):
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        ws = ss.worksheet(SHEET_WARMUP_DAYS)
        rows = ws.get_all_values()
        to_delete = [i + 1 for i, row in enumerate(rows[1:], start=1)
                     if row and str(row[0]) == str(warmup_id)]
        for i in reversed(to_delete):
            ws.delete_rows(i + 1)
        await msg.answer(
            f"✅ Удалено {len(to_delete)} строк по прогреву «{warmup_name}».\n"
            "Теперь добавь статистику заново через «📊 Добавить статистику».",
            reply_markup=_kb_main()
        )
    except Exception as e:
        logger.error(f"warmup clear days: {e}")
        await msg.answer("Ошибка при удалении.", reply_markup=_kb_main())


@warmup_router.callback_query(F.data == "wu_view_days")
async def cb_wu_view_days(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    warmups = _get_warmups()
    if not warmups:
        await cb.message.answer("Нет активных прогревов.", reply_markup=_kb_main())
        return await cb.answer()
    if len(warmups) == 1:
        await _show_days(cb.message, str(warmups[0]["ID"]), warmups[0]["Название"])
    else:
        await cb.message.answer("Выбери прогрев:", reply_markup=_kb_warmups(warmups, "wu_sel_viewdays"))
    await cb.answer()


@warmup_router.callback_query(F.data.startswith("wu_sel_viewdays:"))
async def cb_wu_sel_viewdays(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    warmups = _get_warmups(only_active=False)
    w = next((x for x in warmups if str(x["ID"]) == wid), None)
    if w: await _show_days(cb.message, wid, w["Название"])
    await cb.answer()


async def _show_days(msg, warmup_id: str, warmup_name: str):
    try:
        ss = _gc.open_by_key(_spreadsheet_id)
        ws = ss.worksheet(SHEET_WARMUP_DAYS)
        all_rows = ws.get_all_values()
        header = all_rows[0] if all_rows else []
        data_rows = [(i + 2, row) for i, row in enumerate(all_rows[1:])
                     if row and str(row[0]) == str(warmup_id)]
        if not data_rows:
            await msg.answer(f"Нет данных по прогреву «{warmup_name}».", reply_markup=_kb_main())
            return

        # Строим текст
        lines = [f"📋 <b>{warmup_name}</b> — сохранённые дни:\n"]
        for sheet_row, row in data_rows:
            date = row[2] if len(row) > 2 else "?"
            # Учитываем оба варианта заголовка (старый и новый с Сумма охватов)
            if "Сумма охватов" in header:
                posts = row[3] if len(row) > 3 else "?"
                avg_v = row[5] if len(row) > 5 else "?"
                sales_rub = row[10] if len(row) > 10 else "?"
                sales_cnt = row[9] if len(row) > 9 else "?"
            else:
                posts = row[3] if len(row) > 3 else "?"
                avg_v = row[4] if len(row) > 4 else "?"
                sales_rub = row[9] if len(row) > 9 else "?"
                sales_cnt = row[8] if len(row) > 8 else "?"
            lines.append(f"• {date}: постов={posts}, охват≈{avg_v}, продаж={sales_cnt} ({sales_rub}₽)")

        lines.append("\nЧтобы удалить конкретный день — нажми кнопку ниже:")
        await msg.answer("\n".join(lines), parse_mode="HTML")

        # Кнопки для удаления каждой строки
        buttons = []
        for sheet_row, row in data_rows:
            date = row[2] if len(row) > 2 else f"строка {sheet_row}"
            buttons.append([InlineKeyboardButton(
                text=f"🗑 Удалить {date}",
                callback_data=f"wu_del_row:{sheet_row}:{warmup_id}:{warmup_name[:20]}"
            )])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="wu_back_main")])
        await msg.answer("Выбери строку для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"warmup show days: {e}")
        await msg.answer("Ошибка при загрузке данных.", reply_markup=_kb_main())


@warmup_router.callback_query(F.data.startswith("wu_del_row:"))
async def cb_wu_del_row(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    parts = cb.data.split(":", 3)
    sheet_row = int(parts[1])
    warmup_id = parts[2]
    warmup_name = parts[3] if len(parts) > 3 else ""
    try:
        import asyncio
        ss = await asyncio.to_thread(lambda: _gc.open_by_key(_spreadsheet_id))
        ws = await asyncio.to_thread(ss.worksheet, SHEET_WARMUP_DAYS)
        await asyncio.to_thread(ws.delete_rows, sheet_row)
        await cb.message.answer(f"✅ Строка удалена. Показать оставшиеся данные?",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="📋 Да, показать", callback_data=f"wu_sel_viewdays:{warmup_id}")],
                                    [InlineKeyboardButton(text="◀️ Меню", callback_data="wu_back_main")],
                                ]))
    except Exception as e:
        logger.error(f"warmup del row: {e}")
        await cb.message.answer("Ошибка при удалении строки.", reply_markup=_kb_main())
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_back_main")
async def cb_wu_back_main(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    await cb.message.answer("Меню прогрева:", reply_markup=_kb_main())
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_stats_ok")
async def cb_wu_stats_ok(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    state = warmup_states.get(cb.from_user.id, {})
    if state.get("step") == "confirm_stats":
        state["step"] = "day_sales_count"
        await _ask_next_date(cb.message, state)
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_stats_edit")
async def cb_wu_stats_edit(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    state = warmup_states.get(cb.from_user.id, {})
    if state.get("step") == "confirm_stats":
        state["step"] = "confirm_stats_edit"
        await cb.message.answer(
            "Напиши исправленные данные — 4 числа через пробел:\n"
            "<code>постов  охват  лайков  репостов</code>\n"
            "Пример: <code>5 205 26 10</code>"
        )
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_stats_skip")
async def cb_wu_stats_skip(cb: CallbackQuery):
    if cb.from_user.id != _owner_id: return await cb.answer()
    state = warmup_states.get(cb.from_user.id, {})
    if state.get("step") == "confirm_stats":
        skipped_date = state["dates_queue"].pop(0)
        await cb.message.answer(f"🗑 Дата {skipped_date} удалена.")
        if state["dates_queue"]:
            state["step"] = "confirm_stats"
            await _ask_confirm_stats(cb.message, state)
        else:
            await _finalize_all_days(cb.message, state)
            warmup_states.pop(cb.from_user.id, None)
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_cancel")
async def cb_wu_cancel(cb: CallbackQuery):
    warmup_states.pop(cb.from_user.id, None)
    await cb.message.answer("Отменено.", reply_markup=_kb_main())
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_no_posts")
async def cb_wu_no_posts(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid != _owner_id: return await cb.answer()
    state = warmup_states.get(uid)
    if not state: return await cb.answer()
    state["step"] = "no_posts_date"
    await cb.message.answer(
        "📅 Введи дату дня (без постов):\n"
        "Формат: <code>ДД.ММ</code>, например <code>12.06</code>",
        reply_markup=_kb_cancel()
    )
    await cb.answer()


@warmup_router.callback_query(F.data == "wu_photos_done")
async def cb_wu_photos_done(cb: CallbackQuery):
    uid = cb.from_user.id
    state = warmup_states.get(uid, {})
    photos = state.get("photos", [])
    if not photos:
        await cb.answer("Ты не отправила ни одного скрина.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(f"⏳ Читаю {len(photos)} скрин(а)…")
    try:
        import asyncio
        by_date = await asyncio.to_thread(_parse_stats_images, photos, state.get("subscribers", 0))
    except Exception as e:
        logger.error(f"warmup parse: {e}")
        await cb.message.answer("Не смог распознать статистику 😔\nПопробуй ещё раз.")
        warmup_states.pop(uid, None)
        return
    if not by_date:
        await cb.message.answer("Не нашёл постов на скринах. Попробуй другие скрины.")
        warmup_states.pop(uid, None)
        return
    state["by_date"] = by_date
    state["dates_queue"] = list(by_date.keys())
    state["days_entered"] = []
    state["step"] = "confirm_stats"
    await _ask_confirm_stats(cb.message, state)


# ── Обработка фото ──────────────────────────────────────────────────

async def handle_warmup_photo(msg: Message) -> bool:
    uid = msg.from_user.id
    if uid not in warmup_states: return False
    state = warmup_states[uid]
    if state.get("step") != "collecting_photos": return False

    photo = msg.photo[-1]
    file = await _bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await _bot.download_file(file.file_path, buf)
    state["photos"].append(buf.getvalue())
    count = len(state["photos"])
    kb = _kb_collecting()
    await msg.answer(
        f"✅ Скрин {count} принят. Скидывай ещё или нажми кнопку.",
        reply_markup=kb
    )
    return True


# ── Обработка текста ────────────────────────────────────────────────

async def handle_warmup_text(msg: Message) -> bool:
    uid = msg.from_user.id
    if uid not in warmup_states: return False
    state = warmup_states[uid]
    step = state.get("step")
    text = msg.text.strip() if msg.text else ""

    # ── Создание прогрева ──
    if step == "new_name":
        state["warmup_name"] = text; state["step"] = "new_plan_sum"
        await msg.answer("💰 План по сумме (₽):\n<i>Например: 150000</i>", reply_markup=_kb_cancel())
        return True

    if step == "new_plan_sum":
        try: state["plan_sum"] = float(text.replace(" ", "").replace(",", "."))
        except Exception:
            await msg.answer("Введи число, например: 150000"); return True
        state["step"] = "new_plan_count"
        await msg.answer("🎯 Сколько продаж планируешь?", reply_markup=_kb_cancel())
        return True

    if step == "new_plan_count":
        try: state["plan_count"] = int(text.replace(" ", ""))
        except Exception:
            await msg.answer("Введи число, например: 10"); return True
        state["step"] = "new_subscribers"
        await msg.answer("👥 Сколько подписчиков в канале?\n<i>Нужно для расчёта вовлечённости</i>",
                         reply_markup=_kb_cancel())
        return True

    if step == "new_subscribers":
        try: state["subscribers"] = int(text.replace(" ", "").replace(",", ""))
        except Exception:
            await msg.answer("Введи число, например: 1200"); return True
        wid = _create_warmup(state["warmup_name"], state["plan_sum"], state["plan_count"], state["subscribers"])
        warmup_states.pop(uid, None)
        await msg.answer(
            f"✅ Прогрев <b>{state['warmup_name']}</b> создан!\n"
            f"План: {int(state['plan_sum']):,} ₽ / {state['plan_count']} продаж".replace(",", " ") + "\n"
            f"Подписчиков: {state['subscribers']:,}".replace(",", " ") + "\n\n"
            "Добавляй статистику через «📊 Добавить статистику».",
            reply_markup=_kb_main()
        )
        return True

    # ── Подписчики для существующего прогрева ──
    if step in ("set_subscribers", "upd_subscribers"):
        try: subs = int(text.replace(" ", "").replace(",", ""))
        except Exception:
            await msg.answer("Введи число, например: 1200"); return True
        state["subscribers"] = subs
        _save_subscribers(state["warmup_id"], subs)
        if step == "upd_subscribers":
            warmup_states.pop(uid, None)
            await msg.answer(
                f"✅ Подписчики обновлены: {subs:,}".replace(",", " "),
                reply_markup=_kb_main()
            )
            return True
        state["step"] = "collecting_photos"
        await msg.answer(
            f"✅ Подписчиков: {subs:,}".replace(",", " ") + "\n\n"
            "📸 Скидывай скрины статистики Телеграма — один за другим.\n"
            "Когда закончишь — нажми кнопку <b>Готово</b>.\n\n"
            "Если постов не было — нажми <b>Без постов</b>.",
            reply_markup=_kb_collecting()
        )
        return True

    # ── Сбор фото: любой текст — напомнить что делать ──
    if step == "collecting_photos":
        if text.lower() not in ("готово", "готов", "всё", "все", "done"):
            await msg.answer(
                "Скидывай скрины статистики.\n"
                "Когда все скрины отправлены — нажми <b>Готово</b>.\n"
                "Если постов не было — нажми <b>Без постов</b>.",
                reply_markup=_kb_collecting()
            )
            return True

    # ── Сбор фото: «готово» ──
    if step == "collecting_photos" and text.lower() in ("готово", "готов", "всё", "все", "done"):
        photos = state.get("photos", [])
        if not photos:
            await msg.answer("Ты не отправила ни одного скрина. Скидывай фото или нажми «Отмена».")
            return True
        await msg.answer(f"⏳ Читаю {len(photos)} скрин(а)…")
        try:
            import asyncio
            by_date = await asyncio.to_thread(_parse_stats_images, photos, state.get("subscribers", 0))
        except Exception as e:
            logger.error(f"warmup parse: {e}")
            await msg.answer("Не смог распознать статистику 😔\nПопробуй ещё раз.")
            warmup_states.pop(uid, None)
            return True

        if not by_date:
            await msg.answer("Не нашёл постов на скринах. Попробуй другие скрины.")
            warmup_states.pop(uid, None)
            return True

        # Показываем что Claude прочитал — просим подтвердить или исправить
        state["by_date"] = by_date
        state["dates_queue"] = list(by_date.keys())
        state["days_entered"] = []
        state["step"] = "confirm_stats"
        await _ask_confirm_stats(msg, state)
        return True

    # ── Подтверждение: ввод исправления текстом ──
    if step == "confirm_stats_edit":
        parts = text.replace(",", ".").split()
        cur_date = state["dates_queue"][0]
        d = state["by_date"][cur_date]
        try:
            if len(parts) == 1:
                d["posts"] = int(parts[0])
                d["total_views"] = d["avg_views"] * d["posts"]
            elif len(parts) >= 4:
                d["posts"] = int(parts[0])
                d["avg_views"] = float(parts[1])
                d["total_views"] = float(parts[1]) * int(parts[0])
                d["likes"] = int(parts[2])
                d["reposts"] = int(parts[3])
            else:
                raise ValueError()
            state["step"] = "confirm_stats"
            await _ask_confirm_stats(msg, state)
        except Exception:
            await msg.answer(
                "Напиши 4 числа через пробел:\n"
                "<code>постов  охват  лайков  репостов</code>\n"
                "Пример: <code>5 205 26 10</code>"
            )
        return True

    # ── Ввод даты вручную (без постов) ──
    if step == "no_posts_date":
        import re as _re
        # Принимаем форматы: 12.06, 12.06.2025, 2025-06-12
        date_str = text.strip()
        m = _re.match(r'^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$', date_str)
        if not m:
            await msg.answer("Введи дату в формате ДД.ММ, например: <code>12.06</code>"); return True
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else datetime.now().year
        if year < 100: year += 2000
        from datetime import date as _date
        try:
            d = _date(year, month, day)
        except ValueError:
            await msg.answer("Неверная дата. Введи в формате ДД.ММ, например: <code>12.06</code>"); return True
        date_key = d.strftime("%Y-%m-%d")
        # Создаём запись с 0 постами
        if "by_date" not in state:
            state["by_date"] = {}
        state["by_date"][date_key] = {"posts": 0, "avg_views": 0, "total_views": 0,
                                       "likes": 0, "reposts": 0, "engagement": 0}
        state["dates_queue"] = [date_key]
        state["days_entered"] = state.get("days_entered", [])
        state["step"] = "day_sales_count"
        await _ask_next_date(msg, state)
        return True

    # ── Ввод продаж по датам ──
    if step == "day_sales_count":
        try: state["cur_sales_count"] = int(text.replace(" ", ""))
        except Exception:
            await msg.answer("Введи число продаж, например: 3"); return True
        state["step"] = "day_sales_sum"
        await msg.answer("💰 Сумма продаж за этот день (₽):", reply_markup=_kb_cancel())
        return True

    if step == "day_sales_sum":
        try: state["cur_sales_sum"] = float(text.replace(" ", "").replace(",", "."))
        except Exception:
            await msg.answer("Введи сумму, например: 4500"); return True
        state["step"] = "day_commission"
        await msg.answer("💸 Комиссия/расходы за день (₽, или 0):", reply_markup=_kb_cancel())
        return True

    if step == "day_commission":
        try: commission = float(text.replace(" ", "").replace(",", "."))
        except Exception: commission = 0.0
        # Сохраняем текущую дату
        cur_date = state["dates_queue"][0]
        d = state["by_date"][cur_date]
        state["days_entered"].append({
            "date": cur_date,
            "posts": d["posts"], "total_views": d.get("total_views", 0),
            "avg_views": d["avg_views"],
            "likes": d["likes"], "reposts": d["reposts"],
            "engagement": d["engagement"],
            "sales_count": state["cur_sales_count"],
            "sales_sum": state["cur_sales_sum"],
            "commission": commission,
        })
        state["dates_queue"].pop(0)

        if state["dates_queue"]:
            # Следующая дата — сначала подтверждение статистики
            state["step"] = "confirm_stats"
            await _ask_confirm_stats(msg, state)
        else:
            # Все даты заполнены — сохраняем и генерируем бублик
            await _finalize_all_days(msg, state)
            warmup_states.pop(uid, None)
        return True

    return False


async def _ask_confirm_stats(msg: Message, state: dict):
    cur_date = state["dates_queue"][0]
    d = state["by_date"][cur_date]
    total_dates = len(state["by_date"])
    done = total_dates - len(state["dates_queue"])
    await msg.answer(
        f"📅 <b>{cur_date}</b> ({done+1}/{total_dates}) — прочитал со скрина:\n\n"
        f"  Постов: <b>{d['posts']}</b>\n"
        f"  Ср. охват: <b>{d['avg_views']:.0f}</b>\n"
        f"  Лайки: <b>{d['likes']}</b>\n"
        f"  Репосты: <b>{d['reposts']}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, верно", callback_data="wu_stats_ok")],
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="wu_stats_edit")],
            [InlineKeyboardButton(text="🗑 Удалить эту дату", callback_data="wu_stats_skip")],
        ])
    )


async def _ask_next_date(msg: Message, state: dict):
    cur_date = state["dates_queue"][0]
    d = state["by_date"][cur_date]
    total_dates = len(state["by_date"])
    done = total_dates - len(state["dates_queue"])
    await msg.answer(
        f"📅 <b>{cur_date}</b> ({done+1}/{total_dates})\n"
        f"  Постов: {d['posts']}, ср. охват: {d['avg_views']:.0f}\n"
        f"  Лайки: {d['likes']}, репосты: {d['reposts']}\n\n"
        "Сколько продаж в этот день?",
        reply_markup=_kb_cancel()
    )


async def _finalize_all_days(msg: Message, state: dict):
    warmup_id = state["warmup_id"]
    warmup_name = state["warmup_name"]
    plan_sum = state["plan_sum"]
    plan_count = state["plan_count"]

    import asyncio
    await msg.answer("⏳ Сохраняю данные…")

    # Берём все существующие строки для накопленных итогов (не удаляем — дописываем)
    all_existing = _get_warmup_days(warmup_id)
    base_sum = sum(float(d.get("Продажи руб", 0)) for d in all_existing)
    base_count = sum(int(d.get("Продажи шт", 0)) for d in all_existing)

    # Сортируем новые дни по дате
    def _sort_key(day):
        try: return datetime.datetime.strptime(day["date"], "%d.%m.%y")
        except Exception: return datetime.datetime.min

    new_days_sorted = sorted(state["days_entered"], key=_sort_key)

    # Сохраняем новые дни с накопленным % от плана
    running_sum = base_sum
    running_count = base_count
    for day in new_days_sorted:
        running_sum += day["sales_sum"]
        running_count += day["sales_count"]
        plan_pct = running_sum / plan_sum * 100 if plan_sum > 0 else 0
        _save_day(warmup_id, warmup_name, day["date"],
                  day["posts"], day["avg_views"], day["likes"], day["reposts"],
                  day["engagement"], day["sales_count"], day["sales_sum"],
                  day["commission"], plan_pct, day.get("total_views", 0))

    # Все дни для графика
    all_days = _get_warmup_days(warmup_id)
    # Взвешенный средний охват = сумма(Ср.охват × Постов) / все посты
    total_posts_all = sum(int(d.get("Постов", 0)) for d in all_days)
    total_views_all = sum(float(d.get("Ср.охват", 0)) * int(d.get("Постов", 0)) for d in all_days)
    avg_views_all = total_views_all / total_posts_all if total_posts_all > 0 else 0
    # Вовлечённость = реакции на 1 пост / подписчики × 100
    subs = state.get("subscribers", 0)
    if subs > 0:
        tot_likes_all = sum(int(d.get("Лайки", 0)) for d in all_days)
        tot_rep_all = sum(int(d.get("Репосты", 0)) for d in all_days)
        avg_eng_all = (tot_likes_all + tot_rep_all) / (total_posts_all or 1) / subs * 100
    else:
        per_day_eng = [float(d.get("Вовлечённость%", 0)) for d in all_days]
        avg_eng_all = sum(per_day_eng) / len(per_day_eng) if per_day_eng else 0

    # Корреляция постов → продажи
    corr_lines = []
    for d in all_days[-7:]:
        p = d.get("Постов", 0); s = d.get("Продажи шт", 0)
        if p and s:
            corr_lines.append(f"  {d['Дата']}: {p} пост(а) → {s} продаж")

    try:
        png = await asyncio.to_thread(
            _generate_donut, warmup_name, plan_sum, plan_count,
            running_sum, running_count, avg_views_all, avg_eng_all, all_days,
            state.get("subscribers", 0)
        )
        caption = (
            f"🔥 <b>{warmup_name}</b> — итого {len(all_days)} дн.\n"
            f"💰 Собрано: <b>{int(running_sum):,} ₽</b> ({running_sum/plan_sum*100:.1f}% плана)".replace(",", " ") + "\n"
            f"🎯 Продаж: <b>{running_count}</b> из {plan_count}"
            + ("\n\n🔗 <b>Посты → продажи:</b>\n" + "\n".join(corr_lines) if corr_lines else "")
        )
        await msg.answer_photo(BufferedInputFile(png, filename="warmup.png"), caption=caption)
    except Exception as e:
        logger.error(f"warmup donut: {e}")
        await msg.answer(
            f"✅ Данные сохранены!\n"
            f"💰 Итого: {int(running_sum):,} ₽ ({running_sum/plan_sum*100:.1f}% плана)".replace(",", " ")
        )
    await msg.answer("Что дальше?", reply_markup=_kb_main())


async def _send_report(msg: Message, warmup: dict):
    wid = str(warmup["ID"])
    days = _get_warmup_days(wid)
    if not days:
        await msg.answer(f"По прогреву «{warmup['Название']}» пока нет данных."); return
    plan_sum = float(warmup["План сумма"])
    plan_count = int(warmup["План кол-во"])
    total_sum = sum(float(d.get("Продажи руб", 0)) for d in days)
    total_count = sum(int(d.get("Продажи шт", 0)) for d in days)
    tot_posts = sum(int(d.get("Постов", 0)) for d in days)
    avg_views = (
        sum(float(d.get("Ср.охват", 0)) * int(d.get("Постов", 0)) for d in days) / tot_posts
        if tot_posts > 0 else 0
    )
    subs_rep = int(warmup.get("Подписчики", 0) or 0)
    if subs_rep > 0:
        tot_l = sum(int(d.get("Лайки", 0)) for d in days)
        tot_r = sum(int(d.get("Репосты", 0)) for d in days)
        avg_eng = (tot_l + tot_r) / (tot_posts or 1) / subs_rep * 100
    else:
        avg_eng = sum(float(d.get("Вовлечённость%", 0)) for d in days) / len(days)
    await msg.answer("⏳ Строю отчёт…")
    import asyncio
    try:
        subs = int(warmup.get("Подписчики", 0) or 0)
        png = await asyncio.to_thread(_generate_donut, warmup["Название"], plan_sum, plan_count,
                                      total_sum, total_count, avg_views, avg_eng, days, subs)
        await msg.answer_photo(BufferedInputFile(png, filename="warmup_report.png"),
                               caption=f"📈 Отчёт по прогреву <b>{warmup['Название']}</b>")
    except Exception as e:
        logger.error(f"warmup report: {e}")
        await msg.answer("Ошибка при генерации отчёта.")
