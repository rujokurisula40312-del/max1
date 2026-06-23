"""Интеграция с U-ON Travel CRM.
Поиск заявок по фамилии заказчика/туриста и вывод карточки с ключевыми полями.
Требуются переменные окружения: UON_API_KEY, UON_ACCOUNT_ID.

Особенности U-ON API, важные для понимания кода:
- /users/{page}.json — постраничный список заказчиков (купивших тур).
- /lead/{page}.json   — постраничный список заявок; внутри каждой заявки
  есть массив `tourists` (это и есть туристы, отдельной коллекции нет).
- /request-by-client/{client_id}/{page}.json — заявки конкретного заказчика.
- На «нет данных» U-ON отвечает HTTP 404 с телом {"result":404,"requests":[]} —
  это нормальный ответ, а не ошибка. Эндпоинты для туриста как top-level
  ресурса (/tourist/{page}.json) НЕ существуют — туристы доступны только
  внутри заявок.
"""
import os, logging
import aiohttp
from maxapi import Router, F
from maxapi.types import MessageCreated, MessageCallback, CallbackButton, LinkButton
from maxapi.types.attachments import AttachmentButton, ButtonsPayload
from maxapi.enums import AttachmentType

logger = logging.getLogger(__name__)

UON_API_KEY = os.environ.get("UON_API_KEY", "").strip()
UON_ACCOUNT_ID = os.environ.get("UON_ACCOUNT_ID", "").strip()
UON_BASE = "https://api.u-on.ru"

# Сколько страниц заявок максимум листать при поиске.
# Для поиска по ФИО/email хватает 40 страниц свежих заявок (≈1000 заявок),
# чтобы не превращать поиск в минуту ожидания.
# Для поиска по номеру заявки и по телефону нужен более глубокий скан —
# точные совпадения не должны теряться в архиве.
LEAD_SCAN_PAGES = 60
LEAD_SCAN_PAGES_DEEP = 200

uon_router = Router()
uon_states: dict[int, dict] = {}  # uid -> {"step": "uon_search"}
# Кэш заявок из последнего поиска: uid -> {lead_id: lead_dict}.
# Нужен потому, что U-ON API не отдаёт одну заявку по ID отдельным запросом
# (/lead/{id}.json конфликтует с листингом /lead/{page}.json), приходится
# использовать запись, уже полученную при поиске.
uon_leads_cache: dict[int, dict[str, dict]] = {}


def is_configured() -> bool:
    return bool(UON_API_KEY and UON_ACCOUNT_ID)


def crm_lead_url(lead_id) -> str:
    if not UON_ACCOUNT_ID or not lead_id: return ""
    return f"https://id{UON_ACCOUNT_ID}.u-on.ru/request_edit.php?r_id={lead_id}"


async def _get(session: aiohttp.ClientSession, path: str):
    """Запрос к U-ON. Возвращает (data, status, error_text).

    Особенность: на «нет данных» U-ON отдаёт HTTP 404 с валидным JSON-телом
    вида {"result":404,"requests":[]} — это штатный ответ, мы парсим тело и
    возвращаем его как data, чтобы вызывающий код увидел пустой массив, а не
    None. Логируем такой случай в DEBUG, чтобы не засорять лог WARNING'ами.
    """
    url = f"{UON_BASE}/{UON_API_KEY}/{path}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            txt = await r.text()
            data = None
            try:
                import json as _json
                data = _json.loads(txt)
            except Exception:
                data = None
            if r.status == 200:
                if data is None:
                    return None, 200, f"non-JSON: {txt[:200]}"
                return data, 200, ""
            # Не-200: 404 с валидным телом — штатный «нет данных», не варнинг.
            if r.status == 404 and isinstance(data, dict):
                logger.debug(f"U-ON {path} → 404 (пусто)")
                return data, 404, ""
            logger.warning(f"U-ON {path} → HTTP {r.status}: {txt[:200]}")
            return None, r.status, txt[:300]
    except Exception as e:
        logger.error(f"U-ON GET {path}: {e}")
        return None, 0, str(e)


def _as_list(v):
    if v is None: return []
    if isinstance(v, list): return v
    if isinstance(v, dict): return [v]
    return []


