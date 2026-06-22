"""
Модуль учёта калорий и БЖУК для одного владельца (NUTRITION_OWNER_USER_ID).
Хранение — в Google Sheets (отдельная таблица, SPREADSHEET_ID_NUTRITION).
Распознавание — Gemini Vision (текст или фото).
"""
import asyncio, base64, io, json, logging, time, html, os
from datetime import datetime, timedelta, timezone, date
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from utils import safe_phone

logger = logging.getLogger(__name__)

# ==================== глобалы (заполняет init_nutrition) ====================
gc_client = None
bot_instance = None
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_KEYS: list[str] = []
_gemini_key_index = 0

def _refresh_gemini_keys():
    global _GEMINI_KEYS
    keys = [k for k in [
        os.environ.get("GEMINI_API_KEY", GEMINI_API_KEY),
        os.environ.get("GEMINI_API_KEY_2", ""),
        os.environ.get("GEMINI_API_KEY_3", ""),
    ] if k]
    _GEMINI_KEYS = keys or [GEMINI_API_KEY]

def _get_gemini_key() -> str:
    _refresh_gemini_keys()
    return _GEMINI_KEYS[_gemini_key_index % len(_GEMINI_KEYS)]

def _rotate_gemini_key():
    global _gemini_key_index
    _gemini_key_index += 1
SPREADSHEET_ID_NUTRITION = ""
OWNER_USER_ID = 0
TZ_OFFSET = timezone(timedelta(hours=3))  # Europe/Moscow

nutrition_router = Router()
nutr_states: dict[int, dict] = {}

# Кэш ссылок на листы
_sheets_cache: dict[str, object] = {}

MEALS_HEADER = [
    "id", "user_id", "eaten_at", "meal_name", "grams",
    "kcal", "protein", "fat", "carbs", "fiber",
    "items_json", "confidence", "source", "photo_file_id",
    "raw_input", "tg_chat_id", "tg_msg_id", "created_at", "deleted_at",
]
SETTINGS_HEADER = [
    "user_id", "daily_kcal", "target_p", "target_f", "target_c",
    "height_cm", "activity", "updated_at",
]
WEIGHT_HEADER = ["id", "user_id", "measured_at", "weight_kg", "note"]

DEFAULT_DAILY_KCAL = 1380
DEFAULT_TARGET_P = 69
DEFAULT_TARGET_F = 46
DEFAULT_TARGET_C = 172


def init_nutrition(claude, gc, bot, owner_id: int, spreadsheet_id: str, model: str = None):
    global gc_client, bot_instance, OWNER_USER_ID, SPREADSHEET_ID_NUTRITION, GEMINI_API_KEY
    gc_client = gc
    bot_instance = bot
    OWNER_USER_ID = int(owner_id)
    if not GEMINI_API_KEY:
        GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    SPREADSHEET_ID_NUTRITION = spreadsheet_id
    logger.info(f"nutrition: init (owner={OWNER_USER_ID}, spreadsheet={SPREADSHEET_ID_NUTRITION[:10]}..., model={GEMINI_MODEL})")


def is_owner(uid: int) -> bool:
    return OWNER_USER_ID and uid == OWNER_USER_ID


# ==================== Промпты ====================

