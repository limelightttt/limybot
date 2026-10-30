"""Customer-facing texts in Russian and Uzbek (Latin script). Staff and owners see Russian only.
The Mini App gets APP_TEXTS as JSON; the bot uses t(). Uzbek wording should be checked by a native speaker."""

from html import escape

from .models import Cafe, Order, title

LANGS = ("ru", "uz")

STATUS = {
    "ru": {
        "new": "Новый",
        "accepted": "Принят",
        "cooking": "Готовится",
        "ready": "Готов",
        "done": "Выдан",
        "courier": "Передан курьеру",
        "delivered": "Доставлен",
        "cancelled": "Отменён",
    },
    "uz": {
        "new": "Yangi",
        "accepted": "Qabul qilindi",
        "cooking": "Tayyorlanmoqda",
        "ready": "Tayyor",
        "done": "Berildi",
        "courier": "Kuryerga berildi",
        "delivered": "Yetkazildi",
        "cancelled": "Bekor qilindi",
    },
}

BOT_TEXTS = {
    "ru": {
        "open_menu": "☕️ Открыть меню",
        "my_orders": "📋 Мои заказы",
        "switch_lang": "🇺🇿 Oʻzbekcha",
        "inactive": "«{name}» сейчас не принимает заказы через бота. Позвоните нам или загляните лично 🙌",
        # what the customer is told when staff move the order; ready/courier differ by how it is received
        "status_accepted": "✅ <b>Заказ №{number} принят</b>\nСкоро начнём готовить.",
        "status_cooking": "👨‍🍳 <b>Заказ №{number} готовится</b>",
        "status_ready": "🔔 <b>Заказ №{number} готов!</b>\nЖдём вас: {address}",
        "status_courier": "🛵 <b>Заказ №{number} у курьера</b>\nСкоро будет у вас.",
        "status_cancelled": "❌ <b>Заказ №{number} отменён кафе</b>\nЕсли это ошибка — свяжитесь с нами.",
        "order_sent": "✅ <b>Заказ №{number} отправлен</b> · {name}",
        "pickup": "🏃 Самовывоз: {address}",
        "delivery": "🛵 Доставка: {address}",
        "delivery_fee": "Доставка",
        "total": "Итого",
        "comment": "💬 {comment}",
        "pay_cash": "💵 Наличными при получении",
        "pay_transfer": "💳 Перевод на карту",
        "transfer_howto": "Переведите {total} на карту <code>{card}</code> ({holder}) и нажмите «Я перевёл».",
        "transfer_claimed": "Пришлите сюда скриншот перевода — так кафе быстрее его найдёт.",
        "screenshot_ok": "Спасибо! Скриншот передан кафе.",
        "change": "сдача с {amount}",
        "payment_received": "💳 <b>Оплата по заказу №{number} получена</b>\nСпасибо!",
        "we_will_write": "Мы напишем, когда заказ примут.",
        "status_line": "Статус: {status}",
        "copy_card": "📋 Скопировать номер карты",
        "i_paid": "✅ Я перевёл",
        "cancel_order": "✖️ Отменить заказ",
        "cancel_no": "Заказ уже готовят — отменить нельзя. Позвоните нам, если что-то не так.",
        "cancelled_ok": "Заказ №{number} отменён",
        "claim_no": "Этот заказ уже не ждёт перевода",
        "claim_ok": "Спасибо! Кафе проверит перевод",
        "orders_title": "📋 Ваши последние заказы",
        "orders_empty": "Заказов пока нет — откройте меню 👇",
        "repeat": "🔁 Повторить №{number}",
        "cancel_n": "✖️ Отменить №{number}",
        "privacy": (
            "Что мы храним: имя в Telegram, телефон, адрес доставки и историю заказов в «{name}». "
            "Это нужно, чтобы выполнить заказ и предложить ваши данные в следующий раз. "
            "Данные видит только персонал кафе, третьим лицам они не передаются. "
            "Чтобы удалить их — напишите кафе."
        ),
    },
    "uz": {
        "open_menu": "☕️ Menyuni ochish",
        "my_orders": "📋 Buyurtmalarim",
        "switch_lang": "🇷🇺 Русский",
        "inactive": "«{name}» hozir bot orqali buyurtma qabul qilmayapti. Bizga qoʻngʻiroq qiling yoki keling 🙌",
        "status_accepted": "✅ <b>№{number} buyurtma qabul qilindi</b>\nTez orada tayyorlashni boshlaymiz.",
        "status_cooking": "👨‍🍳 <b>№{number} buyurtma tayyorlanmoqda</b>",
        "status_ready": "🔔 <b>№{number} buyurtma tayyor!</b>\nSizni kutamiz: {address}",
        "status_courier": "🛵 <b>№{number} buyurtma kuryerda</b>\nTez orada yetib boradi.",
        "status_cancelled": "❌ <b>№{number} buyurtma kafe tomonidan bekor qilindi</b>\nXato boʻlsa — biz bilan bogʻlaning.",
        "order_sent": "✅ <b>№{number} buyurtma yuborildi</b> · {name}",
        "pickup": "🏃 Olib ketish: {address}",
        "delivery": "🛵 Yetkazib berish: {address}",
        "delivery_fee": "Yetkazib berish",
        "total": "Jami",
        "comment": "💬 {comment}",
        "pay_cash": "💵 Qabul qilganda naqd",
        "pay_transfer": "💳 Kartaga oʻtkazma",
        "transfer_howto": "{total} ni <code>{card}</code> kartaga ({holder}) oʻtkazing va «Oʻtkazdim» tugmasini bosing.",
        "transfer_claimed": "Oʻtkazma skrinshotini shu yerga yuboring — kafe uni tezroq topadi.",
        "screenshot_ok": "Rahmat! Skrinshot kafega yuborildi.",
        "change": "{amount} dan qaytim",
        "payment_received": "💳 <b>№{number} buyurtma uchun toʻlov qabul qilindi</b>\nRahmat!",
        "we_will_write": "Buyurtma qabul qilinganda xabar beramiz.",
        "status_line": "Holati: {status}",
        "copy_card": "📋 Karta raqamini nusxalash",
        "i_paid": "✅ Oʻtkazdim",
        "cancel_order": "✖️ Buyurtmani bekor qilish",
        "cancel_no": "Buyurtma tayyorlanmoqda — bekor qilib boʻlmaydi. Biror narsa notoʻgʻri boʻlsa, qoʻngʻiroq qiling.",
        "cancelled_ok": "№{number} buyurtma bekor qilindi",
        "claim_no": "Bu buyurtma oʻtkazmani kutmayapti",
        "claim_ok": "Rahmat! Kafe oʻtkazmani tekshiradi",
        "orders_title": "📋 Soʻnggi buyurtmalaringiz",
        "orders_empty": "Hali buyurtmalar yoʻq — menyuni oching 👇",
        "repeat": "🔁 №{number} ni takrorlash",
        "cancel_n": "✖️ №{number} ni bekor qilish",
        "privacy": (
            "Biz saqlaydigan maʼlumotlar: Telegramdagi ism, telefon, yetkazib berish manzili va «{name}»dagi "
            "buyurtmalar tarixi. Bu buyurtmani bajarish va keyingi safar maʼlumotlaringizni taklif qilish uchun kerak. "
            "Maʼlumotlarni faqat kafe xodimlari koʻradi va ular uchinchi shaxslarga berilmaydi. "
            "Ularni oʻchirish uchun kafega yozing."
        ),
    },
}

