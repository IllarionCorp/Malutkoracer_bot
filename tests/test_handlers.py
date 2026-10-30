from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import unittest

from telegram import Message
from telegram.error import Forbidden

from booking_bot.handlers import TrackBot, build_application
from tests.test_booking import BookingFixture


class HandlerTests(BookingFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.handler = TrackBot(self.settings, self.service)
        self.message = MagicMock(spec=Message)
        self.message.chat = SimpleNamespace(type="private")
        self.message.reply_text = AsyncMock()
        self.user = SimpleNamespace(id=20, full_name="Пилот", username="driver")
        self.query = SimpleNamespace(message=self.message, data="", answer=AsyncMock())
        self.update = SimpleNamespace(message=self.message, callback_query=self.query, effective_user=self.user)
        self.context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))

    async def click(self, data):
        self.query.data = data
        await self.handler.callback(self.update, self.context)

    async def text(self, value):
        self.message.text = value
        await self.handler.text(self.update, self.context)

    async def prepare_confirmation(self):
        window = self.window()
        await self.click(f"hours:{window.id}:{self.timestamp(10)}:3")
        await self.text("а123вс77")
        return self.context.user_data["flow"]["nonce"]

    async def test_admin_creates_window_then_user_books_and_cancels(self):
        self.user.id = 1
        await self.click("admin:add")
        await self.text("10.05.2030")
        await self.text("10:00-20:00")
        await self.text("2500,50")
        await self.text("1")
        nonce = self.context.user_data["flow"]["nonce"]
        await self.click(f"admin:save:{nonce}")
        windows = self.store.windows(int(self.now.timestamp()))
        self.assertEqual(len(windows), 1)
        self.user.id = 20
        await self.click("book:0")
        await self.click("day:2030-05-10")
        await self.click(f"window:{windows[0].id}")
        await self.click(f"start:{windows[0].id}:{self.timestamp(10)}")
        await self.click(f"hours:{windows[0].id}:{self.timestamp(10)}:3")
        await self.text("а123вс77")
        self.assertIn("7 501,50 ₽", self.message.reply_text.call_args.args[0])
        nonce = self.context.user_data["flow"]["nonce"]
        await self.click(f"confirm:{nonce}")
        bookings = self.store.upcoming(int(self.now.timestamp()))
        self.assertEqual(len(bookings), 1)
        self.assertEqual(bookings[0].car_plate, "А123ВС77")
        self.context.bot.send_message.assert_awaited_once()
        await self.click(f"cancelask:{bookings[0].id}")
        await self.click(f"cancel:{bookings[0].id}")
        self.assertEqual(self.store.upcoming(int(self.now.timestamp())), [])

    async def test_forged_admin_button_is_denied(self):
        await self.click("admin:add")
        self.assertNotIn("flow", self.context.user_data)
        self.assertIn("администратору", self.message.reply_text.call_args.args[0])
        self.assertEqual(self.store.windows(int(self.now.timestamp())), [])

    async def test_regular_user_cannot_close_window(self):
        window = self.window()
        await self.click(f"admin:close:{window.id}")
        self.assertEqual(self.store.get_window(window.id).status, "open")

    async def test_stale_confirmation_does_not_create_booking(self):
        await self.prepare_confirmation()
        await self.click("confirm:wrong_nonce")
        self.assertEqual(self.store.upcoming(int(self.now.timestamp())), [])

    async def test_expired_confirmation_does_not_create_booking(self):
        nonce = await self.prepare_confirmation()
        self.now += timedelta(minutes=16)
        await self.click(f"confirm:{nonce}")
        self.assertEqual(self.store.upcoming(int(self.now.timestamp())), [])

    async def test_repeated_confirmation_does_not_duplicate_booking(self):
        nonce = await self.prepare_confirmation()
        await self.click(f"confirm:{nonce}")
        await self.click(f"confirm:{nonce}")
        self.assertEqual(len(self.store.upcoming(int(self.now.timestamp()))), 1)
        self.context.bot.send_message.assert_awaited_once()

    async def test_notification_failure_keeps_booking(self):
        nonce = await self.prepare_confirmation()
        self.context.bot.send_message.side_effect = Forbidden("bot blocked")
        with self.assertLogs("booking_bot.handlers", level="WARNING"):
            await self.click(f"confirm:{nonce}")
        self.assertEqual(len(self.store.upcoming(int(self.now.timestamp()))), 1)

    async def test_group_callback_is_rejected(self):
        self.user.id = 1
        self.message.chat.type = "supergroup"
        await self.click("admin:add")
        self.assertNotIn("flow", self.context.user_data)
        self.message.reply_text.assert_not_awaited()

    async def test_application_builds_without_network(self):
        application = build_application(self.settings, self.service)
        self.assertIsNotNone(application.updater)
        self.assertEqual(application.concurrent_updates, 1)
        # Создание клиента не обращается в Telegram; закрываем его ресурсы.
        await application.bot.shutdown()

    async def test_admin_can_set_capacity(self):
        self.user.id = 1
        await self.click("admin:add")
        await self.text("10.05.2030")
        await self.text("10:00-20:00")
        await self.text("2500")
        self.assertEqual(self.context.user_data["flow"]["stage"], "admin_capacity")
        await self.text("0")
        self.assertEqual(self.context.user_data["flow"]["stage"], "admin_capacity")
        await self.text("3")
        nonce = self.context.user_data["flow"]["nonce"]
        await self.click(f"admin:save:{nonce}")
        self.assertEqual(self.store.windows(int(self.now.timestamp()))[0].capacity, 3)

    async def test_public_view_shows_places_nickname_and_hours(self):
        window = self.window(end=14, capacity=3)
        self.book(window.id, hours=3, username="racer")
        await self.click(f"participants:{window.id}:0")
        text = self.message.reply_text.call_args.args[0]
        self.assertIn("осталось 2 из 3", text)
        self.assertIn("@racer — 3 ч", text)
        self.assertNotIn("А123ВС77", text)
        self.assertNotIn("Telegram ID", text)
        self.assertNotIn("7 501,50 ₽", text)
        self.assertNotIn("Пилот", text)

    async def test_public_view_uses_name_when_username_is_missing(self):
        window = self.window()
        self.book(window.id, username=None, full_name="Иван Петров")
        await self.click(f"participants:{window.id}:0")
        self.assertIn("Иван Петров (без username) — 1 ч", self.message.reply_text.call_args.args[0])

    async def test_full_window_stays_visible_in_public_schedule(self):
        window = self.window(end=11)
        self.book(window.id)
        await self.click("schedule:0")
        keyboard = self.message.reply_text.call_args.kwargs["reply_markup"]
        self.assertTrue(any(button.callback_data == "schedule_day:2030-05-10"
                            for row in keyboard.inline_keyboard for button in row))
        await self.click("schedule_day:2030-05-10")
        keyboard = self.message.reply_text.call_args.kwargs["reply_markup"]
        self.assertTrue(any(button.callback_data == f"participants:{window.id}:0"
                            for row in keyboard.inline_keyboard for button in row))
        await self.click(f"participants:{window.id}:0")
        self.assertIn("осталось 0 из 1", self.message.reply_text.call_args.args[0])

    async def test_participant_pages_and_refresh(self):
        window = self.window(capacity=6)
        for index in range(6):
            self.book(window.id, key=str(index), user_id=30 + index, username=f"racer{index}")
        await self.click(f"participants:{window.id}:0")
        text = self.message.reply_text.call_args.args[0]
        self.assertIn("@racer4", text)
        self.assertNotIn("@racer5", text)
        keyboard = self.message.reply_text.call_args.kwargs["reply_markup"]
        self.assertTrue(any(button.callback_data == f"participants:{window.id}:1"
                            for row in keyboard.inline_keyboard for button in row))
        await self.click(f"participants:{window.id}:1")
        self.assertIn("@racer5", self.message.reply_text.call_args.args[0])
        self.assertNotIn("@racer0", self.message.reply_text.call_args.args[0])

    async def test_public_refresh_reflects_cancellation(self):
        window = self.window(end=11, capacity=2)
        booking = self.book(window.id, username="racer")
        await self.click(f"participants:{window.id}:0")
        self.assertIn("осталось 1 из 2", self.message.reply_text.call_args.args[0])
        self.store.cancel(booking.id, booking.user_id, int(self.now.timestamp()))
        await self.click(f"participants:{window.id}:0")
        self.assertIn("осталось 2 из 2", self.message.reply_text.call_args.args[0])
        self.assertNotIn("@racer", self.message.reply_text.call_args.args[0])

    async def test_closed_window_lists_participants_without_accepting_bookings(self):
        window = self.window(end=11, capacity=2)
        self.book(window.id, username="racer")
        self.store.close_window(window.id)
        await self.click("schedule:0")
        keyboard = self.message.reply_text.call_args.kwargs["reply_markup"]
        self.assertTrue(any(button.callback_data == "schedule_day:2030-05-10"
                            for row in keyboard.inline_keyboard for button in row))
        await self.click(f"participants:{window.id}:0")
        text = self.message.reply_text.call_args.args[0]
        self.assertIn("Запись закрыта", text)
        self.assertIn("@racer", text)
        keyboard = self.message.reply_text.call_args.kwargs["reply_markup"]
        self.assertFalse(any(button.callback_data == f"window:{window.id}"
                             for row in keyboard.inline_keyboard for button in row))

    async def test_duration_displays_remaining_for_whole_booking(self):
        window = self.window(end=14, capacity=3)
        self.book(window.id, hour=11, hours=2)
        self.book(window.id, hour=12, hours=1, user_id=21, key="second")
        await self.click(f"start:{window.id}:{self.timestamp(10)}")
        keyboard = self.message.reply_text.call_args.kwargs["reply_markup"]
        labels = {button.callback_data: button.text for row in keyboard.inline_keyboard for button in row}
        self.assertIn("мест: 3", labels[f"hours:{window.id}:{self.timestamp(10)}:1"])
        self.assertIn("мест: 1", labels[f"hours:{window.id}:{self.timestamp(10)}:3"])


if __name__ == "__main__":
    unittest.main()

