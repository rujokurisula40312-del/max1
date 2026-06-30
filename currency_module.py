"""Модуль курсов валют туроператоров.

Источники:
  - tour-kassa.ru  — сводная таблица по всем ТО (сегодня / завтра)
  - cruclub.ru     — курс CruClub
  - pac.ru         — курс PAC Group
  - lavoyage.ru    — курс Ла Вояж
"""
import re
import logging
import datetime
import aiohttp

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def _fetch_html(url: str) -> str:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.text()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_date_dd_mm(text: str) -> datetime.date | None:
    """Извлекает дату вида ДД.ММ или ДД.ММ.ГГ из строки."""
    m = re.search(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?', text)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year_raw = m.group(3)
    if year_raw:
        y = int(year_raw)
        year = 2000 + y if y < 100 else y
    else:
        year = datetime.date.today().year
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _clean_op_name(td) -> str:
    """Берёт только первую строку текста ячейки (имя оператора без ИКС: XXXX)."""
    # Берём текст первого text-узла или первого дочернего тега до <br>/<small>
    parts = []
    for child in td.children:
        import bs4
        if isinstance(child, bs4.NavigableString):
            t = child.strip()
            if t:
                parts.append(t)
                break
        elif child.name in ("br", "small", "span", "sub"):
            break
        else:
            t = child.get_text(strip=True)
            if t:
                parts.append(t)
                break
    name = parts[0] if parts else td.get_text(strip=True)
    # На случай если всё равно слиплось — обрезаем по «ИКС»
    name = re.sub(r'\s*ИКС\s*:.*', '', name, flags=re.I).strip()
    return name


def _parse_table(table, date_label: str) -> list[str]:
    """Парсит таблицу туроператоров, возвращает список строк."""
    lines = [f"💱 <b>Курсы валют туроператоров на {date_label}</b>",
             "<i>Источник: tour-kassa.ru</i>\n"]
    rows = table.find_all("tr")
    header_done = False
    for row in rows:
        tds = row.find_all(["td", "th"])
        if not tds:
            continue
        cells_text = [td.get_text(strip=True) for td in tds]
        if all(c == "" for c in cells_text):
            continue
        if not header_done:
            header_done = True
            continue
        if len(tds) < 4:
            continue
        name = _clean_op_name(tds[0])
        eur  = cells_text[1] if len(cells_text) > 1 else "—"
        usd  = cells_text[4] if len(cells_text) > 4 else "—"
        if "цб рф" in name.lower() or name.strip().lower() == "цб":
            lines.append(f"🏦 <b>ЦБ РФ:</b>  € = {eur} ₽  |  $ = {usd} ₽\n")
        elif name:
            lines.append(f"• <b>{name}</b>:  € {eur} ₽  |  $ {usd} ₽")
    return lines


async def fetch_tour_kassa_rates(tomorrow: bool = False) -> str:
    """Парсит сводную таблицу курсов с tour-kassa.ru.
    Определяет нужную таблицу по дате в заголовке: сравниваем с сегодняшней датой.
    """
    from bs4 import BeautifulSoup
    url = "https://tour-kassa.ru/%D0%BA%D1%83%D1%80%D1%81%D1%8B-%D0%B2%D0%B0%D0%BB%D1%8E%D1%82-%D1%82%D1%83%D1%80%D0%BE%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%D0%BE%D0%B2"
    target_date = datetime.date.today() + datetime.timedelta(days=1 if tomorrow else 0)
    day_label = target_date.strftime("%d.%m.%Y")

    try:
        html = await _fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")

        tables = soup.find_all("table")
        if not tables:
            return "⚠️ Не удалось найти таблицы на tour-kassa.ru"

        # Для каждой таблицы ищем ближайший заголовок с датой
        # и выбираем ту, дата которой совпадает с target_date
        # На tour-kassa.ru: таблица 0 = завтра, таблица 1 = сегодня
        idx = 0 if tomorrow else 1
        best_table = tables[idx] if idx < len(tables) else tables[0]

        lines = _parse_table(best_table, day_label)

        if len(lines) <= 2:
            return "⚠️ Не удалось распознать данные с tour-kassa.ru. Попробуйте позже."

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"fetch_tour_kassa_rates: {e}")
        return f"❌ Ошибка при загрузке tour-kassa.ru: {str(e)[:150]}"


