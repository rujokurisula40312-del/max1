"""
Модуль календаря v2 — полный функционал.
Контекст, редактирование, напоминания, чек-лист, категории, конфликты.
"""
import json, logging, re, asyncio, hashlib
from datetime import datetime, timedelta, timezone
from maxapi import Router, F
from maxapi.types import MessageCallback, CallbackButton, LinkButton
from maxapi.types.attachments import AttachmentButton, ButtonsPayload
from maxapi.enums import AttachmentType
import anthropic


def _make_kb(rows: list[list]) -> AttachmentButton:
    """Собирает AttachmentButton из рядов CallbackButton / LinkButton."""
    return AttachmentButton(
        type=AttachmentType.INLINE_KEYBOARD,
        payload=ButtonsPayload(buttons=rows)
    )

# Карта короткий_токен → полный_event_id. Google Calendar ID для повторяющихся
# событий бывает 50+ символов, и с префиксом «cal_done_» вылезает за лимит
# Telegram callback_data в 64 байта (BUTTON_DATA_INVALID).
_eid_map: dict[str, str] = {}

def _short_eid(eid: str) -> str:
    """Короткий токен для eid; гарантирует, что callback_data влезет в 64 байта."""
    if not eid:
        return ""
    h = hashlib.sha1(eid.encode("utf-8")).hexdigest()[:12]
    _eid_map[h] = eid
    return h

logger = logging.getLogger(__name__)
cal_service = None; claude = None; bot_instance = None
CALENDAR_WORK = ""; CALENDAR_FAMILY = ""; CALENDAR_PERSONAL = ""
TIMEZONE = "Europe/Moscow"; ALLOWED_USERS = []
TZ_OFFSET = timezone(timedelta(hours=3))
cal_router = Router()
cal_states: dict[int, dict] = {}
last_event: dict[int, dict] = {}
reminder_tasks: dict[str, asyncio.Task] = {}

WEEKDAYS_RU = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]

CALENDAR_PROMPT = """Ты — ассистент-планировщик. Сегодня: {today} ({weekday}), Москва UTC+3.
Верни ТОЛЬКО JSON.
- Если задача ОДНА — верни ОБЪЕКТ:
{{"action":"create|list|move|delete|edit|done","title":"название","date":"YYYY-MM-DD","time":"HH:MM или null","end_time":"HH:MM или null","duration_minutes":null,"calendar":"work|family|personal","category":"личное|делегировать|отследить","reminder_minutes":15,"description":"заметки","list_date":"YYYY-MM-DD","list_days":1,"search_text":"для поиска","move_date":"YYYY-MM-DD","move_time":"HH:MM","edit_field":"reminder|time|title","edit_value":"значение","refers_to_last":false}}
- Если в сообщении НЕСКОЛЬКО задач (список, каждая с новой строки, или через «и», или с разными датами) — верни МАССИВ ОБЪЕКТОВ такого же формата, по одному на каждую задачу. action="create" для каждой.

ПРАВИЛА ДАТ (строго):
- Сегодня = {today} ({weekday})
- Завтра = {tomorrow}
- "в понедельник" / "в следующий понедельник" = ближайший понедельник ПОСЛЕ сегодня = {next_mon}
- "во вторник" = {next_tue}, "в среду" = {next_wed}, "в четверг" = {next_thu}, "в пятницу" = {next_fri}
- "через неделю" = {next_week}
- Если день недели уже прошёл на этой неделе — берём следующую неделю
- Без даты и без времени = сегодня, time=null
- Дата вида «1 мая», «2 мая» без года = ближайший такой день в будущем (текущий или следующий год).

🔍 ЗАПРОСЫ-СПРАВКИ (action="list"):
- "что у меня сегодня / на сегодня / сегодня" → list_date=сегодня, list_days=1
- "что завтра / на завтра" → list_date=завтра, list_days=1
- "что у меня 15 мая / события 15 мая / план на 15 мая" → list_date=YYYY-05-15, list_days=1
- "что у меня в среду / в субботу" → list_date=ближайшая такая, list_days=1
- "что на этой неделе / на неделю / план на неделю" → list_date=сегодня, list_days=7
- "что 27-28 мая" / "что у меня 27 и 28 мая" / "27-28 мая" — это ДИАПАЗОН:
  list_date=YYYY-05-27, list_days=2
- "что с 1 по 5 июня" / "1-5 июня" → list_date=YYYY-06-01, list_days=5
- "что на следующей неделе" → list_date=ближайший понедельник, list_days=7
- "что в выходные" → list_date=ближайшая суббота, list_days=2

📅 ВЫЧИСЛЕНИЕ list_days ДЛЯ ДИАПАЗОНА «X-Y число месяца»:
list_days = (Y - X + 1). Примеры:
- «27-28 мая» → list_days = 28-27+1 = 2
- «5-10 июня» → list_days = 10-5+1 = 6
- «1-31 мая» → list_days = 31

ОСТАЛЬНЫЕ ПРАВИЛА:
- Без указания времени → time=null (бот спросит сам, но ТОЛЬКО если задача одна; в массиве оставляй time=null без переспроса)
- "сделай напоминание за 30 мин" → edit, edit_field=reminder, edit_value=30, refers_to_last=true
- "перенеси на час позже" → move, refers_to_last=true
- "что завтра" → list. "сделано" → done
- work=рабочее, family=семья/дети, personal=личное
- reminder_minutes: по умолчанию 15
- ССЫЛКИ: если в тексте есть URL (https://...) — ОБЯЗАТЕЛЬНО сохрани в description. Zoom, Google Meet, сайт отеля — всё в description.
- description: заметки + ссылки + номера рейсов + всё полезное{context}
ТОЛЬКО JSON."""

