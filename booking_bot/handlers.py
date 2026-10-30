from datetime import date, datetime
import logging
import secrets

from telegram import BotCommand, InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, Message, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .service import BookingService, money, normalize_plate, parse_price
from .storage import Availability, Booking, BookingError, Participant


logger = logging.getLogger(__name__)
PAGE_SIZE = 5
FLOW_LIFETIME = 15 * 60


def grid(buttons: list[Button], columns: int = 2) -> list[list[Button]]:
    return [buttons[index:index + columns] for index in range(0, len(buttons), columns)]


class TrackBot:
    def __init__(self, settings: Settings, service: BookingService):
        self.settings = settings
        self.service = service
        self.store = service.store

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    def local(self, timestamp: int, pattern: str = "%d.%m.%Y %H:%M") -> str:
        return datetime.fromtimestamp(timestamp, self.settings.timezone).strftime(pattern)

    def window_text(self, window: Availability) -> str:
        return (f"{self.local(window.starts_at)} — {self.local(window.ends_at, '%H:%M')} · "
                f"{money(window.hourly_price)}/ч · мест: {window.capacity}")

    def participant_text(self, participant: Participant) -> str:
        nickname = (f"@{participant.username}" if participant.username else
                    f"{' '.join(participant.full_name.split())[:80]} (без username)")
        return (f"{nickname} — {participant.hours} ч\n"
                f"{self.local(participant.starts_at)} — {self.local(participant.ends_at)}")

    def booking_text(self, booking: Booking, *, admin: bool = False) -> str:
        text = (
            f"Бронь #{booking.id}\n"
            f"{self.local(booking.starts_at)} — {self.local(booking.ends_at)}\n"
            f"Авто: {booking.car_plate}\n"
            f"{booking.hours} ч × {money(booking.hourly_price)} = {money(booking.total_price)}"
        )
        if admin:
            username = f" (@{booking.username})" if booking.username else ""
            text += f"\nКлиент: {booking.full_name}{username}\nTelegram ID: {booking.user_id}"
        return text

    def main_keyboard(self, user_id: int) -> Keyboard:
        rows = [[Button("Забронировать трек", callback_data="book:0")],
                [Button("Места и участники", callback_data="schedule:0")],
                [Button("Мои брони", callback_data="my:0")]]
        if self.is_admin(user_id):
            rows.append([Button("Управление треком", callback_data="admin:menu")])
        return Keyboard(rows)

    def new_flow(self, context: ContextTypes.DEFAULT_TYPE, stage: str, **values) -> dict:
        flow = {"stage": stage, "expires": int(self.service.now().timestamp()) + FLOW_LIFETIME,
                "nonce": secrets.token_hex(8), **values}
        context.user_data["flow"] = flow
        return flow

    def flow(self, context: ContextTypes.DEFAULT_TYPE, stage: str | None = None) -> dict:
        flow = context.user_data.get("flow")
        if not flow or flow["expires"] <= int(self.service.now().timestamp()) or (stage and flow["stage"] != stage):
            context.user_data.pop("flow", None)
            raise BookingError("Этот шаг устарел. Начни заново: /book или /admin.")
        return flow

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("flow", None)
        await update.message.reply_text(
            f"{self.settings.track_name}\n\nВыбери свободное время, количество часов и укажи госномер. "
            f"Перед подтверждением я покажу полную стоимость.\n\nВремя: {self.settings.timezone.key}.",
            reply_markup=self.main_keyboard(update.effective_user.id),
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = ("/book — забронировать трек\n/schedule — места и участники\n"
                "/my — мои брони и отмена\n/cancel — прервать ввод\n"
                "/id — мой Telegram ID\n\nБронь оформляется на целое число часов. "
                "Стоимость равна цене часа, умноженной на длительность. "
                "Отменить свою бронь можно до начала сеанса.")
        if self.is_admin(update.effective_user.id):
            text += "\n\n/admin — управление\n/addslot — добавить дату, время, цену и места\n/slots — окна записи"
        await update.message.reply_text(text)

    async def identity(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")

    async def abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("flow", None)
        await update.message.reply_text("Ввод отменён. Созданные брони можно посмотреть в /my.",
                                        reply_markup=self.main_keyboard(update.effective_user.id))

    async def book_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("flow", None)
        await self.show_dates(update.message, 0)

    async def my_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("flow", None)
        await self.show_bookings(update.message, update.effective_user.id, 0)

    async def schedule_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("flow", None)
        await self.show_dates(update.message, 0, public=True)

    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("Эта команда доступна администратору.")
            return
        context.user_data.pop("flow", None)
        await self.show_admin(update.message)

    async def add_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("Эта команда доступна администратору.")
            return
        await self.begin_window(update.message, context)

    async def slots_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("Эта команда доступна администратору.")
            return
        context.user_data.pop("flow", None)
        await self.show_windows(update.message, 0)

    async def show_dates(self, message: Message, page: int, *, public: bool = False) -> None:
        now = self.service.now()
        prefix = "schedule" if public else "book"
        day_prefix = "schedule_day" if public else "day"
        days = sorted({self.local(window.starts_at, "%Y-%m-%d")
                       for window in self.store.windows(int(now.timestamp()), include_closed=public)
                       if public or self.service.starts(window)})
        buttons = [Button(datetime.fromisoformat(day).strftime("%d.%m.%Y"), callback_data=f"{day_prefix}:{day}")
                   for day in days[page * 10:(page + 1) * 10]]
        rows = grid(buttons)
        navigation = []
        if page:
            navigation.append(Button("← Назад", callback_data=f"{prefix}:{page - 1}"))
        if len(days) > (page + 1) * 10:
            navigation.append(Button("Дальше →", callback_data=f"{prefix}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([Button("В меню", callback_data="home")])
        text = "Выбери дату:" if buttons else (
            "Расписание пока не опубликовано." if public else
            "Свободных дат пока нет. Администратор добавит время для записи."
        )
        await message.reply_text(text, reply_markup=Keyboard(rows))

    async def show_day(self, message: Message, day: date, *, public: bool = False) -> None:
        windows = [window for window in self.store.windows(int(self.service.now().timestamp()), include_closed=public)
                   if self.local(window.starts_at, "%Y-%m-%d") == day.isoformat()
                   and (public or self.service.starts(window))]
        rows = [[Button(self.window_text(window) + (" · запись закрыта" if window.status == "closed" else ""),
                        callback_data=f"participants:{window.id}:0" if public else f"window:{window.id}")]
                for window in windows]
        rows.append([Button("Выбрать другую дату", callback_data="schedule:0" if public else "book:0")])
        await message.reply_text("Выбери окно и тариф:" if windows else "На эту дату свободного времени уже нет.",
                                 reply_markup=Keyboard(rows))

    async def show_starts(self, message: Message, window_id: int) -> None:
        window = self.service.window(window_id)
        rows = grid([Button(f"{self.local(hour.starts_at, '%H:%M')} · мест: {hour.remaining}",
                            callback_data=f"start:{window_id}:{hour.starts_at}")
                     for hour in self.service.hour_availability(window) if hour.remaining > 0], 2)
        rows.append([Button("Места и участники этого окна", callback_data=f"participants:{window_id}:0")])
        rows.append([Button("К датам", callback_data="book:0")])
        await message.reply_text(f"{self.window_text(window)}\n\nВыбери время начала. Указано число мест на первый час; "
                                 "для всей брони остаток зависит от длительности.",
                                 reply_markup=Keyboard(rows))

    async def show_participants(self, message: Message, window_id: int, page: int) -> None:
        window = self.store.get_window(window_id)
        if window is None or window.ends_at <= int(self.service.now().timestamp()):
            raise BookingError("Это окно уже недоступно. Выбери дату: /schedule.")
        hours = self.service.hour_availability(window)
        availability = [
            f"{self.local(hour.starts_at, '%H:%M')}–{self.local(hour.starts_at + 3600, '%H:%M')}: "
            f"осталось {hour.remaining if window.status == 'open' else 0} из {window.capacity}"
            for hour in hours
        ]
        people = self.store.participants(window.starts_at, window.ends_at, limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE)
        text = (f"{self.window_text(window)}\nВремя: {self.settings.timezone.key}.\n\n"
                + ("Запись закрыта. Новые брони не принимаются.\n\n" if window.status == "closed" else "")
                + "Свободные места по часам:\n" + ("\n".join(availability) or "Времени для новых броней уже нет.")
                + "\n\nКто забронировал (ник, часы и время):\n\n"
                + ("\n\n".join(self.participant_text(person) for person in people[:PAGE_SIZE]) or "Броней на этой странице нет."))
        rows = []
        navigation = []
        if page:
            navigation.append(Button("← Назад", callback_data=f"participants:{window_id}:{page - 1}"))
        if len(people) > PAGE_SIZE:
            navigation.append(Button("Дальше →", callback_data=f"participants:{window_id}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        if window.status == "open" and any(hour.remaining > 0 for hour in hours):
            rows.append([Button("Забронировать место", callback_data=f"window:{window_id}")])
        rows.append([Button("Обновить", callback_data=f"participants:{window_id}:{page}")])
        rows.append([Button("К датам", callback_data="schedule:0")])
        await message.reply_text(text, reply_markup=Keyboard(rows))

    async def show_durations(self, message: Message, window_id: int, starts_at: int) -> None:
        window = self.service.window(window_id)
        durations = self.service.durations(window_id, starts_at)
        if not durations:
            raise BookingError("На это время уже нельзя записаться. Выбери другое: /book.")
        rows = grid([Button(f"{hours} ч · {money(window.hourly_price * hours)} · мест: "
                            f"{self.store.remaining(starts_at, starts_at + hours * 3600, window.capacity)}",
                            callback_data=f"hours:{window_id}:{starts_at}:{hours}") for hours in durations], 2)
        rows.append([Button("Другое время", callback_data=f"window:{window_id}")])
        await message.reply_text(f"Начало: {self.local(starts_at)}\nЦена часа: {money(window.hourly_price)}\n\nСколько часов нужно?",
                                 reply_markup=Keyboard(rows))

    async def show_bookings(self, message: Message, user_id: int, page: int, *, admin: bool = False) -> None:
        bookings = self.store.upcoming(int(self.service.now().timestamp()), user_id=None if admin else user_id,
                                       limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE)
        prefix = "admin:bookings" if admin else "my"
        rows = []
        if not admin:
            rows.extend([[Button(f"Отменить #{booking.id}", callback_data=f"cancelask:{booking.id}")]
                         for booking in bookings[:PAGE_SIZE] if booking.starts_at > int(self.service.now().timestamp())])
        navigation = []
        if page:
            navigation.append(Button("← Назад", callback_data=f"{prefix}:{page - 1}"))
        if len(bookings) > PAGE_SIZE:
            navigation.append(Button("Дальше →", callback_data=f"{prefix}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([Button("В меню", callback_data="admin:menu" if admin else "home")])
        text = "\n\n".join(self.booking_text(booking, admin=admin) for booking in bookings[:PAGE_SIZE])
        await message.reply_text(text or "Предстоящих броней нет.", reply_markup=Keyboard(rows))

    async def show_admin(self, message: Message) -> None:
        await message.reply_text("Управление гоночным треком", reply_markup=Keyboard([
            [Button("Добавить окно записи", callback_data="admin:add")],
            [Button("Окна записи", callback_data="admin:slots:0")],
            [Button("Все брони", callback_data="admin:bookings:0")],
            [Button("В главное меню", callback_data="home")],
        ]))

    async def begin_window(self, message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.new_flow(context, "admin_day")
        example = self.service.now().strftime("%d.%m.%Y")
        await message.reply_text(f"На какую дату открыть запись?\nУкажи ДД.ММ.ГГГГ, например {example}.\n\n/cancel — прервать ввод.")

    async def show_windows(self, message: Message, page: int) -> None:
        windows = self.store.windows(int(self.service.now().timestamp()))
        shown = windows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        text = "\n\n".join(f"Окно #{window.id}\n{self.window_text(window)}" for window in shown)
        rows = [[Button(f"Закрыть запись в окно #{window.id}", callback_data=f"admin:closeask:{window.id}")]
                for window in shown]
        navigation = []
        if page:
            navigation.append(Button("← Назад", callback_data=f"admin:slots:{page - 1}"))
        if len(windows) > (page + 1) * PAGE_SIZE:
            navigation.append(Button("Дальше →", callback_data=f"admin:slots:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([Button("Управление", callback_data="admin:menu")])
        await message.reply_text(text or "Окон записи пока нет. Добавь первое: /addslot.", reply_markup=Keyboard(rows))

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            flow = self.flow(context)
            text = update.message.text
            if flow["stage"].startswith("admin_") and not self.is_admin(update.effective_user.id):
                raise BookingError("Это действие доступно администратору.")
            match flow["stage"]:
                case "admin_day":
                    flow["day"] = self.service.parse_day(text)
                    flow["stage"] = "admin_time"
                    await update.message.reply_text("Укажи начало и конец окна, например 10:00-20:00.\nВремя местное: " + self.settings.timezone.key)
                case "admin_time":
                    flow["starts_at"], flow["ends_at"] = self.service.parse_times(flow["day"], text)
                    flow["stage"] = "admin_price"
                    await update.message.reply_text("Укажи цену одного часа в рублях, например 2500.")
                case "admin_price":
                    flow["hourly_price"] = parse_price(text)
                    flow["stage"] = "admin_capacity"
                    await update.message.reply_text("Сколько автомобилей могут одновременно быть на треке в этом окне?\nУкажи целое число от 1 до 1000.")
                case "admin_capacity":
                    try:
                        capacity = int(text.strip())
                    except ValueError:
                        raise BookingError("Количество мест должно быть целым числом от 1 до 1000.") from None
                    if not 1 <= capacity <= 1000:
                        raise BookingError("Количество мест должно быть целым числом от 1 до 1000.")
                    flow["capacity"] = capacity
                    flow["stage"] = "admin_confirm"
                    await update.message.reply_text(
                        f"Открыть запись?\n{self.local(flow['starts_at'])} — {self.local(flow['ends_at'])}\n"
                        f"Цена часа: {money(flow['hourly_price'])}\nМест для автомобилей: {capacity}",
                        reply_markup=Keyboard([[Button("Открыть запись", callback_data=f"admin:save:{flow['nonce']}")],
                                               [Button("Отмена", callback_data="admin:menu")]]),
                    )
                case "booking_plate":
                    flow["car_plate"] = normalize_plate(text)
                    flow["stage"] = "booking_confirm"
                    await update.message.reply_text(
                        f"Проверь бронь\n\n{self.settings.track_name}\n"
                        f"{self.local(flow['starts_at'])} — {self.local(flow['starts_at'] + flow['hours'] * 3600)}\n"
                        f"Авто: {flow['car_plate']}\n"
                        f"{flow['hours']} ч × {money(flow['hourly_price'])}\n"
                        f"Итого: {money(flow['total_price'])}\n\nБронь будет создана после подтверждения.",
                        reply_markup=Keyboard([[Button("Подтвердить бронь", callback_data=f"confirm:{flow['nonce']}")],
                                               [Button("Отмена", callback_data="home")]]),
                    )
                case _:
                    await update.message.reply_text("Используй кнопки подтверждения или /cancel.")
        except BookingError as error:
            await update.message.reply_text(str(error))

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        message = query.message
        if not isinstance(message, Message) or message.chat.type != "private":
            await query.answer("Открой личный чат с ботом.", show_alert=True)
            return
        await query.answer()
        data = query.data or ""
        user = update.effective_user
        try:
            if data.startswith("admin:"):
                if not self.is_admin(user.id):
                    raise BookingError("Это действие доступно администратору.")
                await self.admin_callback(data, message, context, user.id)
                return
            if data == "home":
                context.user_data.pop("flow", None)
                await message.reply_text("Выбери действие:", reply_markup=self.main_keyboard(user.id))
            elif data.startswith("book:"):
                context.user_data.pop("flow", None)
                await self.show_dates(message, max(0, int(data.split(":")[1])))
            elif data.startswith("schedule:"):
                context.user_data.pop("flow", None)
                await self.show_dates(message, max(0, int(data.split(":")[1])), public=True)
            elif data.startswith("schedule_day:"):
                context.user_data.pop("flow", None)
                await self.show_day(message, date.fromisoformat(data.split(":")[1]), public=True)
            elif data.startswith("participants:"):
                context.user_data.pop("flow", None)
                _, window_id, page = data.split(":")
                await self.show_participants(message, int(window_id), max(0, int(page)))
            elif data.startswith("day:"):
                context.user_data.pop("flow", None)
                await self.show_day(message, date.fromisoformat(data.split(":")[1]))
            elif data.startswith("window:"):
                context.user_data.pop("flow", None)
                await self.show_starts(message, int(data.split(":")[1]))
            elif data.startswith("start:"):
                context.user_data.pop("flow", None)
                _, window_id, starts_at = data.split(":")
                await self.show_durations(message, int(window_id), int(starts_at))
            elif data.startswith("hours:"):
                _, window_id, starts_at, hours = data.split(":")
                window_id, starts_at, hours = int(window_id), int(starts_at), int(hours)
                if hours not in self.service.durations(window_id, starts_at):
                    raise BookingError("На выбранную длительность времени уже нет. Выбери другое: /book.")
                window = self.service.window(window_id)
                self.new_flow(context, "booking_plate", window_id=window_id, starts_at=starts_at, hours=hours,
                              hourly_price=window.hourly_price, total_price=window.hourly_price * hours)
                await message.reply_text("Укажи госномер автомобиля, например А123ВС77.\n\n/cancel — прервать ввод.")
            elif data.startswith("confirm:"):
                flow = self.flow(context, "booking_confirm")
                if data.split(":")[1] != flow["nonce"]:
                    raise BookingError("Это старое подтверждение. Используй последнее сообщение или /book.")
                booking = self.service.book(
                    window_id=flow["window_id"], starts_at=flow["starts_at"], hours=flow["hours"],
                    user_id=user.id, full_name=user.full_name, username=user.username, car_plate=flow["car_plate"],
                    expected_price=flow["total_price"], request_key=f"{user.id}:{flow['nonce']}",
                )
                context.user_data.pop("flow", None)
                try:
                    await message.reply_text("Бронь подтверждена!\n\n" + self.booking_text(booking),
                                             reply_markup=self.main_keyboard(user.id))
                finally:
                    await self.notify_admins(context, "Новая бронь", booking)
            elif data.startswith("my:"):
                context.user_data.pop("flow", None)
                await self.show_bookings(message, user.id, max(0, int(data.split(":")[1])))
            elif data.startswith("cancelask:"):
                context.user_data.pop("flow", None)
                booking = self.store.get_owned(int(data.split(":")[1]), user.id)
                if not booking or booking.status != "active":
                    raise BookingError("Бронь не найдена. Посмотри /my.")
                await message.reply_text("Отменить эту бронь?\n\n" + self.booking_text(booking), reply_markup=Keyboard([
                    [Button("Да, отменить", callback_data=f"cancel:{booking.id}")],
                    [Button("Оставить бронь", callback_data="my:0")],
                ]))
            elif data.startswith("cancel:"):
                booking = self.store.cancel(int(data.split(":")[1]), user.id, int(self.service.now().timestamp()))
                try:
                    await message.reply_text(f"Бронь #{booking.id} отменена.", reply_markup=self.main_keyboard(user.id))
                finally:
                    await self.notify_admins(context, "Бронь отменена", booking)
            else:
                raise BookingError("Эта кнопка устарела. Открой /start.")
        except BookingError as error:
            await message.reply_text(str(error))
        except (ValueError, IndexError, OverflowError):
            await message.reply_text("Некорректная кнопка. Открой /start.")

    async def admin_callback(self, data: str, message: Message, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
        parts = data.split(":")
        action = parts[1]
        if action != "save":
            context.user_data.pop("flow", None)
        match action:
            case "menu":
                await self.show_admin(message)
            case "add":
                await self.begin_window(message, context)
            case "bookings":
                await self.show_bookings(message, user_id, max(0, int(parts[2])), admin=True)
            case "slots":
                await self.show_windows(message, max(0, int(parts[2])))
            case "save":
                flow = self.flow(context, "admin_confirm")
                if parts[2] != flow["nonce"]:
                    raise BookingError("Это старое подтверждение. Начни заново: /addslot.")
                window = self.store.add_window(starts_at=flow["starts_at"], ends_at=flow["ends_at"],
                                               hourly_price=flow["hourly_price"], admin_id=user_id,
                                               capacity=flow["capacity"],
                                               now=int(self.service.now().timestamp()))
                context.user_data.pop("flow", None)
                await message.reply_text(f"Запись открыта! Окно #{window.id}\n{self.window_text(window)}")
                await self.show_admin(message)
            case "closeask":
                window = self.service.window(int(parts[2]))
                await message.reply_text(
                    "Закрыть это окно для новых записей? Уже созданные брони сохранятся.\n\n" + self.window_text(window),
                    reply_markup=Keyboard([[Button("Закрыть запись", callback_data=f"admin:close:{window.id}")],
                                           [Button("Оставить открытым", callback_data="admin:slots:0")]]),
                )
            case "close":
                changed = self.store.close_window(int(parts[2]))
                await message.reply_text("Запись закрыта. Созданные брони сохранены." if changed else "Окно уже закрыто.")
            case _:
                raise BookingError("Неизвестное действие. Открой /admin.")

    async def notify_admins(self, context: ContextTypes.DEFAULT_TYPE, title: str, booking: Booking) -> None:
        for admin_id in self.settings.admin_ids:
            try:
                await context.bot.send_message(admin_id, title + "\n\n" + self.booking_text(booking, admin=True))
            except TelegramError as error:
                # Ошибка уведомления не отменяет уже сохранённую бронь.
                logger.warning("Не доставлено уведомление администратору %s: %s", admin_id, type(error).__name__)

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Не выводим Update и URL запросов: они могут содержать персональные данные и токен.
        logger.error("Ошибка обработки обновления: %s", type(context.error).__name__)
        if isinstance(update, Update) and update.effective_message and update.effective_chat.type == "private":
            try:
                await update.effective_message.reply_text("Не удалось завершить действие. Проверь /my перед повторной записью.")
            except TelegramError:
                pass


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Главное меню"), BotCommand("book", "Забронировать трек"),
        BotCommand("schedule", "Свободные места и участники"),
        BotCommand("my", "Мои брони"), BotCommand("cancel", "Прервать ввод"),
        BotCommand("id", "Мой Telegram ID"), BotCommand("help", "Помощь"),
    ])


def build_application(settings: Settings, service: BookingService) -> Application:
    handlers = TrackBot(settings, service)
    application = Application.builder().token(settings.token).concurrent_updates(False).post_init(post_init).build()
    commands = {
        "start": handlers.start, "help": handlers.help, "id": handlers.identity,
        "cancel": handlers.abort, "book": handlers.book_command, "my": handlers.my_command,
        "schedule": handlers.schedule_command,
        "admin": handlers.admin_command, "addslot": handlers.add_command, "slots": handlers.slots_command,
    }
    for command, handler in commands.items():
        application.add_handler(CommandHandler(command, handler, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(handlers.callback))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handlers.text))
    application.add_error_handler(handlers.error)
    return application