def _pick(d: dict, *keys):
    """Первое непустое значение из перечисленных ключей."""
    if not isinstance(d, dict): return ""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0, "0", "0.00"): return v
    return ""


def _digits(s) -> str:
    """Оставляет только цифры — для нормализации телефонов."""
    if s is None: return ""
    return "".join(ch for ch in str(s) if ch.isdigit())


def _normalize_phone(p: str) -> str:
    """Нормализует телефон до 10 цифр (без кода страны), чтобы +7..., 8...,
    7... и просто 9991234567 матчились друг с другом."""
    d = _digits(p)
    if len(d) == 11 and d[0] in ("7", "8"):
        d = d[1:]
    return d


def classify_query(phrase: str) -> str:
    """Угадывает тип запроса: 'id' (номер заявки), 'phone' (телефон) или 'name'."""
    raw = (phrase or "").strip()
    digits = _digits(raw)
    # Если пользователь ввёл что-то нецифровое (буквы) — это имя/email.
    if any(ch.isalpha() for ch in raw):
        return "name"
    # Только цифры/пунктуация: длинное (>=10 цифр) — телефон, иначе — id заявки.
    if len(digits) >= 10:
        return "phone"
    if 1 <= len(digits) <= 7:
        return "id"
    return "name"


def _person_matches(p: dict, phrase_lower: str, *, qtype: str = "name") -> bool:
    """Совпадение по ФИО/телефону/email — для заказчика или туриста.
    qtype: 'name' (подстрока по ФИО/email), 'phone' (по нормализованным цифрам),
    'id' (этот режим к персоне не применяется — обрабатывается на уровне заявки)."""
    if not isinstance(p, dict): return False
    if qtype == "phone":
        target = _normalize_phone(phrase_lower)
        if not target: return False
        # Имена телефонных полей у разных кабинетов U-ON отличаются — поэтому
        # проходим по ВСЕМ строковым значениям записи, нормализуем до цифр и
        # ищем вхождение. Так поймаем телефон под любым ключом (phone, tel,
        # telephone, phone_mobile, contact_phone, …), включая вложенные
        # массивы туристов мы не разбираем здесь — это делает скан выше.
        for k, v in p.items():
            if isinstance(k, str) and k.startswith("_"):
                continue  # служебные поля, добавленные нами (_client, …)
            if v in (None, "", 0, "0"):
                continue
            if not isinstance(v, (str, int)):
                continue
            digits = _normalize_phone(v)
            # Игнорим короткие числовые поля (id, кол-ва) — они не телефоны.
            if len(digits) < 7:
                continue
            if target in digits:
                return True
        return False
    # name (по умолчанию) — подстрока по ФИО/email
    fields = (
        _pick(p, "surname", "client_surname", "u_surname", "last_name", "fam",
              "t_surname", "tourist_surname", "familiya", "family"),
        _pick(p, "name", "client_name", "u_name", "first_name", "imya",
              "t_name", "tourist_name", "given_name"),
        _pick(p, "middle_name", "client_middle_name", "u_middle_name", "patronymic", "otch",
              "t_middle_name", "tourist_middle_name"),
        _pick(p, "email", "client_email", "u_email", "t_email", "tourist_email"),
    )
    for f in fields:
        if f and phrase_lower in str(f).lower():
            return True
    return False


# Ключи в записи заявки, которые НЕ являются её собственным ID
# (это id связанных сущностей: менеджер, статус, заказчик и т.п.).
# Нужны для исключения ложных совпадений в _lead_id_matches.
_NOT_LEAD_ID_KEYS = frozenset({
    "manager_id", "user_id", "u_id", "client_id", "id_client", "id_user",
    "id_manager", "id_status", "status_id", "id_country", "country_id",
    "id_region", "region_id", "id_city", "city_id", "id_currency", "currency_id",
    "tour_operator_id", "id_tour_operator", "operator_id", "id_operator",
    "id_office", "office_id", "id_source", "source_id",
})