# Mini App strings; error_* match the error codes of POST /app/<slug>/orders
APP_TEXTS = {
    "ru": {
        "closed": "Сейчас закрыто. Работаем {hours}",
        "inactive": "Сейчас не принимаем заказы онлайн",
        "empty_menu": "Меню пока пустое — загляните чуть позже",
        "add": "Добавить",
        "cart": "Корзина",
        "checkout": "Оформить заказ",
        "your_order": "Ваш заказ",
        "clear": "Очистить",
        "pickup": "Самовывоз",
        "pickup_hint": "Заберу сам",
        "delivery": "Доставка",
        "delivery_hint": "Привезите",
        "delivery_from": "Доставка от {min}",
        "address": "Адрес доставки",
        "address_hint": "Улица, дом, подъезд, квартира",
        "phone": "Телефон",
        "share_phone": "Отправить номер из Telegram",
        "comment": "Комментарий",
        "comment_hint": "Например, без сахара",
        "payment": "Оплата",
        "cash": "Наличными",
        "cash_hint": "При получении",
        "transfer_hint": "Номер карты — после заказа",
        "online": "Click · Payme",
        "soon": "скоро",
        "change_from": "Сдача с купюры",
        "no_change": "Без сдачи",
        "other_amount": "Другая сумма",
        "transfer": "Перевод на карту",
        "subtotal": "Сумма",
        "delivery_fee": "Доставка",
        "total": "Итого",
        "confirm": "Подтвердить заказ · {total}",
        "back": "Назад",
        "pick_one": "выберите один",
        "pick_any": "можно несколько",
        "done_title": "Заказ №{number} отправлен",
        "done_text": "Статус заказа придёт в чат с ботом.",
        "transfer_text": "Переведите {total} на карту {card} ({holder}), затем нажмите «Я перевёл» в чате с ботом.",
        "copy": "Скопировать",
        "copied": "Скопировано",
        "close": "Закрыть",
        "repeat_missing": "Сейчас недоступно: {names}",
        "open_in_telegram": "Чтобы оформить заказ, откройте меню через бота в Telegram.",
        "error_closed": "Кафе сейчас закрыто",
        "error_inactive": "Кафе сейчас не принимает заказы онлайн",
        "error_menu_changed": "Меню изменилось — проверьте корзину",
        "error_min_delivery": "Доставка — от {min}",
        "error_no_delivery": "Доставки сейчас нет",
        "error_address": "Укажите адрес доставки",
        "error_phone": "Укажите номер телефона",
        "error_payment": "Этот способ оплаты недоступен",
        "error_change": "Сумма для сдачи меньше заказа",
        "error_unavailable": "Кафе временно недоступно, попробуйте через минуту",
        "error_network": "Нет связи, попробуйте ещё раз",
    },
    "uz": {
        "closed": "Hozir yopiq. Ish vaqti: {hours}",
        "inactive": "Hozir onlayn buyurtma qabul qilmayapmiz",
        "empty_menu": "Menyu hozircha boʻsh — keyinroq kiring",
        "add": "Qoʻshish",
        "cart": "Savat",
        "checkout": "Buyurtma berish",
        "your_order": "Buyurtmangiz",
        "clear": "Tozalash",
        "pickup": "Olib ketish",
        "pickup_hint": "Oʻzim olaman",
        "delivery": "Yetkazib berish",
        "delivery_hint": "Olib keling",
        "delivery_from": "Yetkazib berish {min} dan",
        "address": "Yetkazib berish manzili",
        "address_hint": "Koʻcha, uy, podyezd, xonadon",
        "phone": "Telefon",
        "share_phone": "Telegramdagi raqamni yuborish",
        "comment": "Izoh",
        "comment_hint": "Masalan, shakarsiz",
        "payment": "Toʻlov",
        "cash": "Naqd pul",
        "cash_hint": "Qabul qilganda",
        "transfer_hint": "Karta raqami — buyurtmadan soʻng",
        "online": "Click · Payme",
        "soon": "tez orada",
        "change_from": "Qaysi kupyuradan qaytim",
        "no_change": "Qaytimsiz",
        "other_amount": "Boshqa summa",
        "transfer": "Kartaga oʻtkazma",
        "subtotal": "Summa",
        "delivery_fee": "Yetkazib berish",
        "total": "Jami",
        "confirm": "Buyurtmani tasdiqlash · {total}",
        "back": "Orqaga",
        "pick_one": "bittasini tanlang",
        "pick_any": "bir nechtasini tanlash mumkin",
        "done_title": "№{number} buyurtma yuborildi",
        "done_text": "Buyurtma holati bot bilan chatga keladi.",
        "transfer_text": "{total} ni {card} kartaga ({holder}) oʻtkazing, soʻng bot chatida «Oʻtkazdim»ni bosing.",
        "copy": "Nusxalash",
        "copied": "Nusxalandi",
        "close": "Yopish",
        "repeat_missing": "Hozir mavjud emas: {names}",
        "open_in_telegram": "Buyurtma berish uchun menyuni Telegramdagi bot orqali oching.",
        "error_closed": "Kafe hozir yopiq",
        "error_inactive": "Kafe hozir onlayn buyurtma qabul qilmayapti",
        "error_menu_changed": "Menyu oʻzgardi — savatni tekshiring",
        "error_min_delivery": "Yetkazib berish — {min} dan",
        "error_no_delivery": "Hozir yetkazib berish yoʻq",
        "error_address": "Yetkazib berish manzilini kiriting",
        "error_phone": "Telefon raqamini kiriting",
        "error_payment": "Bu toʻlov usuli mavjud emas",
        "error_change": "Qaytim uchun summa buyurtmadan kam",
        "error_unavailable": "Kafe vaqtincha mavjud emas, bir daqiqadan soʻng urinib koʻring",
        "error_network": "Aloqa yoʻq, qayta urinib koʻring",
    },
}