NUTRITION_SYSTEM_PROMPT = """Ты — нутрициолог-калькулятор для русскоязычного дневника питания.
Задача: по описанию или фото блюда оценить вес, калорийность и БЖУК
(белки, жиры, углеводы, клетчатка) максимально близко к реальности.

═══════════════════════════════════════════════════════════════
МЕТОДИКА (формула Атуотера)
═══════════════════════════════════════════════════════════════
Калории = 4×Белки + 9×Жиры + 4×Углеводы + 2×Клетчатка.
(Спирт = 7 ккал/г.)

После расчёта обязательно сверь: total.kcal сходится с этой формулой
±10%. Не сходится — пересчитай отдельные компоненты.

Источники табличных значений (по приоритету):
1. Скурихин — для русской/советской кухни.
2. USDA — для международных продуктов.
3. Данные на упаковке — если виден бренд.

═══════════════════════════════════════════════════════════════
БАЗОВЫЕ ПРИНЦИПЫ
═══════════════════════════════════════════════════════════════

ВЕС ПОРЦИИ:
- В тексте указаны граммы/мл → используй точно, confidence="high".
- В тексте «N штук» → каждая штука = типичная единица (см. ШТУКИ ниже).
- Описание словесное без размера → бери типичную домашнюю порцию,
  confidence="medium", в notes напиши «Оценила как N-граммовую порцию,
  если другая — нажми «Исправить»».
- Описание скудное («съел что-то», «обед в кафе») → confidence="low",
  уточняющий вопрос в notes.

В каждом item.name указывай «(около Xг)» — это снижает ощущение
фальшивой точности.

КУЛИНАРНАЯ ОБРАБОТКА:
- Жарка на сковороде: +5–10 г масла на порцию (45–90 ккал к жирам).
- Фритюр: +15–30 г масла.
- Запекание/тушение/варка/гриль/пар — без добавления масла.
- В тексте «варёное / тушёное / паровое» — масло НЕ добавляй.
- На фото золотистая корочка / блеск / румяные края — это жарка,
  добавь масло.

ВАРЁНОЕ vs СЫРОЕ:
- Крупы/макароны/рис без уточнения → ВАРЁНАЯ порция (~110–130 ккал/100 г).
  «100 г сухой гречки» = 330 ккал; «100 г гречки» (без уточнения) = 110.
- Мясо/рыба → всегда учитываем съеденную (готовую) массу.
- Каша «на молоке» — добавь молоко отдельным item-ом.

СОСТАВНЫЕ БЛЮДА — РАЗБИВАЙ:
- Гарнир + мясо + овощи → 3 items.
- Бутерброд → хлеб + начинка.
- Салат → база + заправка + добавки (сыр / орехи / курица).
- Каша + молоко + масло + сахар → 4 items.
- Бургер → булка + котлета + сыр + соус + овощи.
- Борщ/суп → жидкое + сметана отдельно (если видно).

═══════════════════════════════════════════════════════════════
РАСПОЗНАВАНИЕ ПО ФОТО
═══════════════════════════════════════════════════════════════

ЯКОРЯ РАЗМЕРА:
- Плоская тарелка: 24–26 см диаметр.
- Глубокая тарелка для супа: 22–24 см, ~250–300 мл при заполнении.
- Десертная: 18–20 см.
- Столовая ложка: 15 мл, чайная: 5 мл.
- Кружка/чашка кофе: 200–240 мл.
- Стакан: 200–250 мл.

МАРКЕРЫ ПРИГОТОВЛЕНИЯ:
- Золотисто-коричневая корочка → жарка/запекание.
- Блеск, капли жира → жарка.
- Бледная матовая → варка/пар/тушение.
- Сметана/майонез отдельной горкой → ОТДЕЛЬНЫЙ item.
- Тёртый сыр сверху → ОТДЕЛЬНЫЙ item.
- Гренки/сухарики в супе → ОТДЕЛЬНЫЙ item (40 г = 130 ккал).

ЧТО НЕ ВЫДУМЫВАЙ:
- Напиток (сок, чай, кофе, морс, лимонад) — ТОЛЬКО при явно видимом
  сосуде (стакан, бокал, чашка, бутылка). Нет сосуда = нет напитка.
- Тёртая морковь / морковь по-корейски — это твёрдый САЛАТ или гарнир,
  а НЕ сок. Маркеры: оранжевые ниточки на тарелке без жидкости.
- Тонкая лужица рядом с блюдом — маринад/выделившийся сок, не напиток.
- Если что-то на фото неузнаваемо — НЕ выдумывай еду; в notes пиши
  «не уверена что это», confidence="low".

═══════════════════════════════════════════════════════════════
РАСПОЗНАВАНИЕ ИЗ ТЕКСТА
═══════════════════════════════════════════════════════════════

ЕДИНИЦЫ (если без явных грамм):
- «миска» супа → 300 мл.
- «тарелка» гарнира → 200–250 г.
- «кусок/ломтик» хлеба → 30 г, «горбушка» → 40–50 г.
- «горсть» (орехи) → 30 г, (овощи) → 50–80 г.
- «ложка» (без уточнения) → столовая = 15 мл.
- «чайная ложка» → 5 мл.
- «стакан» → 200–250 мл.
- «банка консервы» → 160–185 г.
- «пачка творога» → 200 г.
- «щепотка» → 1–3 г, в калориях не учитывай.

ШТУКИ — НЕ ПРИДУМЫВАЙ ФОРМУ:
«N штук» какого-то продукта = N сырых/готовых ЦЕЛЫХ единиц, а не
переработанная форма. Конкретно:
- «курица N шт» → N кусков курицы (грудка/бедро/голень) по 100–130 г,
  НЕ котлеты.
- «рыба N шт» → N филейных кусков по 120–150 г.
- «яйцо/яйца N» → N куриных яиц С1 по 60 г.
- «помидор/огурец/банан/яблоко N шт» → целые плоды среднего размера
  (помидор 150 г, огурец 100 г, банан 120 г, яблоко 150 г).
- «сосиска N шт» → ~50 г каждая.
- «пельмени/сырники/блины/котлеты/биточки/фрикадельки N шт» — ТОЛЬКО
  если явно сказано это слово, тогда штука = 60–100 г в зависимости
  от типа.

ТИПИЧНЫЕ ДОМАШНИЕ ПОРЦИИ (без указания веса):
- Суп: 250–300 мл.
- Второе мясное/рыбное: 120–180 г.
- Гарнир (картошка/каша/макароны): 150–200 г.
- Овощи на пару/тушёные: 150 г.
- Салат: 100–150 г.
- Десерт: 80–120 г.
- Йогурт стаканчик: 125–150 г.
- Творог: 150–200 г.
- Котлета домашняя: 80–100 г, промышленная: 50–80 г.
- Блин тонкий: 40 г, толстый: 70 г.

РАЗМЕРНЫЕ СЛОВА:
- «большой / огромный» → +30–50% от типичного.
- «маленький / небольшой» → −30–50%.
- «половина / полпорции / 1/2» → ровно 50%.
- «треть / 1/3» → 33%, «четверть / 1/4» → 25%.
- «пара / парочка» → 2 шт.
- «несколько» → 3 шт.
- «чуть-чуть / капельку» → 10–20%.

═══════════════════════════════════════════════════════════════
СПЕЦИФИКА ПО ТИПАМ ПРОДУКТОВ
═══════════════════════════════════════════════════════════════

НАПИТКИ (ккал/100 мл):
- Кофе чёрный: ~1. С молоком (40 мл 2.5%): ~25.
- Капучино 250 мл: ~80. Латте 300 мл: ~120. Раф/флэт уайт: 140–180.
- Чай чёрный/зелёный без сахара: 0.
- Молоко 2.5%: 52, 3.2%: 60. Сливки 10%: 119. Кефир 1%: 40.
- Йогурт натуральный 2.5%: 60.
- Сок свежевыжатый: 40–45. Пакетированный: 45–50.
- Кола обычная: 42 (10 г сахара/100 мл). Zero/Light/Max: 0.
- Морс/компот без уточнения «без сахара»: 30 (с сахаром).
- Энергетик обычный: 45, sugar-free: 5.

САХАР В ЧАЕ/КОФЕ — ТОЛЬКО ЕСЛИ ЯВНО упомянут или виден.
1 ч.л. = 5 г = 20 ккал. 1 ст.л. = 25 г = 100 ккал.

КОНСТАНТЫ:
- Хлеб белый: 30 г = 80 ккал. Чёрный: 30 г = 65.
- Растительное масло: 1 ст.л. = 17 г = 153 ккал.
- Сливочное масло: 10 г = 75 ккал.
- Сметана 15%: 158 ккал/100 г, 20%: 206.
- Картофель отварной: 80 ккал/100 г, жареный: 200.
- Гречка варёная: 110, рис варёный: 130, паста варёная: 130.
- Яйцо отварное 60 г = 80, жареное (с маслом) = 110.

СБОРНЫЕ БЛЮДА (ккал/100 г):
- Пельмени: 250–280. Вареники с творогом: 170, с картошкой: 230,
  с вишней: 210.
- Хачапури по-аджарски: 280–320. Чебурек/беляш/самса: 270–300 (1 шт ~100 г).
- Голубец: 130 (1 шт ~150 г).
- Плов: ~250 (выше с бараниной, ниже с курицей).
- Шашлык готовый: свинина 270, курица 200, говядина 220, баранина 290.
- Роллы/суши: 200–350 (с темпурой/майонезом — выше).
- Пицца классическая: 260–280, 1 кусок 1/8 большой ~130 г.
- Бургер без бренда: 250. С брендом — данные производителя.
- Оливье / Селёдка под шубой: 195. Винегрет: 80. Греческий: 100.

СЛАДОСТИ:
- Конфета шоколадная: 50 ккал/шт (~10 г). Карамель/леденец: 20.
- Шоколад молочный: 540 ккал/100 г, 1 долька 5–7 г = 30–40 ккал.
  Тёмный/горький: 540–580.
- Печенье: 400–450 ккал/100 г, 1 шт 12–15 г = 50–65 ккал.
- Пирожное/эклер: 100 г = 300–400.
- Кусок торта без размера → 100–120 г, тип влияет: Наполеон 350,
  Прага 400, Медовик 380, Чизкейк 320, Тирамису 300 ккал/100 г.
- Мороженое пломбир: 220 ккал/100 г, эскимо: 250.
- Зефир/пастила/мармелад: 320 ккал/100 г, 1 шт ~30 г = 95.

СКРЫТЫЕ КАЛОРИИ — учитывай ОТДЕЛЬНОЙ строкой если видно/упомянуто:
- Заправка салата: 5–15 г = 45–135 ккал.
- Сметана: 10–20 г = 16–32.
- Кетчуп: 15–25 г = 15–35.
- Майонез на бутер/в салат: 10–20 г = 65–135.
- Хлеб к супу: НЕ добавляй автоматом — только если видно или явно
  упомянут.

НЕ ЕДА — возвращай {"error": "no_food_described"} если описание ТОЛЬКО:
- Таблетки, витамины, БАДы.
- Жвачка (можно вернуть error или 5 ккал/штука).
- Вода / минералка газированная без сиропа.
- Зелёный чай / травяной без сахара/молока (0 ккал).

═══════════════════════════════════════════════════════════════
УВЕРЕННОСТЬ
═══════════════════════════════════════════════════════════════
- "high" — есть граммы в тексте или фото отличного качества с понятной
  порцией. Доверительный интервал ±10%.
- "medium" — оценил визуально или по типичному размеру. ±20%. В notes
  обязательно: «Оценила как X-граммовую порцию, если другая — нажми
  «Исправить»».
- "low" — описание скудное/неоднозначное. ±30–40%. В notes ОБЯЗАТЕЛЬНО
  уточняющий вопрос («Какой размер порции?», «Что входило в обед?»).

═══════════════════════════════════════════════════════════════
ОКРУГЛЕНИЕ И ФОРМАТ
═══════════════════════════════════════════════════════════════
Калории: до целых. БЖУК: до 0.5 г. Граммы: до целых.

В total.name — короткое обобщающее название (≤60 знаков), без слов
«около» и без граммов. В item.name — наоборот, с «около Xг».

ОТВЕТ — СТРОГО JSON, без markdown-обёртки, без пояснений:
{
  "items": [
    {"name": "Жареная рыба (около 150г)", "grams": 150, "kcal": 270,
     "protein": 30, "fat": 15, "carbs": 0, "fiber": 0}
  ],
  "total": {
    "name": "Жареная рыба с пюре и капустой", "grams": 350,
    "kcal": 405, "protein": 34, "fat": 20, "carbs": 21, "fiber": 4
  },
  "confidence": "high",
  "notes": ""
}

ОШИБКИ:
- На фото нет еды → {"error": "no_food_detected"}
- В тексте нет описания еды → {"error": "no_food_described"}
"""

CORRECTION_SYSTEM_PROMPT = """Ты пересчитываешь ранее посчитанное блюдо
с учётом правки от пользователя. На входе:
1. Предыдущий расчёт в формате JSON (items + total).
2. Правка от пользователя текстом.

ПРИНЦИПЫ ПРАВКИ:
- Применяй точечно: если меняется вес одного компонента — пересчитай
  его пропорционально (БЖУК и kcal меняются в той же пропорции),
  остальные items не трогай.
- Если добавляется новый компонент — добавь его в items с расчётом по
  той же методике (Атуотер: 4/9/4 + 2 fiber).
- Если удаляется компонент — убери из items.
- После любой правки пересчитай total как сумму items.

САМОПРОВЕРКА: total.kcal ≈ 4*P + 9*F + 4*C + 2*Fiber (±10%).

Формат ответа — тот же JSON (items, total, confidence, notes), без
markdown-обёртки и пояснений. Если правка касалась веса/добавления — в
notes коротко напиши, что именно пересчитал.
"""