def _lead_id_matches(lead: dict, query_digits: str) -> bool:
    """Точное совпадение query_digits с ID заявки. Проверяет заведомо
    известные ключи (id/lead_id/r_id/request_id), а также любой ключ,
    оканчивающийся на _id (кроме связанных сущностей вроде manager_id).
    Дополнительно — подстрочное совпадение с номером договора."""
    if not isinstance(lead, dict) or not query_digits: return False
    # 1) Известные имена ID самой заявки
    for k in ("id", "lead_id", "r_id", "request_id", "lead_number", "number"):
        v = lead.get(k)
        if v and str(v).strip() == query_digits:
            return True
    # 2) Регистронезависимый и расширенный обход — если кабинет U-ON
    #    кладёт ID под нестандартным именем (ID, Lead_ID, и т.п.).
    for k, v in lead.items():
        if not isinstance(k, str): continue
        kl = k.lower()
        if kl in _NOT_LEAD_ID_KEYS:
            continue
        if kl in ("id", "lead_id", "r_id", "request_id", "lead_number", "number") \
           or (kl.endswith("_id") and kl not in _NOT_LEAD_ID_KEYS):
            if v and str(v).strip() == query_digits:
                return True
    # 3) Запасной матч — по номеру договора (там тоже бывают цифры)
    contract = _pick(lead, "contract_number", "number_contract", "contract",
                     "nomer_dogovora", "dogovor", "n_dogovor")
    if contract and query_digits in str(contract):
        return True
    return False


def _lead_tourists(lead: dict) -> list:
    """Достаёт массив туристов из заявки. U-ON кладёт туристов в разные ключи
    в разных версиях кабинетов, поэтому пробуем несколько."""
    for k in ("tourists", "tourist", "u_tourists", "u_tourist",
              "travellers", "traveler", "travellers",
              "passengers", "members", "participants"):
        v = lead.get(k)
        if v: return _as_list(v)
    return []


async def _list_clients_matching(s: aiohttp.ClientSession, phrase_lower: str,
                                  qtype: str = "name") -> tuple[list, str]:
    """Листает /users/{page}.json и возвращает заказчиков, у которых есть
    совпадение по фразе. Возвращает (список, диагностика)."""
    found = []
    last_size = None
    diag = ""
    for page in range(1, LEAD_SCAN_PAGES + 1):
        data, status, err = await _get(s, f"users/{page}.json")
        if data is None:
            diag = f"users/{page}.json→HTTP{status}"
            break
        items = _as_list(data.get("users") or data.get("clients") or data.get("user"))
        if not items: break
        for u in items:
            if _person_matches(u, phrase_lower, qtype=qtype):
                found.append(u)
        if last_size is not None and len(items) < last_size:
            break
        last_size = len(items)
    return found, diag