def lang_of(code: str | None) -> str:
    """Telegram's language_code → one of LANGS."""
    return "uz" if code == "uz" else "ru"


def t(lang: str, key: str, **kw) -> str:
    return BOT_TEXTS[lang][key].format(**kw)


def money(cafe: Cafe, amount: int, lang: str) -> str:
    text = cafe.money(amount)
    return text.replace("сум", "soʻm") if lang == "uz" else text


def order_text(cafe: Cafe, o: Order, lang: str) -> str:
    """What the customer sees about their order (HTML): the lines, the total, how it is received and paid."""
    items = [
        f"{escape(title(ln['name'], ln['options']))} × {ln['qty']} — {money(cafe, ln['unit'] * ln['qty'], lang)}"
        for ln in o.lines
    ]
    lines = ["<blockquote>" + "\n".join(items) + "</blockquote>"]
    if o.delivery_fee:
        lines.append(f"{t(lang, 'delivery_fee')} — {money(cafe, o.delivery_fee, lang)}")
    lines.append(f"<b>{t(lang, 'total')}: {money(cafe, o.total, lang)}</b>")
    if o.method == "delivery":
        where = t(lang, "delivery", address=escape(o.address))
    else:
        where = t(lang, "pickup", address=escape(cafe.pickup_address))
    extra = [where, f"📱 {escape(o.phone)}"] + ([t(lang, "comment", comment=escape(o.comment))] if o.comment else [])
    pay = t(lang, "pay_transfer" if o.payment_method == "transfer" else "pay_cash")
    if o.change_from:
        pay += " · " + t(lang, "change", amount=money(cafe, o.change_from, lang))
    extra.append(pay)
    return "\n".join(lines) + "\n\n" + "\n".join(extra)
