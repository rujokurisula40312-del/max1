"""Общие утилиты для finance-bot (Max Messenger)."""
import json, re, logging, time
import gspread

logger = logging.getLogger(__name__)


# ==================== Claude JSON ====================

def parse_claude_json(raw: str):
    """Убирает markdown-обёртку (```json...```) и парсит JSON.
    Возвращает dict или list. Бросает json.JSONDecodeError при ошибке."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    # Для случаев когда Claude добавляет текст вокруг JSON
    text = text.strip()
    jm = re.search(r'[\[{][\s\S]*[\]}]', text)
    if jm:
        text = jm.group(0)
    return json.loads(text)


# ==================== Claude content ====================

def build_claude_content(docs: list) -> list:
    """Формирует content-массив для Claude API из списка документов.
    Каждый doc — dict с type (text/image/document), media_type, data/text."""
    content = []
    for doc in docs:
        if doc["type"] == "text":
            content.append({"type": "text", "text": doc["text"]})
        elif doc["type"] == "document":
            content.append({"type": "document", "source": {
                "type": "base64", "media_type": doc["media_type"], "data": doc["data"]}})
        else:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": doc["media_type"], "data": doc["data"]}})
    return content


# ==================== Text splitting ====================

def split_long_text(text: str, max_len: int = 4000) -> list[str]:
    """Разбивает длинный текст на части не длиннее max_len,
    стараясь резать по переносу строки."""
    if len(text) <= max_len:
        return [text]
    parts = []
    while len(text) > max_len:
        cut = text[:max_len].rfind("\n")
        if cut < max_len // 2:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:]
    if text:
        parts.append(text)
    return parts


# ==================== Duplicate detection ====================

def find_duplicate_row(all_rows: list, name: str, contacts: str, start_row: int = 1) -> int | None:
    """Ищет дубль по телефону (7+ цифр) или по имени (>3-4 букв).
    Возвращает номер строки (1-indexed для gspread) или None.
    all_rows — результат sheet.get_all_values(), name_col=1, contacts_col=2."""
    name_lower = (name or "").lower().strip()
    new_phones = set(re.findall(r'\d{7,}', contacts or ""))

    for i, row in enumerate(all_rows):
        if i < start_row:
            continue
        ex_name = (row[1] if len(row) > 1 else "").lower().strip()
        ex_contacts = row[2] if len(row) > 2 else ""
        ex_phones = set(re.findall(r'\d{7,}', ex_contacts))

        # Совпадение по телефону
        if new_phones and ex_phones and new_phones & ex_phones:
            return i + 1
        # Совпадение по имени
        if ex_name and name_lower and len(name_lower) > 3:
            if ex_name == name_lower or name_lower in ex_name or ex_name in name_lower:
                return i + 1
    return None


# ==================== Safe phone ====================

def safe_phone(val) -> str:
    """Предотвращает интерпретацию телефонов (+55...) как формул в Google Sheets.
    Добавляет апостроф перед значениями начинающимися с + = - @"""
    s = str(val) if val else ""
    if s and s[0] in ('+', '=', '-', '@'):
        return "'" + s
    return s


# ==================== Format number ====================

def format_num(v) -> str:
    """Форматирует число для Google Sheets: запятая вместо точки, без .0 для целых."""
    if v is None or v == "":
        return ""
    try:
        f = float(str(v).replace(",", ".").replace(" ", ""))
        if f == int(f):
            return str(int(f))
        return str(f).replace(".", ",")
    except (ValueError, TypeError):
        return str(v)


# ==================== Markdown → HTML ====================

def md_to_html(text: str) -> str:
    """Конвертирует Markdown в HTML (поддерживается Max Messenger)."""
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'^#{1,4}\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<![<\/])\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    allowed_tags = {'b', 'i', 'code', 'a', 'pre', 'u', 's', 'em', 'strong'}

    def clean_tag(m):
        tag = m.group(1).lower().strip().split()[0].strip('/')
        return m.group(0) if tag in allowed_tags else ''

    text = re.sub(r'<(/?\w[^>]*)>', clean_tag, text)
    return text.strip()


# ==================== Fuzzy match ====================

def normalize_text(t: str) -> str:
    return re.sub(r'[\s\-_]+', ' ', t.lower().strip())


def fuzzy_match(query: str, text: str) -> bool:
    """Нечёткий поиск: подстрока, корень слова, ё/е, перестановки."""
    nq = normalize_text(query).replace("ё", "е")
    nt = normalize_text(text).replace("ё", "е")
    if not nq or not nt:
        return False
    # Для коротких однословных запросов (≤5 символов) требуем точную границу
    # слова, иначе «котор» матчит «которая», «эт» матчит «этот» и т.п. —
    # короткие стемы слишком часто встречаются в обычной русской речи.
    if " " not in nq and len(nq) <= 5:
        return bool(re.search(rf'\b{re.escape(nq)}\b', nt))
    if nq in nt:
        return True
    nq_ns = nq.replace(" ", "")
    nt_ns = nt.replace(" ", "")
    if nq_ns in nt_ns:
        return True
    words = nq.split()
    if len(words) > 1 and all(w in nt for w in words):
        return True
    for qw in words:
        if len(qw) >= 4:
            stem = qw[:max(4, len(qw) - 3)]
            if any(tw.startswith(stem) for tw in nt.split()):
                return True
    for tw in nt.split():
        if len(tw) >= 4 and len(nq_ns) >= 4:
            common = min(len(tw), len(nq_ns))
            match_len = 0
            for ci in range(common):
                if tw[ci] == nq_ns[ci]:
                    match_len += 1
                else:
                    break
            if match_len >= 4:
                return True
    # Анаграмма — только для длинных слов (7+ символов), чтобы «баку» не матчилось с «куба»
    if len(nq_ns) >= 7:
        for word in nt.split():
            w = word.replace(" ", "")
            if len(w) >= 7 and abs(len(w) - len(nq_ns)) <= 1:
                if sorted(nq_ns) == sorted(w):
                    return True
    return False


# ==================== FakeMsg ====================

class FakeMsg:
    """Обёртка для передачи распознанного текста в обработчик как Message (Max API).

    Оборачивает объект maxapi.types.message.Message, подменяя text.
    Используется при распознавании голосовых сообщений — передаёт
    распознанный текст в тот же хендлер, что и текстовое сообщение.
    """
    def __init__(self, original_message, new_text):
        # original_message — объект maxapi Message (event.message)
        self._msg = original_message
        self.text = new_text
        self.caption = None
        self.photo = None
        self.document = None
        self.voice = None

    @property
    def sender(self):
        return self._msg.sender

    @property
    def recipient(self):
        return self._msg.recipient

    async def answer(self, text, **kwargs):
        return await self._msg.answer(text, **kwargs)

    async def answer_photo(self, *args, **kwargs):
        fn = getattr(self._msg, "answer_photo", None)
        if fn:
            return await fn(*args, **kwargs)

    @property
    def body(self):
        return self._msg.body


# ==================== Google Sheets retry ====================

def sheets_retry(func, *args, retries=3, **kwargs):
    """Вызывает func с retry при 429 (quota exceeded) от Google Sheets API."""
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if e.response.status_code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