async def _scan_leads_for_phrase(s: aiohttp.ClientSession, phrase_lower: str,
                                  qtype: str = "name") -> tuple[list, str]:
    """Листает /lead/{page}.json и возвращает заявки, в которых:
    - qtype='name'  → совпала фамилия/email заказчика или туриста;
    - qtype='phone' → совпал телефон заказчика или туриста (нормализованный);
    - qtype='id'    → ID заявки (или номер договора) равен запросу.
    Это единственный способ найти заявку по фамилии туриста — отдельной
    коллекции туристов в U-ON API нет.
    Для точных запросов (id/phone) сканируем глубже, чтобы не пропустить
    архивные заявки."""
    query_digits = _digits(phrase_lower) if qtype == "id" else ""
    max_pages = LEAD_SCAN_PAGES_DEEP if qtype in ("id", "phone") else LEAD_SCAN_PAGES
    found = []
    last_size = None
    diag = ""
    pages_scanned = 0
    logged_keys = False
    # U-ON использует разные endpoint'ы в разных аккаунтах — пробуем оба
    base_path = None
    for candidate in ("lead", "request"):
        test, st, _ = await _get(s, f"{candidate}/1.json")
        if test is not None:
            items = _as_list(test.get("leads") or test.get("lead")
                             or test.get("requests") or test.get("request"))
            if items:
                base_path = candidate
                logger.info(f"U-ON scan: используем /{candidate}/{{page}}.json")
                break
        logger.debug(f"U-ON scan: /{candidate}/1.json пустой или ошибка (st={st})")
    if base_path is None:
        logger.warning("U-ON scan: оба endpoint'а /lead/ и /request/ вернули пусто")
        return found, "lead/1.json и request/1.json — нет данных"
    for page in range(1, max_pages + 1):
        data, status, err = await _get(s, f"{base_path}/{page}.json")
        if data is None:
            diag = f"{base_path}/{page}.json→HTTP{status}"
            break
        leads = _as_list(data.get("leads") or data.get("lead") or data.get("requests") or data.get("request"))
        if not leads: break
        pages_scanned += 1
        # Один раз в скан логируем ключи первой заявки — чтобы по логам
        # видеть, под какими именами U-ON реально отдаёт id/phone/etc.
        if not logged_keys and leads and isinstance(leads[0], dict):
            first = leads[0]
            logger.info(f"U-ON /{base_path}/1.json sample lead keys: {sorted(first.keys())}")
            # Логируем ключи туристов если они есть в первой заявке
            tourists_sample = _lead_tourists(first)
            if tourists_sample and isinstance(tourists_sample[0], dict):
                logger.info(f"U-ON tourist keys: {sorted(tourists_sample[0].keys())}")
            else:
                # Какой ключ содержит туристов?
                tourist_like = {k: type(v).__name__ for k, v in first.items()
                                if isinstance(v, (list, dict)) and k not in ("_client",)}
                logger.info(f"U-ON lead nested fields (potential tourists): {tourist_like}")
            logged_keys = True
        for ld in leads:
            matched_tourist = None
            if qtype == "id":
                if not _lead_id_matches(ld, query_digits):
                    continue
            elif _person_matches(ld, phrase_lower, qtype=qtype):
                pass
            else:
                tourists = _lead_tourists(ld)
                for t in tourists:
                    if _person_matches(t, phrase_lower, qtype=qtype):
                        matched_tourist = t
                        break
                if matched_tourist is None and qtype == "name":
                    # Запасной вариант: ищем подстроку по всем строковым значениям
                    # заявки (вдруг ФИО туриста лежит под нестандартным ключом)
                    for k, v in ld.items():
                        if k.startswith("_"): continue
                        if isinstance(v, str) and phrase_lower in v.lower():
                            matched_tourist = {"_raw_match": k, k: v}
                            break
                if matched_tourist is None:
                    continue
            if matched_tourist is not None:
                ld["_matched_tourist"] = matched_tourist
            found.append(ld)
        if last_size is not None and len(leads) < last_size:
            break
        last_size = len(leads)
    logger.info(f"U-ON scan_leads «{phrase_lower}» (qtype={qtype}): "
                f"страниц={pages_scanned}, найдено={len(found)}")
    return found, diag


async def _paged_get_leads(s: aiohttp.ClientSession, base_path: str) -> tuple[list, str]:
    """Тянет все страницы из <base_path>/{page}.json. Возвращает (заявки, diag)."""
    leads = []
    last_size = None
    diag = ""
    for page in range(1, LEAD_SCAN_PAGES + 1):
        data, status, err = await _get(s, f"{base_path}/{page}.json")
        if data is None:
            diag = f"{base_path}/{page}.json→HTTP{status}"
            break
        page_leads = _as_list(data.get("leads") or data.get("lead") or data.get("requests"))
        if not page_leads: break
        leads.extend(page_leads)
        if last_size is not None and len(page_leads) < last_size:
            break
        last_size = len(page_leads)
    return leads, diag


async def leads_by_client(client_id) -> tuple[list, str]:
    """Заявки по ID заказчика через /request-by-client/{id}/{page}.json."""
    async with aiohttp.ClientSession() as s:
        leads, diag = await _paged_get_leads(s, f"request-by-client/{client_id}")
    return leads, diag


async def get_lead(lead_id) -> dict | None:
    async with aiohttp.ClientSession() as s:
        # Пробуем оба известных эндпоинта U-ON для получения заявки по ID
        for path in (f"lead/{lead_id}.json", f"request/{lead_id}.json"):
            data, status, err = await _get(s, path)
            if not data:
                continue
            lead = data.get("lead") or data.get("request") or data.get("requests")
            if isinstance(lead, list):
                return lead[0] if lead else None
            if isinstance(lead, dict):
                return lead
    return None