async def fetch_cruclub_rates() -> str:
    """Курс CruClub со страницы cruclub.ru."""
    from bs4 import BeautifulSoup
    try:
        html = await _fetch_html("https://www.cruclub.ru/agent/howto/book/")
        soup = BeautifulSoup(html, "html.parser")
        flat = _norm(soup.get_text())

        m_usd = re.search(r'1\s*USD\s*=\s*([\d.,]+)\s*RU[BR]', flat, re.I)
        m_eur = re.search(r'1\s*EUR\s*=\s*([\d.,]+)\s*RU[BR]', flat, re.I)
        if m_usd and m_eur:
            return f"💱 <b>Курс CruClub</b>\n\n1 USD = {m_usd.group(1)} ₽\n1 EUR = {m_eur.group(1)} ₽\n\n<i>Источник: cruclub.ru</i>"

        m_usd = re.search(r'USD\s*=\s*([\d.,]+)', flat, re.I)
        m_eur = re.search(r'EUR\s*=\s*([\d.,]+)', flat, re.I)
        if m_usd and m_eur:
            return f"💱 <b>Курс CruClub</b>\n\n1 USD = {m_usd.group(1)} ₽\n1 EUR = {m_eur.group(1)} ₽\n\n<i>Источник: cruclub.ru</i>"

        return "⚠️ Не удалось найти курс CruClub. Проверьте вручную: cruclub.ru/agent/howto/book/"
    except Exception as e:
        logger.error(f"fetch_cruclub_rates: {e}")
        return f"❌ Ошибка CruClub: {str(e)[:150]}"


async def fetch_lavoyage_rates() -> str:
    """Курс Ла Вояж со страницы lavoyage.ru.
    Курсы лежат прямо в HTML в JSON-блоке: "USD":{"рб":NNN} и "EUR":{"рб":NNN}.
    Кириллица в ключе побита кодировкой ("руб" → "рб"), поэтому парсим именно так."""
    try:
        html = await _fetch_html("https://lavoyage.ru/")
        m_usd = re.search(r'"USD"\s*:\s*\{[^}]*"рб"\s*:\s*([0-9.]+)', html)
        m_eur = re.search(r'"EUR"\s*:\s*\{[^}]*"рб"\s*:\s*([0-9.]+)', html)
        if m_usd and m_eur:
            usd = float(m_usd.group(1))
            eur = float(m_eur.group(1))
            return (f"💱 <b>Курс Ла Вояж</b>\n\n"
                    f"1 USD = {usd:.2f} ₽\n"
                    f"1 EUR = {eur:.2f} ₽\n\n"
                    f"<i>Источник: lavoyage.ru</i>")
        return "⚠️ Не удалось найти курс Ла Вояж. Проверьте вручную: lavoyage.ru"
    except Exception as e:
        logger.error(f"fetch_lavoyage_rates: {e}")
        return f"❌ Ошибка Ла Вояж: {str(e)[:150]}"


async def fetch_pac_rates() -> str:
    """Курс PAC Group с главной страницы pac.ru."""
    from bs4 import BeautifulSoup
    try:
        html = await _fetch_html("https://www.pac.ru/")
        soup = BeautifulSoup(html, "html.parser")
        flat = _norm(soup.get_text())

        m_usd = re.search(r'1\s*\$\s*[=:]\s*([\d]{2,3}[.,]\d{2,6})', flat)
        m_eur = re.search(r'1\s*€\s*[=:]\s*([\d]{2,3}[.,]\d{2,6})', flat)
        if m_usd and m_eur:
            usd = m_usd.group(1).replace(",", ".")
            eur = m_eur.group(1).replace(",", ".")
            return f"💱 <b>Курс PAC Group</b>\n\n1 USD = {usd} ₽\n1 EUR = {eur} ₽\n\n<i>Источник: pac.ru</i>"

        m_usd = re.search(r'USD\s*[=:]\s*([\d.,]+)', flat, re.I)
        m_eur = re.search(r'EUR\s*[=:]\s*([\d.,]+)', flat, re.I)
        if m_usd and m_eur:
            return f"💱 <b>Курс PAC Group</b>\n\n1 USD = {m_usd.group(1)} ₽\n1 EUR = {m_eur.group(1)} ₽\n\n<i>Источник: pac.ru</i>"

        return "⚠️ Не удалось найти курс PAC Group. Проверьте вручную: pac.ru"
    except Exception as e:
        logger.error(f"fetch_pac_rates: {e}")
        return f"❌ Ошибка PAC Group: {str(e)[:150]}"