LEFTOVER_SYSTEM_PROMPT = """Ты пересчитываешь съеденное: пользователь не доел
всю порцию. На входе:
1. Изначальный расчёт блюда (items + total) в JSON — что было на тарелке.
2. Описание остатков: текст («осталась половина», «съела треть», «не доела
   почти ничего», «доела всё») ИЛИ фото остатков (то, что НЕ съели).

ЛОГИКА ПЕРЕСЧЁТА:
1. Определи долю съеденного (eaten_ratio, число от 0 до 1):
   - «доел/доела всё» / «съел/съела всё» / «всё» → 1.0 (нет изменений).
   - «съел/съела половину» / «осталась половина» → 0.5.
   - «съела треть» → 0.33. «осталась треть» → 0.67.
   - «съела чуть» / «почти ничего» → 0.2.
   - «осталось чуть-чуть» / «доела почти всё» → 0.9.
   - «осталось N грамм» от total.grams=G → eaten_ratio = (G − N) / G.
   - «съела N грамм» → eaten_ratio = N / G.
   - Процент явный («осталось 30%» → eaten_ratio = 0.7).
2. Если на ФОТО остатков:
   - Оцени массу остатков визуально (тарелка 24–26 см, ложка 15 мл, ладонь).
   - eaten_ratio = (total.grams − остатки_грамм) / total.grams.
   - Если порция распределена неравномерно (например, осталось пюре, но
     рыбу съели всю) — пересчитай items индивидуально, а не пропорционально.
3. Применяй коэффициент к КАЖДОМУ полю каждого item:
   new.grams = old.grams × eaten_ratio
   new.kcal = old.kcal × eaten_ratio
   new.protein = old.protein × eaten_ratio
   new.fat = old.fat × eaten_ratio
   new.carbs = old.carbs × eaten_ratio
   new.fiber = old.fiber × eaten_ratio
   (То же для total.)
4. В name каждого item обнови «(около Xг)» — поставь новый вес.
5. В notes напиши коротко: какую долю/сколько грамм съел, сколько осталось.

ОКРУГЛЕНИЕ: ккал до целых, БЖУК до 0.5 г, граммы до целых.

САМОПРОВЕРКА: total.kcal ≈ 4*P + 9*F + 4*C + 2*Fiber (±10%).

Если описание остатков непонятно («ну такое», «не очень»):
{"error": "unclear_leftover", "notes": "Не поняла. Скажи: половина / треть /
осталось N грамм, или пришли фото остатков."}

Если пользователь говорит «доела всё» — верни прежний расчёт без изменений
(скопируй items и total как есть), notes="Доела всё, без изменений".

Формат ответа — тот же JSON (items, total, confidence, notes), без
markdown-обёртки и пояснений.
"""


# ==================== Утилиты ====================

def _now_msk() -> datetime:
    return datetime.now(TZ_OFFSET)


def _today_msk() -> date:
    return _now_msk().date()


def _parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def _to_msk(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_OFFSET)
    return dt.astimezone(TZ_OFFSET)


def _f(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(str(v).replace(",", "."))
    except Exception:
        return float(default)


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _parse_claude_json(raw: str):
    """Локальная копия parse_claude_json из utils — модуль самодостаточен."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    import re
    jm = re.search(r'[\[{][\s\S]*[\]}]', text)
    if jm:
        text = jm.group(0)
    return json.loads(text)


# ==================== Sheets ====================

def _ensure_sheet(name: str, header: list[str]):
    """Возвращает worksheet; создаёт лист и шапку при отсутствии.
    Кэшируем ТОЛЬКО полностью валидный лист — иначе при retry создадим его
    заново или починим шапку без задвоения."""
    if name in _sheets_cache:
        return _sheets_cache[name]
    ss = gc_client.open_by_key(SPREADSHEET_ID_NUTRITION)
    try:
        ws = ss.worksheet(name)
        existing = True
    except Exception:
        logger.info(f"nutrition: creating sheet '{name}'")
        ws = ss.add_worksheet(title=name, rows=1000, cols=max(10, len(header) + 2))
        existing = False

    # Гарантируем валидную шапку. Делаем эту часть отдельно от add_worksheet,
    # чтобы при ошибке append_row при следующем вызове не было «лист уже есть».
    try:
        first_row = ws.row_values(1)
    except Exception as e:
        logger.warning(f"nutrition: row_values(1) on '{name}': {e}")
        first_row = []
    needs_header = (
        not first_row
        or first_row != header[:len(first_row)]
        or len(first_row) < len(header)
    )
    if needs_header:
        try:
            ws.update("A1", [header])
        except Exception as e:
            logger.warning(f"nutrition: header write for '{name}': {e}")
            # НЕ кэшируем — при следующем вызове попробуем починить шапку снова
            return ws

    _sheets_cache[name] = ws
    return ws


def _meals_ws():
    return _ensure_sheet("nutrition_meals", MEALS_HEADER)


def _settings_ws():
    return _ensure_sheet("nutrition_settings", SETTINGS_HEADER)


def _weight_ws():
    return _ensure_sheet("nutrition_weight", WEIGHT_HEADER)


def _new_id() -> int:
    return int(time.time() * 1000)


# Маппинг строки листа → dict
def _row_to_meal(row: list, idx: int) -> dict:
    """idx — номер строки в gspread (1-indexed, шапка=1)."""
    g = lambda i: row[i] if i < len(row) else ""
    return {
        "_row": idx,
        "id": int(g(0)) if g(0) else 0,
        "user_id": int(g(1)) if g(1) else 0,
        "eaten_at": g(2),
        "meal_name": g(3),
        "grams": _f(g(4)),
        "kcal": _f(g(5)),
        "protein": _f(g(6)),
        "fat": _f(g(7)),
        "carbs": _f(g(8)),
        "fiber": _f(g(9)),
        "items_json": g(10),
        "confidence": g(11),
        "source": g(12),
        "photo_file_id": g(13),
        "raw_input": g(14),
        "tg_chat_id": int(g(15)) if g(15) else 0,
        "tg_msg_id": int(g(16)) if g(16) else 0,
        "created_at": g(17),
        "deleted_at": g(18),
    }


def insert_meal(meal: dict) -> int:
    """Возвращает meal_id."""
    ws = _meals_ws()
    mid = _new_id()
    items_json = json.dumps(
        {"items": meal.get("items", []), "total": meal.get("total", {}),
         "confidence": meal.get("confidence", ""), "notes": meal.get("notes", "")},
        ensure_ascii=False,
    )
    total = meal.get("total", {}) or {}
    # Защита от формульной инъекции в Sheets: любые строковые поля,
    # начинающиеся с = + - @, экранируем апострофом через safe_phone().
    row = [
        mid,
        int(meal["user_id"]),
        meal.get("eaten_at") or _now_msk().isoformat(timespec="seconds"),
        safe_phone(total.get("name") or meal.get("meal_name", "")),
        total.get("grams", "") or "",
        total.get("kcal", 0) or 0,
        total.get("protein", "") or "",
        total.get("fat", "") or "",
        total.get("carbs", "") or "",
        total.get("fiber", "") or "",
        items_json,
        safe_phone(meal.get("confidence", "")),
        safe_phone(meal.get("source", "text")),
        safe_phone(meal.get("photo_file_id", "") or ""),
        safe_phone((meal.get("raw_input") or "")[:500]),
        meal.get("tg_chat_id", "") or "",
        meal.get("tg_msg_id", "") or "",
        _now_msk().isoformat(timespec="seconds"),
        "",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return mid


def _find_meal_row(meal_id: int):
    ws = _meals_ws()
    rows = ws.get_all_values()
    for idx, r in enumerate(rows[1:], start=2):
        if r and r[0] and str(r[0]) == str(meal_id):
            return ws, idx, _row_to_meal(r, idx)
    return ws, None, None


def get_meal(meal_id: int) -> dict | None:
    _, _, m = _find_meal_row(meal_id)
    return m


def soft_delete_meal(meal_id: int):
    ws, idx, _ = _find_meal_row(meal_id)
    if idx is None:
        return False
    ws.update_cell(idx, MEALS_HEADER.index("deleted_at") + 1,
                   _now_msk().isoformat(timespec="seconds"))
    return True


def update_meal_calc(meal_id: int, new_calc: dict):
    """Перезапись итогов и items_json после правки."""
    ws, idx, _ = _find_meal_row(meal_id)
    if idx is None:
        return False
    total = new_calc.get("total", {}) or {}
    items_json = json.dumps(
        {"items": new_calc.get("items", []), "total": total,
         "confidence": new_calc.get("confidence", ""), "notes": new_calc.get("notes", "")},
        ensure_ascii=False,
    )
    # Колонки 4..12 (1-indexed): meal_name, grams, kcal, protein, fat, carbs, fiber, items_json, confidence
    # Защита от формульной инъекции — safe_phone() для строковых полей.
    updates = [
        (4, safe_phone(total.get("name", "") or "")),
        (5, total.get("grams", "") or ""),
        (6, total.get("kcal", 0) or 0),
        (7, total.get("protein", "") or ""),
        (8, total.get("fat", "") or ""),
        (9, total.get("carbs", "") or ""),
        (10, total.get("fiber", "") or ""),
        (11, items_json),
        (12, safe_phone(new_calc.get("confidence", "") or "")),
    ]
    for col, val in updates:
        ws.update_cell(idx, col, val)
    return True


def list_meals_between(user_id: int, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Возвращает живые (deleted_at пуст) приёмы пищи в [start_dt, end_dt)."""
    ws = _meals_ws()
    rows = ws.get_all_values()
    out = []
    for idx, r in enumerate(rows[1:], start=2):
        if not r or not r[0]:
            continue
        m = _row_to_meal(r, idx)
        if m["user_id"] != user_id:
            continue
        if m["deleted_at"]:
            continue
        eaten = _parse_iso(m["eaten_at"])
        if not eaten:
            continue
        eaten = _to_msk(eaten)
        if start_dt <= eaten < end_dt:
            out.append(m)
    out.sort(key=lambda x: x["eaten_at"])
    return out


def get_settings(user_id: int) -> dict:
    ws = _settings_ws()
    rows = ws.get_all_values()
    for r in rows[1:]:
        if r and r[0] and str(r[0]) == str(user_id):
            return {
                "user_id": user_id,
                "daily_kcal": int(_f(r[1] if len(r) > 1 else "", DEFAULT_DAILY_KCAL)),
                "target_p": int(_f(r[2] if len(r) > 2 else "", DEFAULT_TARGET_P)),
                "target_f": int(_f(r[3] if len(r) > 3 else "", DEFAULT_TARGET_F)),
                "target_c": int(_f(r[4] if len(r) > 4 else "", DEFAULT_TARGET_C)),
                "height_cm": int(_f(r[5] if len(r) > 5 else "", 0)) or None,
                "activity": (r[6] if len(r) > 6 else "") or "low",
            }
    return {
        "user_id": user_id,
        "daily_kcal": DEFAULT_DAILY_KCAL,
        "target_p": DEFAULT_TARGET_P,
        "target_f": DEFAULT_TARGET_F,
        "target_c": DEFAULT_TARGET_C,
        "height_cm": None,
        "activity": "low",
    }


def set_settings(user_id: int, **fields):
    ws = _settings_ws()
    cur = get_settings(user_id)
    cur.update({k: v for k, v in fields.items() if v is not None})
    rows = ws.get_all_values()
    target_idx = None
    for idx, r in enumerate(rows[1:], start=2):
        if r and r[0] and str(r[0]) == str(user_id):
            target_idx = idx
            break
    new_row = [
        user_id,
        cur["daily_kcal"], cur["target_p"], cur["target_f"], cur["target_c"],
        cur.get("height_cm") or "", cur.get("activity") or "low",
        _now_msk().isoformat(timespec="seconds"),
    ]
    if target_idx:
        ws.update(f"A{target_idx}:H{target_idx}", [new_row])
    else:
        ws.append_row(new_row, value_input_option="USER_ENTERED")


def log_weight(user_id: int, weight_kg: float, note: str = "", on: date = None) -> bool:
    """Перезаписывает запись за дату, если уже была."""
    ws = _weight_ws()
    on = on or _today_msk()
    safe_note = safe_phone(note or "")
    rows = ws.get_all_values()
    for idx, r in enumerate(rows[1:], start=2):
        if (r and len(r) >= 3 and str(r[1]) == str(user_id)
                and r[2] == on.isoformat()):
            ws.update(f"A{idx}:E{idx}",
                      [[r[0] or _new_id(), user_id, on.isoformat(), weight_kg, safe_note]])
            return True
    ws.append_row([_new_id(), user_id, on.isoformat(), weight_kg, safe_note],
                  value_input_option="USER_ENTERED")
    return True


def list_weights(user_id: int, since: date = None) -> list[tuple[date, float]]:
    ws = _weight_ws()
    rows = ws.get_all_values()
    out = []
    for r in rows[1:]:
        if not r or len(r) < 4 or str(r[1]) != str(user_id):
            continue
        try:
            d = date.fromisoformat(r[2])
        except Exception:
            continue
        if since and d < since:
            continue
        out.append((d, _f(r[3])))
    out.sort(key=lambda x: x[0])
    return out


# ==================== Claude ====================

def _downscale(data: bytes, max_dim: int = 1600, max_bytes: int = 5 * 1024 * 1024) -> bytes:
    if len(data) <= max_bytes:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            if max(img.size) <= max_dim:
                return data
        except Exception:
            return data
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"nutrition: downscale failed: {e}")
        return data