def _fmt_money(v) -> str:
    if v in (None, "", 0, "0", "0.00"): return ""
    try:
        f = float(str(v).replace(",", ".").replace(" ", ""))
        if f == int(f): return f"{int(f):,}".replace(",", " ")
        return f"{f:,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)


def fmt_lead_card(lead: dict) -> tuple[str, str]:
    """Возвращает (текст карточки, URL заявки в U-ON)."""
    lid = _pick(lead, "id", "lead_id", "r_id")
    fio = " ".join(filter(None, [
        _pick(lead, "client_surname", "surname", "u_surname", "fam"),
        _pick(lead, "client_name", "name", "u_name", "imya"),
        _pick(lead, "client_middle_name", "middle_name", "u_middle_name", "otch"),
    ])).strip()
    contract_num = _pick(lead, "contract_number", "number_contract", "contract",
                          "nomer_dogovora", "dogovor", "n_dogovor")
    contract_date = _pick(lead, "contract_date", "date_contract", "data_dogovora", "dat_dogovor")
    operator = _pick(lead, "tour_operator", "operator", "touroperator", "tour_operator_name",
                     "name_touroperator", "operator_name", "to_name")
    operator_num = _pick(lead, "tour_operator_number", "touroperator_number",
                         "tour_number", "number_tour", "tour_op_number",
                         "nomer_zayavki", "zayavka_to", "to_number", "n_zayavka")
    country = _pick(lead, "country", "country_name", "name_country", "strana")
    region = _pick(lead, "region", "region_name", "name_region", "kurort", "city")
    date_from = _pick(lead, "from_date", "date_from", "departure", "dat_begin",
                      "data_zaezda", "datebegin", "date_begin")
    date_to = _pick(lead, "to_date", "date_to", "return", "dat_end",
                    "data_viezda", "dateend", "date_end")
    nights = _pick(lead, "nights", "nochej", "nochey", "n_nights")
    cost = _pick(lead, "cost", "price", "sum", "summa", "total", "total_cost",
                 "tour_cost", "cost_total", "summa_total", "summ")
    paid = _pick(lead, "pay", "paid", "payed", "clean_pay",
                 "summa_oplacheno", "oplacheno", "summa_pay", "summ_pay", "pay_summa")
    remainder = _pick(lead, "remainder", "debt", "balance", "ostatok",
                      "summa_dolg", "dolg", "doplata", "summa_doplata",
                      "to_pay", "summ_debt", "debt_summa")
    if not remainder and cost and paid:
        try: remainder = float(str(cost).replace(",", ".")) - float(str(paid).replace(",", "."))
        except Exception: pass
    status = _pick(lead, "status_name", "status", "name_status", "status_text")

    # Турист, по которому совпала фамилия (если совпадало по туристу, а не заказчику)
    mt = lead.get("_matched_tourist") if isinstance(lead, dict) else None
    matched_tourist_fio = ""
    if isinstance(mt, dict):
        matched_tourist_fio = " ".join(filter(None, [
            _pick(mt, "u_surname", "surname", "last_name"),
            _pick(mt, "u_name", "name", "first_name"),
        ])).strip()

    lines = [f"<b>Заявка #{lid}</b>" if lid else "<b>Заявка</b>"]
    if fio: lines.append(f"👤 {fio}")
    if matched_tourist_fio and matched_tourist_fio.lower() != fio.lower():
        lines.append(f"🧳 Турист: {matched_tourist_fio}")
    if contract_num:
        s = f"📄 Договор {contract_num}"
        if contract_date: s += f" от {contract_date}"
        lines.append(s)
    tour_parts = [str(p) for p in (country, region) if p]
    tour_str = ", ".join(tour_parts)
    if date_from or date_to:
        tour_str += f" • {date_from}"
        if date_to: tour_str += f" — {date_to}"
    if nights: tour_str += f" ({nights} н)"
    tour_str = tour_str.strip(", •")
    if tour_str: lines.append(f"✈️ {tour_str}")
    if operator:
        s = f"🏢 {operator}"
        if operator_num: s += f" / бронь {operator_num}"
        lines.append(s)
    if cost: lines.append(f"💰 Сумма: {_fmt_money(cost)} руб")
    if paid: lines.append(f"✅ Оплачено: {_fmt_money(paid)} руб")
    if remainder and _fmt_money(remainder): lines.append(f"❗ <b>Остаток: {_fmt_money(remainder)} руб</b>")
    if status: lines.append(f"📌 Статус: {status}")
    return "\n".join(lines), crm_lead_url(lid)