def now_msk(): return datetime.now(TZ_OFFSET)
def fmt_date(d):
    wd=["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]; return f"{wd[d.weekday()]} {d.strftime('%d.%m.%Y')}"

def fmt_event(ev):
    s=ev.get("start",{}); title=ev.get("summary","?"); desc=ev.get("description","") or ""
    t = datetime.fromisoformat(s["dateTime"]).strftime("%H:%M") if "dateTime" in s else "Весь день"
    if "dateTime" in ev.get("end",{}): t += f"–{datetime.fromisoformat(ev['end']['dateTime']).strftime('%H:%M')}"
    cat=""
    if "[отследить]" in desc.lower(): cat=" [отследить]"
    elif "[делегировать]" in desc.lower(): cat=" [делегир.]"
    return f"  {t}  {title}{cat}"

_cal_404_warned = set()  # Запоминаем какие календари уже выдали 404 чтобы не спамить

def get_events(start_date, days=1):
    s=datetime.combine(start_date,datetime.min.time()).isoformat()+"+03:00"
    e=datetime.combine(start_date+timedelta(days=days),datetime.min.time()).isoformat()+"+03:00"
    all_ev=[]
    for cid,cn in [(CALENDAR_WORK,"Рабочий"),(CALENDAR_FAMILY,"Семейный"),(CALENDAR_PERSONAL,"Личный")]:
        if not cid: continue
        try:
            r=cal_service.events().list(calendarId=cid,timeMin=s,timeMax=e,singleEvents=True,orderBy="startTime",timeZone=TIMEZONE).execute()
            for ev in r.get("items",[]): ev["_cid"]=cid; all_ev.append(ev)
        except Exception as ex:
            err_str = str(ex)
            # 404 = сервисный аккаунт не имеет доступа к этому календарю
            if "404" in err_str:
                if cid not in _cal_404_warned:
                    _cal_404_warned.add(cid)
                    logger.warning(f"Cal {cn} ({cid}): нет доступа (404). Дайте доступ сервисному аккаунту в настройках Google Calendar.")
            else:
                logger.error(f"Cal {cn}: {ex}")
    all_ev.sort(key=lambda x:x.get("start",{}).get("dateTime",x.get("start",{}).get("date","")))
    return all_ev

def find_event(search, days=30):
    q=search.lower(); evs=get_events(now_msk().date(),days)
    for ev in evs:
        if q in (ev.get("summary","") or "").lower(): return ev
    for ev in evs:
        if any(w in (ev.get("summary","") or "").lower() for w in q.split()): return ev
    return None

def check_conflicts(d,t,dur=60):
    if not t: return []
    try:
        dt=datetime.strptime(f"{d} {t}","%Y-%m-%d %H:%M"); end=dt+timedelta(minutes=dur or 60)
        return [ev for ev in get_events(dt.date(),1) if "dateTime" in ev.get("start",{}) and dt<datetime.fromisoformat(ev.get("end",{}).get("dateTime",ev["start"]["dateTime"])).replace(tzinfo=None) and end>datetime.fromisoformat(ev["start"]["dateTime"]).replace(tzinfo=None)]
    except: return []


def suggest_free_slots(d, requested_time, dur=60, count=3):
    """Найти до `count` свободных слотов длиной dur (мин) на дату d.
    Окно поиска 08:00–22:00, шаг 30 мин. Сортируем по близости к
    requested_time. Возвращаем список datetime объектов."""
    dur = int(dur or 60)
    try:
        date_obj = datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return []
    # Занятые интервалы дня (только timed events)
    busy = []
    for ev in get_events(date_obj, 1):
        s = ev.get("start", {})
        if "dateTime" not in s:
            continue
        try:
            sdt = datetime.fromisoformat(s["dateTime"]).replace(tzinfo=None)
            edt_str = ev.get("end", {}).get("dateTime") or s["dateTime"]
            edt = datetime.fromisoformat(edt_str).replace(tzinfo=None)
        except Exception:
            continue
        busy.append((sdt, edt))

    # Сетка слотов 08:00..22:00 шагом 30 мин
    base = datetime.combine(date_obj, datetime.min.time())
    candidates = []
    end_window = base + timedelta(hours=22)
    cur = base + timedelta(hours=8)
    while cur + timedelta(minutes=dur) <= end_window:
        slot_start = cur
        slot_end = cur + timedelta(minutes=dur)
        ok = True
        for bs, be in busy:
            if slot_start < be and slot_end > bs:
                ok = False
                break
        if ok:
            candidates.append(slot_start)
        cur += timedelta(minutes=30)

    if requested_time:
        try:
            req_dt = datetime.strptime(f"{d} {requested_time}", "%Y-%m-%d %H:%M")
            candidates.sort(key=lambda s: abs((s - req_dt).total_seconds()))
        except Exception:
            pass
    return candidates[:count]


def _build_conflict_kb(suggestions: list, with_force: bool = True) -> AttachmentButton:
    """Строит клавиатуру с альтернативными временами + Создать/Отмена."""
    rows = []
    if suggestions:
        rows.append([
            CallbackButton(text=f"⏰ {s.strftime('%H:%M')}", payload=f"cal_alt_{s.strftime('%H%M')}")
            for s in suggestions[:3]
        ])
    bottom = []
    if with_force:
        bottom.append(CallbackButton(text="✅ Всё равно создать", payload="cal_force"))
    bottom.append(CallbackButton(text="❌ Отмена", payload="cal_cancel"))
    rows.append(bottom)
    return _make_kb(rows)

def create_event(data):
    cal_map={"work":CALENDAR_WORK,"family":CALENDAR_FAMILY,"personal":CALENDAR_PERSONAL}
    cid=cal_map.get(data.get("calendar","personal"),CALENDAR_PERSONAL) or CALENDAR_PERSONAL
    ev={"summary":data.get("title","Событие")}
    d=data.get("date",now_msk().strftime("%Y-%m-%d")); t=data.get("time")
    if t:
        ev["start"]={"dateTime":f"{d}T{t}:00+03:00","timeZone":TIMEZONE}
        dur=data.get("duration_minutes") or 60; et=data.get("end_time")
        if et: ev["end"]={"dateTime":f"{d}T{et}:00+03:00","timeZone":TIMEZONE}
        else:
            end=datetime.strptime(f"{d} {t}","%Y-%m-%d %H:%M")+timedelta(minutes=dur)
            ev["end"]={"dateTime":end.strftime("%Y-%m-%dT%H:%M:00+03:00"),"timeZone":TIMEZONE}
    else: ev["start"]={"date":d}; ev["end"]={"date":d}
    dp=[]
    if data.get("description"): dp.append(data["description"])
    if data.get("category"): dp.append(f"[{data['category']}]")
    if dp: ev["description"]="\n".join(dp)
    rm=data.get("reminder_minutes",15)
    ev["reminders"]={"useDefault":False,"overrides":[{"method":"popup","minutes":rm}]}
    logger.info(f"Creating event: {ev.get('summary','')} on {d} {t or 'all-day'} in calendar {cid[:30]}")
    try:
        result=cal_service.events().insert(calendarId=cid,body=ev).execute(); result["_cid"]=cid
        logger.info(f"Event created: {result.get('id','')}")
        return result
    except Exception as e:
        logger.error(f"CREATE EVENT FAILED: {e}\nCalendar: {cid}\nEvent: {ev}")
        raise

async def schedule_reminder(uid,ev_data,mins):
    try:
        s=ev_data.get("start",{})
        if "dateTime" not in s: return
        evt=datetime.fromisoformat(s["dateTime"]); rt=evt-timedelta(minutes=mins)
        delay=(rt-datetime.now(evt.tzinfo)).total_seconds()
        if delay<=0: return
        title=ev_data.get("summary",""); ts=evt.strftime("%H:%M")
        async def _send():
            await asyncio.sleep(delay)
            kb=_make_kb([[CallbackButton(text="Выполнено",payload=f"cal_done_{_short_eid(ev_data.get('id',''))}")]])
            await bot_instance.send_message(user_id=uid,text=f"<b>Напоминание</b>\n\n{title}\nЧерез {mins} мин — в {ts}",attachments=[kb],notify=True)
        tk=f"{uid}_{ev_data.get('id','')}"
        if tk in reminder_tasks: reminder_tasks[tk].cancel()
        reminder_tasks[tk]=asyncio.create_task(_send())
    except Exception as e: logger.error(f"Reminder: {e}")

# ==================== ПЕРИОДИЧЕСКИЕ НАПОМИНАНИЯ ====================
# Единая система — check_upcoming_reminders в конце файла

def _build_checklist_view(date, label, evs):
    text=f"<b>{label}</b> ({fmt_date(date)})\n\n"
    for i, ev in enumerate(evs, 1):
        done="[done]" in (ev.get("description","") or "").lower()
        mark="✓" if done else "○"
        text+=f"{mark} {i}. {fmt_event(ev).strip()}\n"
    busy=sum(1 for e in evs if "dateTime" in e.get("start",{}))
    if busy>=5: text+=f"\n<b>Загруженный день — {busy} событий!</b>"
    rows=[]; cur=[]
    date_str=date.strftime("%Y-%m-%d")
    for i, ev in enumerate(evs, 1):
        if "[done]" in (ev.get("description","") or "").lower(): continue
        cur.append(CallbackButton(text=f"✓ {i}", payload=f"cchk_{date_str}_{i}"))
        if len(cur)==5: rows.append(cur); cur=[]
    if cur: rows.append(cur)
    rows.append([CallbackButton(text="+ Добавить", payload="cal_add"),
                 CallbackButton(text="В начало", payload="reset")])
    return text, _make_kb(rows)

async def show_checklist(msg,date,label):
    evs=get_events(date,1)
    if not evs: await msg.answer(f"<b>{label}</b> ({fmt_date(date)}) — нет событий, день свободен."); return
    text, kb = _build_checklist_view(date, label, evs)
    await msg.answer(text, attachments=[kb])

@cal_router.message_callback(F.callback.payload.startswith("cchk_"))
async def cb_checklist_done(event: MessageCallback):
    pl=event.callback.payload[len("cchk_"):]
    date_str, _, idx_str = pl.partition("_")
    try: idx=int(idx_str)-1
    except ValueError: await event.bot.send_callback(event.callback.callback_id, notification=" "); return
    try: d=datetime.strptime(date_str,"%Y-%m-%d").date()
    except: d=now_msk().date()
    evs=get_events(d,1)
    if 0<=idx<len(evs):
        ev=evs[idx]; eid=ev.get("id",""); cid=ev.get("_cid",CALENDAR_PERSONAL)
        try:
            full=cal_service.events().get(calendarId=cid,eventId=eid).execute()
            desc=full.get("description","") or ""
            if "[done]" not in desc.lower():
                full["description"]=desc+"\n[DONE]"
                full["summary"]="✓ "+full.get("summary","")
                cal_service.events().update(calendarId=cid,eventId=eid,body=full).execute()
        except Exception as e:
            logger.error(f"Mark done: {e}")
    label="Сегодня" if d==now_msk().date() else ("Завтра" if d==now_msk().date()+timedelta(days=1) else fmt_date(d))
    evs=get_events(d,1)
    if not evs:
        try: await event.message.edit(text=f"<b>{label}</b> ({fmt_date(d)}) — нет событий.")
        except: pass
        await event.bot.send_callback(event.callback.callback_id, notification="Отмечено!"); return
    text, kb = _build_checklist_view(d, label, evs)
    try: await event.message.edit(text=text, attachments=[kb])
    except Exception as e: logger.warning(f"Checklist refresh: {e}")
    await event.bot.send_callback(event.callback.callback_id, notification="Отмечено!")

async def show_week(msg, start=None, days=7, label=None):
    """Показать события на N дней начиная со start (или сегодня)."""
    start = start or now_msk().date()
    days = max(1, int(days))
    if label is None:
        if days == 7:
            label = "Неделя"
        else:
            label = f"{days} дней"
    text = f"<b>{label}</b> {fmt_date(start)} – {fmt_date(start+timedelta(days=days-1))}\n"
    wd=["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]; tot=0
    day_btns=[]
    for i in range(days):
        d=start+timedelta(days=i); evs=get_events(d,1); tot+=len(evs)
        text+=f"\n<b>{wd[d.weekday()]} {d.strftime('%d.%m')}</b>"
        if not evs: text+=" — свободно"
        else:
            text+=f" ({len(evs)}):"
            for ev in evs[:4]: text+="\n"+fmt_event(ev)
            if len(evs)>4: text+=f"\n   ...ещё {len(evs)-4}"
        day_btns.append(CallbackButton(
            text=f"{wd[d.weekday()]} {d.strftime('%d.%m')}" + (f" ({len(evs)})" if evs else ""),
            payload=f"cday_{d.strftime('%Y-%m-%d')}"
        ))
    if tot>25: text+=f"\n\n<b>Период перегружен — {tot} событий!</b>"
    text+="\n\n<i>Нажми на день — увидишь его чеклист.</i>"
    rows = []
    for i in range(0, len(day_btns), 4):
        rows.append(day_btns[i:i+4])
    rows.append([CallbackButton(text="+ Добавить", payload="cal_add"),
                 CallbackButton(text="В начало", payload="reset")])
    kb=_make_kb(rows)
    if len(text)>4000:
        for p in [text[i:i+4000] for i in range(0,len(text),4000)][:-1]: await msg.answer(p)
        await msg.answer([text[i:i+4000] for i in range(0,len(text),4000)][-1], attachments=[kb])
    else: await msg.answer(text, attachments=[kb])

@cal_router.message_callback(F.callback.payload.startswith("cday_"))
async def cb_open_day(event: MessageCallback):
    date_str=event.callback.payload[len("cday_"):]
    try: d=datetime.strptime(date_str,"%Y-%m-%d").date()
    except: d=now_msk().date()
    label="Сегодня" if d==now_msk().date() else ("Завтра" if d==now_msk().date()+timedelta(days=1) else fmt_date(d))
    await show_checklist(event.message, d, label)
    await event.bot.send_callback(event.callback.callback_id, notification=" ")

async def handle_calendar_text(msg):
    uid=msg.sender.user_id; text=(msg.body.text or "").strip(); t=text.lower()
    if any(w in t for w in ["что сегодня","план на сегодня","мои дела"]): await show_checklist(msg,now_msk().date(),"Сегодня"); return True
    if any(w in t for w in ["что завтра","план на завтра"]): await show_checklist(msg,now_msk().date()+timedelta(days=1),"Завтра"); return True
    if any(w in t for w in ["что на неделю","план на неделю"]): await show_week(msg); return True
    w=await msg.answer("Обрабатываю...")
    try:
        n=now_msk(); ctx=""
        le=last_event.get(uid)
        if le: ctx=f"\nПоследнее событие: \"{le.get('summary','')}\" id={le.get('id','')}"
        # Вычисляем ближайшие дни недели
        today_wd = n.weekday()  # 0=пн
        def next_weekday(wd):
            days = (wd - today_wd) % 7
            if days == 0: days = 7  # если сегодня такой день — берём следующую неделю
            return (n + timedelta(days=days)).strftime("%Y-%m-%d")
        prompt=CALENDAR_PROMPT.format(
            today=n.strftime("%Y-%m-%d"),
            weekday=WEEKDAYS_RU[n.weekday()],
            tomorrow=(n+timedelta(days=1)).strftime("%Y-%m-%d"),
            next_mon=next_weekday(0),
            next_tue=next_weekday(1),
            next_wed=next_weekday(2),
            next_thu=next_weekday(3),
            next_fri=next_weekday(4),
            next_week=(n+timedelta(days=7)).strftime("%Y-%m-%d"),
            context=ctx
        )
        r=claude.messages.create(model="claude-haiku-4-5-20251001",max_tokens=2500,system=prompt,messages=[{"role":"user","content":text}])
        raw=r.content[0].text.strip()
        if raw.startswith("```"): raw=raw.split("\n",1)[1] if "\n" in raw else raw[3:]; raw=raw.rsplit("```",1)[0]
        parsed=json.loads(raw)

        # Список задач: создаём все события без переспроса времени
        if isinstance(parsed, list):
            created=[]; failed=[]
            for item in parsed:
                if not isinstance(item, dict) or item.get("action","create") != "create":
                    continue
                try:
                    result=create_event(item); last_event[uid]=result
                    await schedule_reminder(uid,result,item.get("reminder_minutes",15))
                    created.append(item)
                except Exception as ex:
                    logger.error(f"Cal batch item: {ex}")
                    failed.append(item.get("title","?"))
            lines=[f"✅ Создано: {len(created)}"]
            for it in created:
                t=it.get("time") or "весь день"
                tt=f" в {t}" if it.get("time") else " — весь день"
                lines.append(f"• {it.get('date','')}{tt}: {it.get('title','')}")
            if failed: lines.append(f"\nНе создано: {', '.join(failed)}")
            try: await w.message.edit(text="\n".join(lines))
            except: await msg.answer("\n".join(lines))
            return True

        data=parsed; action=data.get("action","create")

        if action=="list":
            ld=data.get("list_date"); days=data.get("list_days",1) or 1
            try:
                d=datetime.strptime(ld,"%Y-%m-%d").date() if ld else now_msk().date()
            except Exception:
                d=now_msk().date()
            try: days=max(1, int(days))
            except Exception: days=1
            try: await w.message.delete()
            except: pass
            if days>1:
                # Заголовок диапазона: «Неделя» если 7 дней и старт=сегодня;
                # иначе «N дней с DD.MM по DD.MM».
                today=now_msk().date()
                if days==7 and d==today:
                    label="Неделя"
                else:
                    label=f"С {d.strftime('%d.%m')} по {(d+timedelta(days=days-1)).strftime('%d.%m')}"
                await show_week(msg, start=d, days=days, label=label)
            else:
                label=("Сегодня" if d==now_msk().date()
                       else ("Завтра" if d==now_msk().date()+timedelta(days=1)
                             else fmt_date(d)))
                await show_checklist(msg, d, label)
            return True

        if action=="create":
            if not data.get("time"):
                cal_states[uid]={"step":"cal_ask_time","event_data":data}
                await w.message.edit(text=f"<b>{data.get('title','')}</b>\nДата: {data.get('date','')}\n\nВо сколько? (например 14:00 / весь день)")
                return True
            conflicts=check_conflicts(data.get("date"),data.get("time"),data.get("duration_minutes"))
            if conflicts:
                cal_states[uid]={"step":"cal_confirm","event_data":data}
                ct="\n".join([f"  ! {fmt_event(c)}" for c in conflicts])
                slots=suggest_free_slots(data.get("date"), data.get("time"), data.get("duration_minutes") or 60, count=3)
                slots_text=""
                if slots:
                    slots_text="\n\n<i>Можно сдвинуть на свободный слот:</i>"
                kb=_build_conflict_kb(slots, with_force=True)
                await w.message.edit(
                    text=(
                        f"<b>⚠️ Конфликт</b>\nЗапрошено: {data.get('time','')} «{data.get('title','')}»\n\n"
                        f"Уже занято:\n{ct}\n\n"
                        f"<i>Может делегируешь то, что мешает? Открой день в /день и поправь существующее.</i>"
                        f"{slots_text}"
                    ),
                    attachments=[kb],
                ); return True
            result=create_event(data); last_event[uid]=result
            await schedule_reminder(uid,result,data.get("reminder_minutes",15))
            cn={"work":"Рабочий","family":"Семейный","personal":"Личный"}.get(data.get("calendar","personal"),"Личный")
            cat=f"\nКатегория: {data['category']}" if data.get("category") else ""
            time_str = f"в {data.get('time','')}" if data.get('time') else "весь день"
            end_str = f"–{data['end_time']}" if data.get('end_time') else ""
            rm = data.get('reminder_minutes', 15)
            desc = data.get('description','')
            confirm_text = (
                f"✅ <b>{data.get('title','')}</b>\n\n"
                f"{data.get('date','')} {time_str}{end_str}\n"
                f"{cn}{cat}\n"
                f"Напоминание за {rm} мин"
            )
            if desc:
                short_desc = desc[:150]
                confirm_text += f"\n{short_desc}"
            try:
                await w.message.edit(text=confirm_text)
            except:
                await msg.answer(confirm_text)
            return True

        if action=="edit":
            target=last_event.get(uid) if data.get("refers_to_last") else None
            if not target and data.get("search_text"): target=find_event(data["search_text"])
            if not target and uid in last_event: target=last_event[uid]
            if not target: await w.message.edit(text="Не нашёл событие. Уточни название."); return True
            cid=target.get("_cid",CALENDAR_PERSONAL); eid=target["id"]
            field=data.get("edit_field",""); val=data.get("edit_value","")
            if field=="reminder" and val:
                mins=int(val) if str(val).isdigit() else 15
                target["reminders"]={"useDefault":False,"overrides":[{"method":"popup","minutes":mins}]}
                cal_service.events().update(calendarId=cid,eventId=eid,body=target).execute()
                await schedule_reminder(uid,target,mins)
                await w.message.edit(text=f"Напоминание «{target.get('summary','')}» — за {mins} мин")
            elif field=="time" and val:
                d=target["start"].get("dateTime","")[:10]; target["start"]["dateTime"]=f"{d}T{val}:00+03:00"
                end=datetime.strptime(f"{d} {val}","%Y-%m-%d %H:%M")+timedelta(minutes=60)
                target["end"]={"dateTime":end.strftime("%Y-%m-%dT%H:%M:00+03:00"),"timeZone":TIMEZONE}
                cal_service.events().update(calendarId=cid,eventId=eid,body=target).execute()
                await w.message.edit(text=f"Время «{target.get('summary','')}» → {val}")
            else: await w.message.edit(text="Не понял что изменить.")
            return True

        if action=="move":
            target=last_event.get(uid) if data.get("refers_to_last") else None
            if not target and data.get("search_text"): target=find_event(data["search_text"])
            if not target: await w.message.edit(text="Не нашёл событие."); return True
            nd=data.get("move_date") or data.get("date"); nt=data.get("move_time") or data.get("time")
            if nd and nt:
                target["start"]={"dateTime":f"{nd}T{nt}:00+03:00","timeZone":TIMEZONE}
                end=datetime.strptime(f"{nd} {nt}","%Y-%m-%d %H:%M")+timedelta(minutes=60)
                target["end"]={"dateTime":end.strftime("%Y-%m-%dT%H:%M:00+03:00"),"timeZone":TIMEZONE}
                cal_service.events().update(calendarId=target.get("_cid",CALENDAR_PERSONAL),eventId=target["id"],body=target).execute()
                await w.message.edit(text=f"«{target.get('summary','')}» → {nd} {nt}")
            else: await w.message.edit(text="Укажи дату и время.")
            return True

        if action=="delete":
            target=last_event.get(uid) if data.get("refers_to_last") else None
            if not target and data.get("search_text"): target=find_event(data["search_text"])
            if not target: await w.message.edit(text="Не нашёл событие."); return True
            cal_service.events().delete(calendarId=target.get("_cid",CALENDAR_PERSONAL),eventId=target["id"]).execute()
            last_event.pop(uid,None); await w.message.edit(text=f"«{target.get('summary','')}» удалено."); return True

        if action=="done":
            target=find_event(data.get("search_text","")) if data.get("search_text") else last_event.get(uid)
            if not target: await w.message.edit(text="Не нашёл задачу."); return True
            cid=target.get("_cid",CALENDAR_PERSONAL); desc=target.get("description","") or ""
            if "[done]" not in desc.lower():
                target["description"]=desc+"\n[DONE]"; target["summary"]="✓ "+target.get("summary","")
                cal_service.events().update(calendarId=cid,eventId=target["id"],body=target).execute()
            await w.message.edit(text=f"✓ «{target.get('summary','')}» выполнено!"); return True

        await w.message.edit(text="Не понял. Попробуй: добавь, перенеси, удали, что завтра..."); return True
    except json.JSONDecodeError:
        try: await w.message.edit(text="Не разобрал. Попробуй иначе.")
        except: await msg.answer("Не разобрал. Попробуй иначе.")
        return True
    except Exception as e:
        logger.error(f"Cal: {e}")
        err = str(e)[:100].replace("<","&lt;").replace(">","&gt;")
        try: await w.message.edit(text=f"Ошибка: {err}")
        except: await msg.answer(f"Ошибка: {err}")
        return True

@cal_router.message_callback(F.callback.payload=="cal_today")
async def cb_today(event: MessageCallback):
    await show_checklist(event.message, now_msk().date(), "Сегодня")
    await event.bot.send_callback(event.callback.callback_id, notification=" ")

@cal_router.message_callback(F.callback.payload=="cal_tomorrow")
async def cb_tmrw(event: MessageCallback):
    await show_checklist(event.message, now_msk().date()+timedelta(days=1), "Завтра")
    await event.bot.send_callback(event.callback.callback_id, notification=" ")

@cal_router.message_callback(F.callback.payload=="cal_week")
async def cb_week(event: MessageCallback):
    await show_week(event.message)
    await event.bot.send_callback(event.callback.callback_id, notification=" ")

@cal_router.message_callback(F.callback.payload=="cal_add")
async def cb_add(event: MessageCallback):
    cal_states[event.callback.user.user_id]={"step":"calendar_input"}
    await event.message.answer("Напиши что добавить:\n• Встреча завтра в 14:00\n• Рейс SU1234 15 апреля 8:30\n• Позвонить Кристине послезавтра")
    await event.bot.send_callback(event.callback.callback_id, notification=" ")

@cal_router.message_callback(F.callback.payload=="cal_force")
async def cb_force(event: MessageCallback):
    uid=event.callback.user.user_id; data=cal_states.get(uid,{}).get("event_data")
    if not data:
        await event.bot.send_callback(event.callback.callback_id, notification=" ")
        return
    result=create_event(data); last_event[uid]=result; cal_states.pop(uid,None)
    await schedule_reminder(uid,result,data.get("reminder_minutes",15))
    await event.message.edit(text=f"«{data.get('title','')}» создано.")
    await event.bot.send_callback(event.callback.callback_id, notification=" ")

@cal_router.message_callback(F.callback.payload.startswith("cal_alt_"))
async def cb_apply_alt_time(event: MessageCallback):
    uid = event.callback.user.user_id
    data = cal_states.get(uid, {}).get("event_data")
    if not data:
        await event.bot.send_callback(event.callback.callback_id, notification=" ")
        return
    raw = event.callback.payload[len("cal_alt_"):]
    if len(raw) != 4 or not raw.isdigit():
        await event.bot.send_callback(event.callback.callback_id, notification=" ")
        return
    new_time = f"{raw[:2]}:{raw[2:]}"
    try:
        datetime.strptime(new_time, "%H:%M")
    except ValueError:
        await event.bot.send_callback(event.callback.callback_id, notification=" ")
        return

    data["time"] = new_time
    data["end_time"] = None
    try:
        result = create_event(data); last_event[uid] = result
        await schedule_reminder(uid, result, data.get("reminder_minutes", 15))
        cal_states.pop(uid, None)
        await event.message.edit(
            text=(
                f"✅ <b>{data.get('title','')}</b>\n"
                f"{data.get('date','')} в {new_time}\n"
                f"<i>Перенесла на свободный слот.</i>"
            )
        )
    except Exception as e:
        logger.error(f"Cal alt time create: {e}")
    await event.bot.send_callback(event.callback.callback_id, notification=" ")

@cal_router.message_callback(F.callback.payload=="cal_cancel")
async def cb_cancel(event: MessageCallback):
    cal_states.pop(event.callback.user.user_id, None)
    await event.message.edit(text="Отменено.")
    await event.bot.send_callback(event.callback.callback_id, notification=" ")

@cal_router.message_callback(F.callback.payload.startswith("cal_done_"))
async def cb_done(event: MessageCallback):
    token=event.callback.payload[9:]
    eid=_eid_map.get(token, token)
    for cid in [CALENDAR_WORK,CALENDAR_FAMILY,CALENDAR_PERSONAL]:
        if not cid: continue
        try:
            ev=cal_service.events().get(calendarId=cid,eventId=eid).execute()
            d=ev.get("description","") or ""
            if "[done]" not in d.lower(): ev["description"]=d+"\n[DONE]"; ev["summary"]="✓ "+ev.get("summary","")
            cal_service.events().update(calendarId=cid,eventId=eid,body=ev).execute()
            await event.message.edit(text=f"✓ {ev.get('summary','')} выполнено!")
            await event.bot.send_callback(event.callback.callback_id, notification=" ")
            return
        except: continue

async def handle_cal_time_input(msg):
    uid=msg.sender.user_id; data=cal_states.get(uid,{}).get("event_data")
    if not data: return False
    t=(msg.body.text or "").strip().lower()
    if "весь день" in t or t in ("весь","всд","вд"):
        data["time"]=None; result=create_event(data); last_event[uid]=result; cal_states.pop(uid,None)
        await schedule_reminder(uid,result,data.get("reminder_minutes",15))
        cn={"work":"Рабочий","family":"Семейный","personal":"Личный"}.get(data.get("calendar","personal"),"Личный")
        await msg.answer(text=
            f"✅ <b>{data.get('title','')}</b>\n\n"
            f"{data.get('date','')} — весь день\n"
            f"{cn}"
        ); return True
    # Парсинг времени — поддерживаем "14:00", "14.00", "14 00", "14", "2 часа"
    m=re.search(r'(\d{1,2})[:\.](\d{2})',t)
    if not m:
        m2=re.search(r'^(\d{1,2})$',t.strip())
        if m2:
            data["time"]=f"{int(m2.group(1)):02d}:00"
        else:
            m3=re.search(r'(\d{1,2})\s*(час|ч\b)',t)
            if m3: data["time"]=f"{int(m3.group(1)):02d}:00"
            else: await msg.answer(text="Не понял. Напиши время: 14:00 или «весь день»"); return True
    else: data["time"]=f"{int(m.group(1)):02d}:{m.group(2)}"
    # Парсинг времени окончания если есть диапазон "10:00-13:00"
    m_end=re.search(r'[-–]\s*(\d{1,2})[:\.](\d{2})',t)
    if m_end: data["end_time"]=f"{int(m_end.group(1)):02d}:{m_end.group(2)}"
    conflicts=check_conflicts(data.get("date"),data["time"],data.get("duration_minutes"))
    if conflicts:
        cal_states[uid]={"step":"cal_confirm","event_data":data}
        slots=suggest_free_slots(data.get("date"), data["time"], data.get("duration_minutes") or 60, count=3)
        slots_text="\n\n<i>Можно сдвинуть на свободный слот:</i>" if slots else ""
        kb=_build_conflict_kb(slots, with_force=True)
        await msg.answer(
            text=(
                f"<b>⚠️ Конфликт</b>\nЗапрошено: {data['time']} «{data.get('title','')}»\n\n"
                f"Уже занято:\n"+"\n".join([f"  ! {fmt_event(c)}" for c in conflicts])+
                "\n\n<i>Может делегируешь то, что мешает? Открой день в /день и поправь существующее.</i>"+
                slots_text
            ),
            attachments=[kb],
        ); return True
    result=create_event(data); last_event[uid]=result; cal_states.pop(uid,None)
    await schedule_reminder(uid,result,data.get("reminder_minutes",15))
    cn={"work":"Рабочий","family":"Семейный","personal":"Личный"}.get(data.get("calendar","personal"),"Личный")
    end_str=f"–{data['end_time']}" if data.get("end_time") else ""
    rm=data.get("reminder_minutes",15)
    await msg.answer(
        text=(
            f"✅ <b>{data.get('title','')}</b>\n\n"
            f"{data.get('date','')} в {data['time']}{end_str}\n"
            f"{cn}\n"
            f"Напоминание за {rm} мин"
        )
    ); return True

def is_calendar_intent(text):
    t=text.lower()
    return any(k in t for k in ["встреча","событие","напомни","поставь","запиши в календарь","что у меня","что на сегодня","что завтра","расписание","перенеси","отмени","задач","дело","дела","рейс","перелёт","перелет","маникюр","врач","позвонить","созвон","бронь","что на неделю","план на","в календарь","добавь","напоминание за","сделано","выполнено","удали","измени время","мои дела"])

# ==================== ПЕРИОДИЧЕСКИЕ НАПОМИНАНИЯ ====================
_sent_reminders = set()  # Запоминаем отправленные напоминания (event_id + time)

async def check_upcoming_reminders():
    """Проверяет ближайшие события и шлёт напоминания за 15 и 5 мин."""
    logger.info("calendar reminder loop: started")
    while True:
        try:
            await asyncio.sleep(300)  # проверяем каждые 5 минут
            if not cal_service:
                logger.info("calendar reminder: cal_service пустой, пропускаю тик")
                continue
            if not bot_instance:
                logger.info("calendar reminder: bot_instance пустой, пропускаю тик")
                continue
            if not ALLOWED_USERS:
                logger.info("calendar reminder: ALLOWED_USERS пустой, пропускаю тик")
                continue
            now = now_msk()
            events = get_events(now.date(), 1)
            logger.info(f"calendar reminder tick: now={now.strftime('%H:%M')}, events_today={len(events)}")
            fired = 0
            for ev in events:
                s = ev.get("start", {})
                if "dateTime" not in s: continue
                ev_time = datetime.fromisoformat(s["dateTime"])
                diff = (ev_time - now).total_seconds() / 60  # минут до события
                eid = ev.get("id", "")
                title = ev.get("summary", "Событие")
                ts = ev_time.strftime("%H:%M")
                # Бэг был: 5-минутное напоминание стояло в elif — никогда не срабатывало,
                # потому что 0 < diff <= 5 всегда уже попадает в первый if (0 < diff <= 15).
                # Развёл на два независимых if.
                # Напоминание за 15 минут
                if 0 < diff <= 15:
                    key = f"{eid}_15"
                    if key not in _sent_reminders:
                        _sent_reminders.add(key)
                        desc = ev.get("description", "") or ""
                        text = f"🔔 <b>{title}</b> через {int(diff)} мин\nВ {ts}"
                        if desc:
                            import re as _re
                            urls = _re.findall(r'https?://\S+', desc)
                            clean = _re.sub(r'\[.*?\]', '', desc).strip()
                            if clean and len(clean) < 200:
                                text += f"\n{clean}"
                            if urls:
                                text += "\n" + "\n".join(urls)
                        kb = _make_kb([[CallbackButton(text="Выполнено", payload=f"cal_done_{_short_eid(eid)}")]])
                        for uid in ALLOWED_USERS:
                            try:
                                await bot_instance.send_message(user_id=uid, text=text, attachments=[kb], notify=True)
                                fired += 1
                                logger.info(f"calendar reminder 15min sent: uid={uid} title={title!r} diff={int(diff)}")
                            except Exception as e:
                                logger.error(f"Reminder 15min send failed uid={uid}: {e}")
                # Напоминание за 5 минут — независимый if, не elif.
                if 0 < diff <= 5:
                    key = f"{eid}_5"
                    if key not in _sent_reminders:
                        _sent_reminders.add(key)
                        for uid in ALLOWED_USERS:
                            try:
                                await bot_instance.send_message(user_id=uid, text=f"⚡ <b>{title}</b> — через {int(diff)} мин! В {ts}", notify=True)
                                fired += 1
                                logger.info(f"calendar reminder 5min sent: uid={uid} title={title!r}")
                            except Exception as e:
                                logger.error(f"Reminder 5min send failed uid={uid}: {e}")
            if fired:
                logger.info(f"calendar reminder tick done: fired={fired}")
            # Чистим старые записи (больше 1000)
            if len(_sent_reminders) > 1000:
                _sent_reminders.clear()
        except Exception as e:
            logger.error(f"Reminder loop: {e}", exc_info=True)
            await asyncio.sleep(60)

_last_digest_date = None
# Хук для расширения утренней сводки: callable (или async callable), возвращающий
# дополнительный текст. Регистрируется из bot.py.
morning_extras_callback = None

async def _send_daily_digest_to_all(today):
    if not bot_instance or not ALLOWED_USERS: return False
    evs = get_events(today, 1) if cal_service else []
    # База: события календаря
    if not evs:
        base_text = f"<b>☀️ Доброе утро!</b>\n{fmt_date(today)} — в календаре пусто."
        kb = None
    else:
        base_text, kb = _build_checklist_view(today, "☀️ Доброе утро! План на сегодня", evs)
    # Дополнения от bot.py: задачи, туры и пр.
    extras = ""
    if morning_extras_callback:
        try:
            res = morning_extras_callback()
            if asyncio.iscoroutine(res):
                res = await res
            extras = (res or "").strip()
        except Exception as e:
            logger.warning(f"Morning extras failed: {e}")
    text = base_text + (("\n\n" + extras) if extras else "")
    for uid in ALLOWED_USERS:
        try:
            await bot_instance.send_message(user_id=uid, text=text, attachments=[kb] if kb else [])
        except Exception as e:
            logger.error(f"Daily digest send to {uid}: {e}")
    return True

async def daily_digest_loop():
    """Каждое утро в 09:00 МСК шлёт план на день. Catch-up при старте — если бот
    рестартился между 09:00 и 10:30 МСК и сегодняшний digest ещё не уходил."""
    global _last_digest_date
    try:
        now = now_msk()
        today = now.date()
        target_today = now.replace(hour=9, minute=0, second=0, microsecond=0)
        catchup_deadline = now.replace(hour=10, minute=30, second=0, microsecond=0)
        if target_today <= now <= catchup_deadline and _last_digest_date != today:
            logger.info(f"Daily digest catch-up at startup ({now.strftime('%H:%M')})")
            if await _send_daily_digest_to_all(today):
                _last_digest_date = today
        elif now > catchup_deadline:
            _last_digest_date = today
    except Exception as e:
        logger.error(f"Daily digest catch-up: {e}")
    while True:
        try:
            now = now_msk()
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if target <= now: target = target + timedelta(days=1)
            delay = (target - now).total_seconds()
            await asyncio.sleep(delay)
            today = now_msk().date()
            if _last_digest_date == today: continue
            if await _send_daily_digest_to_all(today):
                _last_digest_date = today
        except Exception as e:
            logger.error(f"Daily digest loop: {e}")
            await asyncio.sleep(3600)


_bg_tasks: set = set()

def start_reminder_loop():
    """Запускает фоновую задачу проверки напоминаний.
    Сохраняем ссылки на таски, иначе CPython 3.13 GC их прибивает."""
    t1 = asyncio.ensure_future(check_upcoming_reminders())
    t2 = asyncio.ensure_future(daily_digest_loop())
    for t in (t1, t2):
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    logger.info("Reminder loop + daily digest started")

def init_calendar(cal_svc,claude_client,bot,work_cal,family_cal,personal_cal,tz,allowed):
    global cal_service,claude,bot_instance,CALENDAR_WORK,CALENDAR_FAMILY,CALENDAR_PERSONAL,TIMEZONE,ALLOWED_USERS
    cal_service=cal_svc; claude=claude_client; bot_instance=bot
    CALENDAR_WORK=work_cal; CALENDAR_FAMILY=family_cal; CALENDAR_PERSONAL=personal_cal
    TIMEZONE=tz; ALLOWED_USERS=allowed
    # Проверяем доступ к календарям при старте
    for cal_id, cal_name in [(work_cal,"Рабочий"),(family_cal,"Семейный"),(personal_cal,"Личный")]:
        if not cal_id: continue
        try:
            cal_svc.events().list(calendarId=cal_id, maxResults=1, timeMin=datetime.now(TZ_OFFSET).isoformat()).execute()
            logger.info(f"Calendar OK: {cal_name}")
        except Exception as e:
            logger.error(f"Calendar FAIL: {cal_name} ({cal_id[:30]}...): {e}")
    # Запускаем фоновый цикл проверки напоминаний
    start_reminder_loop()