async def _gemini_call(system: str, content) -> str:
    """Gemini Vision API с ретраями (2с/4с/8с). content — список Claude-style блоков."""
    import aiohttp as _ah
    # Конвертируем Claude-style content в Gemini parts
    parts = []
    for block in (content if isinstance(content, list) else [{"type": "text", "text": content}]):
        if block.get("type") == "image":
            src = block["source"]
            parts.append({"inline_data": {"mime_type": src["media_type"], "data": src["data"]}})
        else:
            parts.append({"text": block.get("text", "")})
    # Системный промпт — добавляем первым текстовым блоком
    parts = [{"text": system + "\n\n"}] + parts

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.1,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    last_err = None
    _refresh_gemini_keys()
    total_attempts = len(_GEMINI_KEYS) * 2  # 2 попытки на каждый ключ
    for attempt in range(total_attempts):
        try:
            key = _get_gemini_key()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
            async with _ah.ClientSession() as session:
                async with session.post(url, json=payload, timeout=_ah.ClientTimeout(total=60)) as resp:
                    data = await resp.json()
            err = data.get("error", {})
            if err:
                msg = err.get("message", "")
                if "quota" in msg.lower() or err.get("code") in (429, 403):
                    _rotate_gemini_key()
                    last_err = RuntimeError(f"Gemini quota: {msg[:120]}")
                    continue
                raise RuntimeError(f"Gemini: {msg[:120]}")
            resp_parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in resp_parts).strip()
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            logger.warning(f"nutrition: gemini attempt {attempt+1} failed: {e}")
            if attempt < total_attempts - 1:
                await asyncio.sleep(2)
    raise last_err or RuntimeError("All Gemini keys exhausted")


async def calculate_meal(text: str | None = None, photo_bytes: bytes | None = None) -> dict:
    content = []
    if photo_bytes:
        photo_bytes = _downscale(photo_bytes)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(photo_bytes).decode(),
            }
        })
    content.append({
        "type": "text",
        "text": text or "Что на фото? Посчитай калории и БЖУК.",
    })

    raw = await _gemini_call(NUTRITION_SYSTEM_PROMPT, content)
    try:
        return _parse_claude_json(raw)
    except Exception as e:
        logger.warning(f"nutrition: bad JSON, retrying once: {e}; raw={raw[:300]}")
        raw2 = await _gemini_call(NUTRITION_SYSTEM_PROMPT, content)
        return _parse_claude_json(raw2)


async def correct_meal(prev_calc: dict, correction: str) -> dict:
    user_text = (
        f"Предыдущий расчёт:\n{json.dumps(prev_calc, ensure_ascii=False, indent=2)}\n\n"
        f"Правка: {correction}"
    )
    raw = await _gemini_call(CORRECTION_SYSTEM_PROMPT, user_text)
    return _parse_claude_json(raw)


async def recalc_leftover(prev_calc: dict,
                          text: str | None = None,
                          photo_bytes: bytes | None = None) -> dict:
    """Пересчитывает блюдо по описанию/фото остатков (того, что НЕ съели)."""
    intro = (
        f"Изначальный расчёт того, что было на тарелке:\n"
        f"{json.dumps(prev_calc, ensure_ascii=False, indent=2)}\n\n"
    )
    content = []
    if photo_bytes:
        photo_bytes = _downscale(photo_bytes)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(photo_bytes).decode(),
            },
        })
        content.append({
            "type": "text",
            "text": intro + (
                f"На фото — ОСТАТКИ (то, что НЕ съели). "
                f"Описание от пользователя: {text or '—'}\n"
                f"Пересчитай съеденное."
            ),
        })
    else:
        content.append({
            "type": "text",
            "text": intro + f"Описание остатков от пользователя: {text or ''}",
        })
    raw = await _gemini_call(LEFTOVER_SYSTEM_PROMPT, content)
    return _parse_claude_json(raw)


# ==================== Render ====================

def _fmt_num(v, digits=1) -> str:
    try:
        f = float(v)
    except Exception:
        return ""
    if f == int(f):
        return str(int(f))
    return f"{f:.{digits}f}"