def _kb_for_lead(url: str) -> AttachmentButton | None:
    if not url: return None
    return AttachmentButton(
        type=AttachmentType.INLINE_KEYBOARD,
        payload=ButtonsPayload(buttons=[[LinkButton(text="🔗 Открыть в U-ON", url=url)]])
    )


async def start_search(msg):
    """Точка входа из меню «📦 Заказы → 🔍 Найти заявку». msg — maxapi Message."""
    if not is_configured():
        await msg.answer(
            "U-ON не настроен. Добавь переменные окружения:\n"
            "<code>UON_API_KEY</code> и <code>UON_ACCOUNT_ID</code>."
        )
        return
    uon_states[msg.sender.user_id] = {"step": "uon_search"}
    await msg.answer(
        "Введи фамилию, номер заявки или телефон:\n"
        "• <i>Иванова</i> — поиск по ФИО заказчика/туриста\n"
        "• <i>727</i> — поиск по номеру заявки\n"
        "• <i>+7 999 123 45 67</i> — поиск по телефону"
    )


async def handle_text(msg) -> bool:
    """Обработать ввод поискового запроса. msg — maxapi Message. Возвращает True если шаг активен."""
    uid = msg.sender.user_id
    if uon_states.get(uid, {}).get("step") != "uon_search": return False
    phrase = (msg.body.text or "").strip()
    if len(phrase) < 2:
        await msg.answer("Минимум 2 символа. Попробуй ещё раз:")
        return True
    uon_states.pop(uid, None)
    if not is_configured():
        await msg.answer("U-ON не настроен (UON_API_KEY/UON_ACCOUNT_ID).")
        return True
    qtype = classify_query(phrase)
    qtype_label = {"id": "номер заявки", "phone": "телефон", "name": "ФИО"}[qtype]
    w = await msg.answer(f"Ищу «{phrase}» ({qtype_label}) в U-ON…")
    phrase_lower = phrase.lower()

    all_leads = []
    seen_lead_ids = set()
    diag_parts = []

    # 0) Для поиска по ID — сначала пробуем прямой запрос по номеру (быстро)
    if qtype == "id":
        query_digits = _digits(phrase)
        direct = await get_lead(query_digits)
        if direct and isinstance(direct, dict):
            lid = str(_pick(direct, "id", "lead_id", "r_id", "request_id") or "")
            all_leads.append(direct)
            if lid: seen_lead_ids.add(lid)

    # 1) Совпадения по заказчикам в /users/{page}.json → их заявки через
    #    /request-by-client/{id}/{page}.json. Это быстрый путь — обычно
    #    хватает, если ищут заказчика по ФИО или телефону. При поиске
    #    по номеру заявки этот шаг бесполезен — пропускаем.
    async with aiohttp.ClientSession() as s:
        if qtype != "id":
            clients, c_diag = await _list_clients_matching(s, phrase_lower, qtype=qtype)
            if c_diag: diag_parts.append(f"users: {c_diag}")
            logger.info(f"U-ON «{phrase}» ({qtype}): совпало заказчиков={len(clients)}")
        else:
            clients, c_diag = [], ""
        for u in clients[:10]:
            c_id = _pick(u, "id", "u_id", "user_id")
            if not c_id: continue
            leads, ld_diag = await _paged_get_leads(s, f"request-by-client/{c_id}")
            for ld in leads:
                lid = str(_pick(ld, "id", "lead_id") or "")
                if lid and lid in seen_lead_ids: continue
                if lid: seen_lead_ids.add(lid)
                ld["_client"] = u
                all_leads.append(ld)

        # 2) Полнотекстовый скан /lead/{page}.json — чтобы найти заявки
        #    по фамилии туриста (а не заказчика), по телефону туриста,
        #    или по номеру самой заявки. При поиске по ID пропускаем, если
        #    прямой запрос уже нашёл заявку.
        if qtype == "id" and all_leads:
            scanned_leads, s_diag = [], ""
        else:
            scanned_leads, s_diag = await _scan_leads_for_phrase(s, phrase_lower, qtype=qtype)
        if s_diag: diag_parts.append(f"lead: {s_diag}")
        for ld in scanned_leads:
            lid = str(_pick(ld, "id", "lead_id") or "")
            if lid and lid in seen_lead_ids: continue
            if lid: seen_lead_ids.add(lid)
            all_leads.append(ld)

    if not all_leads:
        search_url = f"https://id{UON_ACCOUNT_ID}.u-on.ru/" if UON_ACCOUNT_ID else ""
        text = (f"В U-ON не нашёл заявок с «{phrase}» ({qtype_label}).\n\n"
                f"Поищи вручную в U-ON — там поиск по туристам работает через приложение:")
        kb = None
        if search_url:
            kb = AttachmentButton(
                type=AttachmentType.INLINE_KEYBOARD,
                payload=ButtonsPayload(buttons=[[LinkButton(text="🔍 Открыть U-ON", url=search_url)]])
            )
        if diag_parts:
            text += f"\n\n<i>Диагностика API: {'; '.join(diag_parts[:2])}</i>"
        try:
            atts = [kb] if kb else None
            await w.edit(text=text, attachments=atts)
        except Exception: pass
        return True

    uid_cache: dict[str, dict] = {}
    for ld in all_leads:
        lid_str = str(_pick(ld, "id", "lead_id") or "")
        if lid_str: uid_cache[lid_str] = ld
    uon_leads_cache[uid] = uid_cache

    if len(all_leads) == 1:
        ld = all_leads[0]
        lid = _pick(ld, "id", "lead_id")
        if isinstance(ld, dict):
            logger.info(f"U-ON lead #{lid} keys: {sorted(ld.keys())}")
        text, url = fmt_lead_card(ld)
        kb = _kb_for_lead(url)
        atts = [kb] if kb else None
        try:
            await w.edit(text=text, attachments=atts)
        except Exception:
            await msg.answer(text, attachments=atts)
        return True

    # Несколько — показать список
    btn_rows = []
    for ld in all_leads[:20]:
        cl = ld.get("_client") or {}
        mt = ld.get("_matched_tourist")
        person = mt if isinstance(mt, dict) else (cl if cl else ld)
        surname = _pick(person, "u_surname", "surname", "client_surname", "last_name")
        name = _pick(person, "u_name", "name", "client_name", "first_name")
        initial = (str(name)[:1] + ".") if name else ""
        country = _pick(ld, "country", "country_name")
        df = _pick(ld, "from_date", "date_from")
        lid = _pick(ld, "id", "lead_id")
        label = f"#{lid} {surname} {initial} {country} {df}".strip()
        if not lid: continue
        btn_rows.append([CallbackButton(text=label[:60], payload=f"uon_l_{lid}")])
    head = f"Найдено заявок: <b>{len(all_leads)}</b>. Выбери:"
    kb_list = AttachmentButton(
        type=AttachmentType.INLINE_KEYBOARD,
        payload=ButtonsPayload(buttons=btn_rows)
    )
    try:
        await w.edit(text=head, attachments=[kb_list])
    except Exception:
        await msg.answer(head, attachments=[kb_list])
    return True


@uon_router.message_callback(F.callback.payload.startswith("uon_l_"))
async def cb_uon_lead(event: MessageCallback):
    lid = event.callback.payload[len("uon_l_"):]
    uid = event.callback.user.user_id
    lead = (uon_leads_cache.get(uid) or {}).get(lid)
    if not isinstance(lead, dict):
        lead = await get_lead(lid)
    if not lead:
        await event.bot.send_callback(event.callback.callback_id, notification="Заявка не найдена — повтори поиск.")
        return
    if isinstance(lead, dict):
        logger.info(f"U-ON lead #{lid} keys: {sorted(lead.keys())}")
    text, url = fmt_lead_card(lead)
    kb = _kb_for_lead(url)
    atts = [kb] if kb else None
    await event.message.answer(text, attachments=atts)
    await event.bot.send_callback(event.callback.callback_id)