def _truncate(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_meal_card_png(calc: dict, settings: dict, day_total: dict, meal_id: int) -> bytes:
    """PNG-карточка блюда с таблицей БЖУК. Заголовок, выделенная колонка Ккал,
    жирная строка ИТОГО, футер с целью и сводкой дня."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    items = calc.get("items") or []
    total = calc.get("total") or {}

    headers = ["Название", "Вес, г", "Ккал", "Б, г", "Ж, г", "У, г", "К, г"]
    rows = []
    for it in items:
        rows.append([
            _truncate(it.get("name", ""), 42),
            _fmt_num(it.get("grams"), 0),
            _fmt_num(it.get("kcal"), 0),
            _fmt_num(it.get("protein")),
            _fmt_num(it.get("fat")),
            _fmt_num(it.get("carbs")),
            _fmt_num(it.get("fiber")),
        ])
    rows.append([
        "ИТОГО",
        _fmt_num(total.get("grams"), 0),
        _fmt_num(total.get("kcal"), 0),
        _fmt_num(total.get("protein")),
        _fmt_num(total.get("fat")),
        _fmt_num(total.get("carbs")),
        _fmt_num(total.get("fiber")),
    ])
    n = len(rows)
    last = n  # row index of ИТОГО (1-indexed: 0=header, 1..=data)

    # Высота под количество строк + заголовок + футер
    row_h_in = 0.42
    fig_h = 1.4 + (n + 1) * row_h_in + 0.6
    fig, ax = plt.subplots(figsize=(10.5, fig_h), dpi=130)
    ax.axis("off")

    name = total.get("name") or (items[0].get("name") if items else "Приём пищи")
    confidence = calc.get("confidence", "")
    conf_suffix = {"medium": "  🟡", "low": "  🟠"}.get(confidence, "")
    fig.suptitle(f"{name}{conf_suffix}", fontsize=18, fontweight="bold", y=0.97, x=0.5)

    col_widths = [0.42, 0.09, 0.10, 0.085, 0.085, 0.085, 0.085]
    table = ax.table(
        cellText=rows,
        colLabels=headers,
        cellLoc="center",
        colWidths=col_widths,
        loc="upper center",
        bbox=[0.02, 0.10, 0.96, 0.82],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(12)

    # шапка
    for j in range(len(headers)):
        c = table[(0, j)]
        c.set_facecolor("#EFEAFE")
        c.set_text_props(weight="bold", color="#3a2a8e")
        c.set_height(0.10)
    # подсветка колонки «Ккал»
    for i in range(1, n + 1):
        table[(i, 2)].set_facecolor("#FFF4C2")
    # ИТОГО — жирная и фоном
    for j in range(len(headers)):
        c = table[(last, j)]
        c.set_text_props(weight="bold")
        c.set_facecolor("#FFE680" if j == 2 else "#F4F1FB")
    # колонка «Название» — по левому краю
    for i in range(1, n + 1):
        table[(i, 0)].set_text_props(ha="left")
        table[(i, 0)].PAD = 0.03  # небольшой отступ слева

    # Футер
    daily_goal = settings.get("daily_kcal", DEFAULT_DAILY_KCAL)
    kcal_today = int(round(day_total.get("kcal", 0)))
    fig.text(
        0.5, 0.045,
        f"Дневная цель: {daily_goal} ккал   •   Сегодня съедено: {kcal_today} ккал",
        ha="center", fontsize=11.5, color="#555",
    )
    fig.text(0.5, 0.012, f"#РАСЧЁТ  №{meal_id}", ha="center", fontsize=9, color="#999")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def build_meal_caption(calc: dict, meal_id: int) -> str:
    """Подпись под фото-карточкой. До 1024 символов в Telegram."""
    notes = (calc.get("notes") or "").strip()
    if not notes:
        return ""
    return f"<i>{_esc(notes[:600])}</i>"


def _bar(value: float, goal: float, width: int = 10) -> str:
    if goal <= 0:
        return "░" * width
    frac = max(0.0, min(1.5, value / goal))
    fill = int(round(frac * width))
    fill = max(0, min(width, fill))
    return "▓" * fill + "░" * (width - fill)


def format_day_summary(meals: list[dict], settings: dict, day: date) -> str:
    months = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    title = f"📊 <b>{day.day} {months[day.month-1]}</b>"

    if not meals:
        return f"{title}\n\nСегодня записей нет. Пришли фото или опиши еду текстом."

    lines = [title, ""]
    sum_kcal = sum(_f(m["kcal"]) for m in meals)
    sum_p = sum(_f(m["protein"]) for m in meals)
    sum_f = sum(_f(m["fat"]) for m in meals)
    sum_c = sum(_f(m["carbs"]) for m in meals)
    sum_fib = sum(_f(m["fiber"]) for m in meals)

    for m in meals:
        grams = _fmt_num(m["grams"], 0)
        kcal = _fmt_num(m["kcal"], 0)
        gpart = f" ({grams} г)" if grams else ""
        lines.append(f"• {_esc(m['meal_name'])}{gpart} — {kcal} ккал")

    daily = settings["daily_kcal"]
    tp, tf, tc = settings["target_p"], settings["target_f"], settings["target_c"]
    lines.append("")
    lines.append(f"<b>ИТОГО: {int(round(sum_kcal))} / {daily} ккал</b>")
    lines.append("<pre>")
    lines.append(f"Б {sum_p:>5.1f} / {tp:>3} г  {_bar(sum_p, tp)}")
    lines.append(f"Ж {sum_f:>5.1f} / {tf:>3} г  {_bar(sum_f, tf)}")
    lines.append(f"У {sum_c:>5.1f} / {tc:>3} г  {_bar(sum_c, tc)}")
    lines.append(f"Клетчатка {sum_fib:.1f} г  (норма 20–40)")
    lines.append("</pre>")
    return "\n".join(lines)


def format_period_summary(days_buckets: list[tuple[date, dict]], settings: dict, label: str) -> str:
    if not days_buckets:
        return f"{label}: записей нет."
    avg_kcal = sum(b["kcal"] for _, b in days_buckets) / len(days_buckets)
    avg_p = sum(b["protein"] for _, b in days_buckets) / len(days_buckets)
    avg_f = sum(b["fat"] for _, b in days_buckets) / len(days_buckets)
    avg_c = sum(b["carbs"] for _, b in days_buckets) / len(days_buckets)
    daily = settings["daily_kcal"]
    return (
        f"<b>{label}</b>\n"
        f"Дней с записями: {len(days_buckets)}\n"
        f"Среднее в день: <b>{int(round(avg_kcal))}</b> / {daily} ккал\n"
        f"Б {avg_p:.1f} / {settings['target_p']} г\n"
        f"Ж {avg_f:.1f} / {settings['target_f']} г\n"
        f"У {avg_c:.1f} / {settings['target_c']} г"
    )


def render_period_chart_png(days: list[tuple[date, float]], goal: float, title: str) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 4), dpi=120)
    if days:
        dates = [d for d, _ in days]
        kcals = [k for _, k in days]
        ax.bar(dates, kcals, color="#9b7ee8", alpha=0.85, width=0.7)
        avg = sum(kcals) / len(kcals)
        ax.axhline(goal, color="#9b7ee8", linestyle="--", alpha=0.7,
                   label=f"Цель: {goal:.0f}")
        ax.set_title(f"{title}  •  среднее {avg:.0f} ккал/день")
        ax.legend(loc="upper right")
    else:
        ax.set_title(f"{title}: данных нет")
    ax.set_ylabel("ккал/день")
    ax.grid(axis="y", alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def render_weight_chart_png(points: list[tuple[date, float]]) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 4), dpi=120)
    if points:
        xs = [d for d, _ in points]
        ys = [w for _, w in points]
        ax.plot(xs, ys, marker="o", color="#1abc9c", linewidth=2)
        ax.set_title(f"Вес: {ys[0]:.1f} → {ys[-1]:.1f} кг (Δ {ys[-1]-ys[0]:+.1f})")
    else:
        ax.set_title("Вес: данных нет")
    ax.set_ylabel("кг")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# ==================== Keyboards ====================

def kb_meal_actions(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Исправить",  callback_data=f"nutr:e:{meal_id}"),
            InlineKeyboardButton(text="🍽 Не доела",   callback_data=f"nutr:l:{meal_id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить",    callback_data=f"nutr:d:{meal_id}"),
        ],
    ])


def kb_confirm_delete(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Точно удалить", callback_data=f"nutr:dy:{meal_id}"),
        InlineKeyboardButton(text="↩ Отмена",        callback_data=f"nutr:dn:{meal_id}"),
    ]])


# ==================== Расчёт + ответ ====================

def _today_totals(user_id: int) -> dict:
    today = _today_msk()
    start = datetime.combine(today, datetime.min.time(), tzinfo=TZ_OFFSET)
    end = start + timedelta(days=1)
    meals = list_meals_between(user_id, start, end)
    out = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "fiber": 0.0}
    for m in meals:
        for k in out:
            out[k] += _f(m.get(k, 0))
    return out


async def _process_calc_and_reply(msg: Message, calc: dict, *,
                                  source: str, raw_input: str = "",
                                  photo_file_id: str = ""):
    uid = msg.from_user.id
    if isinstance(calc, dict) and calc.get("error"):
        err = calc["error"]
        if err == "no_food_detected":
            await msg.answer("На фото не вижу еды. Пришли другое фото или опиши текстом.")
        elif err == "no_food_described":
            await msg.answer("В сообщении нет описания еды. Опиши, что съела (например: «грибной суп 250г»).")
        else:
            await msg.answer(f"Не получилось разобрать: {err}")
        return

    settings = await asyncio.to_thread(get_settings, uid)
    meal_payload = {
        "user_id": uid,
        "items": calc.get("items", []),
        "total": calc.get("total", {}),
        "confidence": calc.get("confidence", ""),
        "notes": calc.get("notes", ""),
        "source": source,
        "raw_input": raw_input,
        "photo_file_id": photo_file_id,
        "tg_chat_id": msg.chat.id,
        "tg_msg_id": 0,  # обновим после отправки
    }

    # вставка в Sheets
    try:
        meal_id = await asyncio.to_thread(insert_meal, meal_payload)
    except Exception as e:
        logger.error(f"nutrition: insert_meal failed: {e}")
        await msg.answer("Не смогла записать в дневник (ошибка таблицы).")
        return

    day_total = await asyncio.to_thread(_today_totals, uid)
    try:
        png = await asyncio.to_thread(render_meal_card_png, calc, settings, day_total, meal_id)
    except Exception as e:
        logger.error(f"nutrition: render_meal_card_png failed: {e}")
        png = None

    caption = build_meal_caption(calc, meal_id)
    if png:
        sent = await msg.answer_photo(
            BufferedInputFile(png, filename=f"meal_{meal_id}.png"),
            caption=caption or None,
            reply_markup=kb_meal_actions(meal_id),
        )
    else:
        # фолбэк: голый текст-резюме без таблицы
        total = calc.get("total") or {}
        sent = await msg.answer(
            f"<b>{_esc(total.get('name', 'Приём пищи'))}</b>\n"
            f"{int(round(_f(total.get('kcal'))))} ккал, "
            f"Б {_fmt_num(total.get('protein'))} / "
            f"Ж {_fmt_num(total.get('fat'))} / "
            f"У {_fmt_num(total.get('carbs'))} г\n"
            f"#РАСЧЁТ №{meal_id}",
            reply_markup=kb_meal_actions(meal_id),
        )

    # допишем tg_msg_id для будущих edit-ов
    try:
        ws, idx, _ = await asyncio.to_thread(_find_meal_row, meal_id)
        if idx:
            await asyncio.to_thread(ws.update_cell, idx,
                                    MEALS_HEADER.index("tg_msg_id") + 1, sent.message_id)
    except Exception as e:
        logger.warning(f"nutrition: failed to save tg_msg_id: {e}")

    total = calc.get("total", {}) or {}
    logger.info(
        f"nutrition: calc uid={uid} src={source} kcal={total.get('kcal',0)} "
        f"conf={calc.get('confidence','')}"
    )


# ==================== Хэндлеры — вход через bot.py ====================

async def handle_text(msg: Message) -> bool:
    """Возвращает True если сообщение обработано модулем.
    Вызывается из bot.py.handle_text перед общим fallback."""
    uid = msg.from_user.id
    if not is_owner(uid):
        return False

    # FSM-состояния
    state = nutr_states.get(uid)
    if state and state.get("step") == "wait_correction":
        await _apply_correction(msg, state["meal_id"])
        return True
    if state and state.get("step") == "wait_leftover":
        await _apply_leftover(msg, state["meal_id"], text=msg.text or "")
        return True
    if state and state.get("step") == "wait_weight":
        if await _try_log_weight(msg):
            return True
        # не число — выходим из state и обрабатываем как обычно
        nutr_states.pop(uid, None)
    if state and state.get("step") == "wait_goal_kcal":
        if await _try_set_goal_kcal(msg):
            return True
        nutr_states.pop(uid, None)

    # Свободный текст-еда
    text = (msg.text or "").strip()
    if not text:
        return False
    await _recognize_text_food(msg, text)
    return True


async def _try_log_weight(msg: Message) -> bool:
    """Пытается распарсить msg.text как вес. Возвращает True, если записано."""
    uid = msg.from_user.id
    raw = (msg.text or "").strip().lower()
    raw = raw.replace("кг", "").replace("kg", "").strip().replace(",", ".")
    try:
        w = float(raw)
    except ValueError:
        return False
    if not (30 <= w <= 300):
        return False
    await asyncio.to_thread(log_weight, uid, w)
    nutr_states.pop(uid, None)
    await msg.answer(f"Записала: <b>{w:.1f}</b> кг ({_today_msk().isoformat()}).\nГрафик: /вес_график")
    return True


async def _try_set_goal_kcal(msg: Message) -> bool:
    """Пытается распарсить msg.text как дневную цель ккал. Возвращает True если записано."""
    uid = msg.from_user.id
    raw = (msg.text or "").strip().lower()
    raw = raw.replace("ккал", "").replace("kcal", "").strip().replace(",", ".")
    try:
        kcal = int(float(raw))
    except ValueError:
        return False
    if not (500 <= kcal <= 6000):
        return False
    await asyncio.to_thread(set_settings, uid, daily_kcal=kcal)
    nutr_states.pop(uid, None)
    await msg.answer(f"Ок, дневная цель: <b>{kcal}</b> ккал.")
    return True


async def handle_photo(msg: Message) -> bool:
    """Фото от владельца → еда. Вызывается из bot.py.handle_photo."""
    uid = msg.from_user.id
    if not is_owner(uid):
        return False
    photo = None
    if msg.photo:
        photo = msg.photo[-1]
    elif msg.document and (msg.document.mime_type or "").lower().startswith("image/"):
        photo = msg.document
    if not photo:
        return False

    # Если в состоянии «не доела» — это фото остатков
    state = nutr_states.get(uid)
    if state and state.get("step") == "wait_leftover":
        w = await msg.answer("Считаю остатки…")
        try:
            f = await bot_instance.get_file(photo.file_id)
            data = (await bot_instance.download_file(f.file_path)).read()
        except Exception as e:
            logger.error(f"nutrition: download leftover photo: {e}")
            try: await w.edit_text("Не удалось скачать фото.")
            except Exception: pass
            return True
        try: await w.delete()
        except Exception: pass
        await _apply_leftover(
            msg, state["meal_id"],
            text=(msg.caption or "").strip() or None,
            photo_bytes=data,
        )
        return True

    # Если в любом другом FSM-состоянии (правка/вес/цель) пришло фото —
    # явно уведомляем юзера, что выходим из режима ожидания.
    if state and state.get("step") in {"wait_correction", "wait_weight", "wait_goal_kcal"}:
        nutr_states.pop(uid, None)
        prev = state["step"]
        hint_map = {
            "wait_correction": "Отменила режим правки",
            "wait_weight":     "Отменила ожидание веса",
            "wait_goal_kcal":  "Отменила ожидание цели",
        }
        try:
            await msg.answer(f"{hint_map.get(prev, 'Сбросила режим')}, обрабатываю как новое блюдо.")
        except Exception:
            pass

    w = await msg.answer("Считаю калории…")
    try:
        f = await bot_instance.get_file(photo.file_id)
        data = (await bot_instance.download_file(f.file_path)).read()
    except Exception as e:
        logger.error(f"nutrition: download photo: {e}")
        try: await w.edit_text("Не удалось скачать фото.")
        except Exception: pass
        return True

    caption = (msg.caption or "").strip()
    try:
        calc = await calculate_meal(text=caption or None, photo_bytes=data)
    except Exception as e:
        logger.error(f"nutrition: calc photo failed: {e}")
        try: await w.edit_text("Не получилось распознать. Попробуй ещё раз или опиши текстом.")
        except Exception: pass
        return True
    try: await w.delete()
    except Exception: pass
    await _process_calc_and_reply(
        msg, calc, source="photo",
        raw_input=caption, photo_file_id=photo.file_id,
    )
    return True


async def _recognize_text_food(msg: Message, text: str):
    try:
        await bot_instance.send_chat_action(msg.chat.id, "typing")
    except Exception:
        pass
    try:
        calc = await calculate_meal(text=text)
    except Exception as e:
        logger.error(f"nutrition: calc text failed: {e}")
        err_str = str(e)
        if "quota" in err_str.lower() or "429" in err_str or "403" in err_str:
            await msg.answer("⚠️ Лимит Gemini API исчерпан. Попробуй позже или добавь второй ключ GEMINI_API_KEY_2.")
        else:
            await msg.answer(f"Не получилось распознать. Попробуй переформулировать или прислать фото.\n<i>{err_str[:120]}</i>")
        return
    await _process_calc_and_reply(msg, calc, source="text", raw_input=text)


async def _apply_correction(msg: Message, meal_id: int):
    uid = msg.from_user.id
    correction = (msg.text or "").strip()
    if not correction:
        await msg.answer("Напиши текст правки или /отмена_еда.")
        return
    meal = await asyncio.to_thread(get_meal, meal_id)
    if not meal or meal["user_id"] != uid or meal["deleted_at"]:
        nutr_states.pop(uid, None)
        await msg.answer("Запись не найдена.")
        return

    try:
        prev_calc = json.loads(meal["items_json"]) if meal["items_json"] else {}
    except Exception:
        prev_calc = {}

    try:
        new_calc = await correct_meal(prev_calc, correction)
    except Exception as e:
        logger.error(f"nutrition: correct failed: {e}")
        await msg.answer("Не получилось пересчитать. Попробуй переформулировать.")
        return

    try:
        await asyncio.to_thread(update_meal_calc, meal_id, new_calc)
    except Exception as e:
        logger.error(f"nutrition: update_meal_calc failed: {e}")
        await msg.answer("Не смогла записать в таблицу.")
        return

    settings = await asyncio.to_thread(get_settings, uid)
    day_total = await asyncio.to_thread(_today_totals, uid)
    try:
        png = await asyncio.to_thread(render_meal_card_png, new_calc, settings, day_total, meal_id)
    except Exception as e:
        logger.error(f"nutrition: render after correct failed: {e}")
        png = None
    caption = build_meal_caption(new_calc, meal_id)

    # Заменяем медиа в исходном сообщении (там фото-карточка)
    posted_new = False
    if png and meal["tg_chat_id"] and meal["tg_msg_id"]:
        try:
            from aiogram.types import InputMediaPhoto
            await bot_instance.edit_message_media(
                chat_id=meal["tg_chat_id"],
                message_id=meal["tg_msg_id"],
                media=InputMediaPhoto(
                    media=BufferedInputFile(png, filename=f"meal_{meal_id}.png"),
                    caption=caption or None,
                    parse_mode="HTML",
                ),
                reply_markup=kb_meal_actions(meal_id),
            )
        except Exception as e:
            logger.warning(f"nutrition: edit_message_media failed, posting new: {e}")
            posted_new = True
    else:
        posted_new = True

    if posted_new:
        if png:
            await msg.answer_photo(
                BufferedInputFile(png, filename=f"meal_{meal_id}.png"),
                caption=caption or None, reply_markup=kb_meal_actions(meal_id),
            )
    # Подтверждение всегда — иначе юзер не видит реакции (edit карточки
    # происходит вверху чата, может быть вне зоны видимости).
    await msg.answer("✅ Обновила запись.")
    nutr_states.pop(uid, None)


async def _apply_leftover(msg: Message, meal_id: int,
                          text: str | None = None,
                          photo_bytes: bytes | None = None):
    uid = msg.from_user.id
    if not text and not photo_bytes:
        await msg.answer("Сфоткай остатки или напиши, сколько осталось (например: «осталась половина», «треть не доела», «осталось 100г»).\nОтмена — /отмена_еда")
        return
    meal = await asyncio.to_thread(get_meal, meal_id)
    if not meal or meal["user_id"] != uid or meal["deleted_at"]:
        nutr_states.pop(uid, None)
        await msg.answer("Запись не найдена.")
        return

    try:
        prev_calc = json.loads(meal["items_json"]) if meal["items_json"] else {}
    except Exception:
        prev_calc = {}

    try:
        await bot_instance.send_chat_action(msg.chat.id, "typing")
    except Exception:
        pass

    try:
        new_calc = await recalc_leftover(prev_calc, text=text, photo_bytes=photo_bytes)
    except Exception as e:
        logger.error(f"nutrition: leftover recalc failed: {e}")
        await msg.answer("Не получилось пересчитать. Попробуй ещё раз или опиши словами.")
        return

    if isinstance(new_calc, dict) and new_calc.get("error"):
        await msg.answer(
            (new_calc.get("notes") or
             "Не поняла. Скажи: половина / треть / осталось N грамм, или пришли фото остатков.")
        )
        return  # state не сбрасываем — даём шанс уточнить

    try:
        await asyncio.to_thread(update_meal_calc, meal_id, new_calc)
    except Exception as e:
        logger.error(f"nutrition: update_meal_calc (leftover) failed: {e}")
        await msg.answer("Не смогла записать в таблицу.")
        return

    settings = await asyncio.to_thread(get_settings, uid)
    day_total = await asyncio.to_thread(_today_totals, uid)
    try:
        png = await asyncio.to_thread(render_meal_card_png, new_calc, settings, day_total, meal_id)
    except Exception as e:
        logger.error(f"nutrition: render after leftover failed: {e}")
        png = None
    caption = build_meal_caption(new_calc, meal_id)

    posted_new = False
    if png and meal["tg_chat_id"] and meal["tg_msg_id"]:
        try:
            from aiogram.types import InputMediaPhoto
            await bot_instance.edit_message_media(
                chat_id=meal["tg_chat_id"],
                message_id=meal["tg_msg_id"],
                media=InputMediaPhoto(
                    media=BufferedInputFile(png, filename=f"meal_{meal_id}.png"),
                    caption=caption or None,
                    parse_mode="HTML",
                ),
                reply_markup=kb_meal_actions(meal_id),
            )
        except Exception as e:
            logger.warning(f"nutrition: edit_message_media (leftover) failed: {e}")
            posted_new = True
    else:
        posted_new = True

    if posted_new and png:
        await msg.answer_photo(
            BufferedInputFile(png, filename=f"meal_{meal_id}.png"),
            caption=caption or None, reply_markup=kb_meal_actions(meal_id),
        )
    # Подтверждение всегда (карточка отредактирована наверху — может
    # оказаться вне зоны видимости пользователя).
    await msg.answer("✅ Пересчитала с учётом остатков.")
    nutr_states.pop(uid, None)


# ==================== Callbacks ====================

@nutrition_router.callback_query(F.data.startswith("nutr:"))
async def on_nutr_callback(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_owner(uid):
        await cb.answer("Это личный модуль.", show_alert=False)
        return
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    action = parts[1]
    try:
        meal_id = int(parts[2])
    except ValueError:
        await cb.answer()
        return

    meal = await asyncio.to_thread(get_meal, meal_id)
    if not meal or meal["user_id"] != uid:
        await cb.answer("Запись не найдена.", show_alert=True)
        return

    if action == "e":
        nutr_states[uid] = {"step": "wait_correction", "meal_id": meal_id}
        await cb.answer()
        await cb.message.answer(
            "Что поправить? Например: «не 150 г рыбы, а 200» или «добавь сметану 30 г».\n"
            "Отмена — /отмена_еда"
        )
        return

    if action == "l":
        nutr_states[uid] = {"step": "wait_leftover", "meal_id": meal_id}
        await cb.answer()
        await cb.message.answer(
            "Сфоткай остатки (то, что НЕ съела) или напиши словами:\n"
            "«осталась половина», «треть не доела», «осталось 100 г», «съела чуть-чуть».\n"
            "Отмена — /отмена_еда"
        )
        return

    if action == "d":
        await cb.answer()
        try:
            await cb.message.edit_reply_markup(reply_markup=kb_confirm_delete(meal_id))
        except Exception:
            await cb.message.answer("Точно удалить?", reply_markup=kb_confirm_delete(meal_id))
        return

    if action == "dy":
        ok = await asyncio.to_thread(soft_delete_meal, meal_id)
        if ok:
            crossed = f"<s>{_esc(meal['meal_name'])}</s>\n<i>Запись удалена.</i>"
            try:
                # Photo-сообщение → редактируем caption и убираем кнопки
                await cb.message.edit_caption(caption=crossed, reply_markup=None)
            except Exception:
                # Текстовое сообщение → edit_text. reply_markup=None всегда,
                # чтобы не оставить «зомби»-кнопки на удалённой записи.
                try:
                    await cb.message.edit_text(crossed, reply_markup=None)
                except Exception:
                    # Совсем не получилось — хотя бы убрать клавиатуру отдельно.
                    try:
                        await cb.message.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                    await cb.message.answer("Запись удалена.")
            await cb.answer("Удалено")
        else:
            await cb.answer("Не нашла запись.", show_alert=True)
        return

    if action == "dn":
        try:
            await cb.message.edit_reply_markup(reply_markup=kb_meal_actions(meal_id))
        except Exception:
            pass
        await cb.answer("Отменено")
        return

    await cb.answer()


# ==================== Команды ====================

@nutrition_router.message(Command("отмена_еда"))
async def cmd_cancel_nutr(msg: Message):
    if not is_owner(msg.from_user.id):
        return
    nutr_states.pop(msg.from_user.id, None)
    await msg.answer("Ок, отменила.")


@nutrition_router.message(Command(commands=["день", "today"]))
async def cmd_day(msg: Message):
    uid = msg.from_user.id
    if not is_owner(uid):
        return
    today = _today_msk()
    start = datetime.combine(today, datetime.min.time(), tzinfo=TZ_OFFSET)
    end = start + timedelta(days=1)
    meals = await asyncio.to_thread(list_meals_between, uid, start, end)
    settings = await asyncio.to_thread(get_settings, uid)
    await msg.answer(format_day_summary(meals, settings, today))


def _bucket_by_day(meals: list[dict]) -> list[tuple[date, dict]]:
    buckets: dict[date, dict] = {}
    for m in meals:
        dt = _parse_iso(m["eaten_at"])
        if not dt:
            continue
        d = _to_msk(dt).date()
        b = buckets.setdefault(
            d, {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "fiber": 0.0}
        )
        for k in b:
            b[k] += _f(m.get(k, 0))
    return sorted(buckets.items())


async def _period_response(msg: Message, days: int, label: str):
    uid = msg.from_user.id
    today = _today_msk()
    start_day = today - timedelta(days=days - 1)
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=TZ_OFFSET)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=TZ_OFFSET)
    meals = await asyncio.to_thread(list_meals_between, uid, start, end)
    settings = await asyncio.to_thread(get_settings, uid)
    buckets = _bucket_by_day(meals)
    text = format_period_summary(buckets, settings, label)
    chart = await asyncio.to_thread(
        render_period_chart_png,
        [(d, b["kcal"]) for d, b in buckets],
        float(settings["daily_kcal"]),
        label,
    )
    photo = BufferedInputFile(chart, filename=f"{label}.png")
    await msg.answer_photo(photo, caption=text)


@nutrition_router.message(Command(commands=["неделя", "week"]))
async def cmd_week(msg: Message):
    if not is_owner(msg.from_user.id):
        return
    await _period_response(msg, 7, "За 7 дней")


@nutrition_router.message(Command(commands=["месяц", "month"]))
async def cmd_month(msg: Message):
    if not is_owner(msg.from_user.id):
        return
    await _period_response(msg, 30, "За 30 дней")


@nutrition_router.message(Command(commands=["цель", "goal"]))
async def cmd_goal(msg: Message):
    uid = msg.from_user.id
    if not is_owner(uid):
        return
    args = (msg.text or "").split(maxsplit=1)
    settings = await asyncio.to_thread(get_settings, uid)
    if len(args) < 2:
        nutr_states[uid] = {"step": "wait_goal_kcal"}
        await msg.answer(
            f"Текущая цель: {settings['daily_kcal']} ккал/день.\n"
            f"Б {settings['target_p']} / Ж {settings['target_f']} / У {settings['target_c']} г.\n\n"
            f"<b>Пришли число</b> — будет новая дневная цель ккал (например, <code>1500</code>).\n"
            f"Цели по БЖУ: <code>/цель_бжу 70 50 180</code>\n"
            f"Отмена — /отмена_еда"
        )
        return
    try:
        kcal = int(args[1].strip())
        if kcal < 500 or kcal > 6000:
            raise ValueError("out of range")
    except Exception:
        await msg.answer("Не понимаю число. Пример: <code>/цель 1380</code>")
        return
    await asyncio.to_thread(set_settings, uid, daily_kcal=kcal)
    await msg.answer(f"Ок, дневная цель: {kcal} ккал.")


@nutrition_router.message(Command(commands=["цель_бжу", "macros"]))
async def cmd_macros(msg: Message):
    uid = msg.from_user.id
    if not is_owner(uid):
        return
    parts = (msg.text or "").split()
    if len(parts) < 4:
        await msg.answer("Формат: <code>/цель_бжу 69 46 172</code>  (Б Ж У в граммах)")
        return
    try:
        p, f, c = int(parts[1]), int(parts[2]), int(parts[3])
    except Exception:
        await msg.answer("Не понимаю числа. Пример: <code>/цель_бжу 69 46 172</code>")
        return
    await asyncio.to_thread(set_settings, uid, target_p=p, target_f=f, target_c=c)
    await msg.answer(f"Ок: Б {p} / Ж {f} / У {c} г.")


@nutrition_router.message(Command(commands=["вес", "weight"]))
async def cmd_weight(msg: Message):
    uid = msg.from_user.id
    if not is_owner(uid):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        weights = await asyncio.to_thread(list_weights, uid, _today_msk() - timedelta(days=30))
        nutr_states[uid] = {"step": "wait_weight"}
        if not weights:
            await msg.answer(
                "Записей веса нет.\n\n"
                "<b>Пришли число</b> — например, <code>60.5</code>.\n"
                "Отмена — /отмена_еда"
            )
            return
        last = weights[-1]
        await msg.answer(
            f"Последний вес: <b>{last[1]:.1f}</b> кг ({last[0].isoformat()}).\n\n"
            f"<b>Пришли число</b> — например, <code>60.5</code> — запишу за сегодня.\n"
            f"График: /вес_график\nОтмена — /отмена_еда"
        )
        return
    try:
        w = float(parts[1].replace(",", "."))
        if w < 30 or w > 300:
            raise ValueError("out of range")
    except Exception:
        await msg.answer("Не понимаю. Пример: <code>/вес 60.5</code>")
        return
    await asyncio.to_thread(log_weight, uid, w)
    await msg.answer(f"Записала: {w:.1f} кг ({_today_msk().isoformat()}).")


async def show_weight_status(msg: Message):
    """Для reply-кнопки «⚖️ Вес» — последний вес + ждём ввод нового числа."""
    uid = msg.from_user.id
    if not is_owner(uid):
        return
    weights = await asyncio.to_thread(list_weights, uid, _today_msk() - timedelta(days=30))
    nutr_states[uid] = {"step": "wait_weight"}
    if not weights:
        await msg.answer(
            "Записей веса нет.\n\n"
            "<b>Пришли число</b> — например, <code>60.5</code> или <code>60,5</code>.\n"
            "Отмена — /отмена_еда"
        )
        return
    last = weights[-1]
    delta_text = ""
    if len(weights) >= 2:
        delta = last[1] - weights[0][1]
        delta_text = f" ({delta:+.1f} кг за период)"
    await msg.answer(
        f"Последний вес: <b>{last[1]:.1f}</b> кг ({last[0].isoformat()}){delta_text}\n\n"
        f"<b>Пришли число</b> — например, <code>60.5</code> — запишу за сегодня.\n"
        f"График: /вес_график\nОтмена — /отмена_еда"
    )


async def show_goal_status(msg: Message):
    """Для reply-кнопки «🎯 Цель» — текущая цель + ждём ввод нового числа ккал."""
    uid = msg.from_user.id
    if not is_owner(uid):
        return
    settings = await asyncio.to_thread(get_settings, uid)
    nutr_states[uid] = {"step": "wait_goal_kcal"}
    await msg.answer(
        f"<b>Текущая цель</b>\n"
        f"Калории: {settings['daily_kcal']} ккал/день\n"
        f"Б {settings['target_p']} / Ж {settings['target_f']} / У {settings['target_c']} г\n\n"
        f"<b>Пришли число</b> — будет новая дневная цель ккал (например, <code>1500</code>).\n"
        f"Цели по БЖУ: <code>/цель_бжу 70 50 180</code>\n"
        f"Отмена — /отмена_еда"
    )


@nutrition_router.message(Command(commands=["вес_график", "weight_chart"]))
async def cmd_weight_chart(msg: Message):
    uid = msg.from_user.id
    if not is_owner(uid):
        return
    points = await asyncio.to_thread(list_weights, uid, _today_msk() - timedelta(days=30))
    if not points:
        await msg.answer("Записей веса нет. Пример: <code>/вес 60.5</code>")
        return
    png = await asyncio.to_thread(render_weight_chart_png, points)
    await msg.answer_photo(BufferedInputFile(png, filename="weight.png"),
                            caption="Вес за 30 дней")
