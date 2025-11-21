import logging
import os
from datetime import datetime
from typing import Dict, Tuple

import requests
import xml.etree.ElementTree as ET
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:
    load_dotenv = None

# ====== НАСТРОЙКИ ======
CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"  # официальный XML-эндпоинт ЦБ РФ
# Пример и описание: XML_daily.asp без параметров возвращает котировки на последнюю дату. :contentReference[oaicite:0]{index=0}

# Кеш курсов, чтобы не долбить ЦБ на каждый символ
_rates_cache: Dict[str, float] | None = None
_names_cache: Dict[str, str] | None = None
_date_cache: datetime | None = None

# Время жизни кеша в секундах (10 минут)
CACHE_TTL_SECONDS = 600
_last_fetch_ts: float | None = None

# ====== ЛОГИРОВАНИЕ ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ====== РАБОТА С КУРСАМИ ЦБ ======
def fetch_cbr_rates(force: bool = False) -> Tuple[Dict[str, float], Dict[str, str], datetime]:
    """
    Получаем курсы валют у ЦБ РФ и возвращаем:
    - rates: словарь { 'USD': руб_за_1_единицу, ... }
    - names: словарь { 'USD': 'Доллар США', ... }
    - date: дата котировок
    """
    import time

    global _rates_cache, _names_cache, _date_cache, _last_fetch_ts

    now_ts = time.time()
    if (
        not force
        and _rates_cache is not None
        and _names_cache is not None
        and _date_cache is not None
        and _last_fetch_ts is not None
        and (now_ts - _last_fetch_ts) < CACHE_TTL_SECONDS
    ):
        return _rates_cache, _names_cache, _date_cache

    logger.info("Fetching rates from CBR...")
    resp = requests.get(CBR_DAILY_URL, timeout=10)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)

    rates: Dict[str, float] = {"RUB": 1.0}
    names: Dict[str, str] = {"RUB": "Российский рубль"}

    # Дата в атрибуте Date, формат: "02.03.2002"
    date_str = root.get("Date")
    if date_str:
        cbr_date = datetime.strptime(date_str, "%d.%m.%Y")
    else:
        cbr_date = datetime.now()

    for valute in root.findall("Valute"):
        char_code = valute.findtext("CharCode", "").upper()
        nominal_str = valute.findtext("Nominal", "1")
        value_str = valute.findtext("Value", "0")

        if not char_code:
            continue

        try:
            nominal = int(nominal_str)
            # В XML используется запятая как разделитель дробной части
            value = float(value_str.replace(",", "."))
            rub_per_unit = value / nominal
        except ValueError:
            logger.warning("Skip valute %s: bad data", char_code)
            continue

        rates[char_code] = rub_per_unit
        names[char_code] = valute.findtext("Name", char_code)

    _rates_cache, _names_cache, _date_cache = rates, names, cbr_date
    _last_fetch_ts = now_ts

    return rates, names, cbr_date


def normalize_text(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def detect_currency_code(
    raw_currency: str,
    rates: Dict[str, float],
    names: Dict[str, str],
) -> str | None:
    """
    Пытаемся понять, какую валюту имел в виду пользователь.
    Поддержка:
    - RUB/RUR/руб/россия/рф
    - USD/доллар/доллары/сша/america/usa
    - KZT/тенге/казахстан
    - THB/бат/тайланд
    - ISO-код (gbp, cny и т.п.)
    - Часть официального названия валюты из XML ЦБ (например, 'юань')
    """
    aliases = {
        # Рубль
        "rub": "RUB",
        "rur": "RUB",
        "руб": "RUB",
        "рубль": "RUB",
        "рубли": "RUB",
        "рублей": "RUB",
        "россия": "RUB",
        "рф": "RUB",
        "russia": "RUB",

        # Доллар США
        "usd": "USD",
        "доллар": "USD",
        "доллары": "USD",
        "долларов": "USD",
        "бакс": "USD",
        "баксы": "USD",
        "сша": "USD",
        "usa": "USD",
        "america": "USD",
        "америка": "USD",

        # Казахский тенге
        "kzt": "KZT",
        "тенге": "KZT",
        "казахстан": "KZT",
        "казахстанский": "KZT",
        "казахстана": "KZT",

        # Тайский бат
        "thb": "THB",
        "бат": "THB",
        "баты": "THB",
        "батов": "THB",
        "тайланд": "THB",
        "тайский": "THB",
        "thailand": "THB",
    }

    normalized = normalize_text(raw_currency)
    tokens = normalized.replace(",", " ").replace(".", " ").split()

    # 1. Прямое совпадение по алиасам
    for token in tokens:
        if token in aliases:
            return aliases[token]

    # 2. ISO-код (например "usd", "eur", "gbp") — если есть в списке курсов ЦБ
    for token in tokens:
        code = token.upper()
        if code in rates:
            return code

    # 3. Поиск по части официального названия из XML ЦБ
    for code, name in names.items():
        name_norm = normalize_text(name)
        if normalized in name_norm:
            return code

    return None


def parse_amount_and_currency(text: str) -> Tuple[float | None, str | None]:
    """
    Ожидаем формат наподобие:
    "100 usd"
    "2500 руб"
    "100 kzt"
    Возвращаем (amount, raw_currency_str).
    """
    cleaned = text.replace(",", ".").strip()
    parts = cleaned.split()

    if not parts:
        return None, None

    try:
        amount = float(parts[0])
    except ValueError:
        return None, None

    if len(parts) == 1:
        return amount, None

    currency_raw = " ".join(parts[1:])
    return amount, currency_raw


def format_amount(value: float, digits: int = 2) -> str:
    """
    Красивое форматирование числа:
    12345.678 -> "12 345.68"
    """
    s = f"{value:,.{digits}f}"
    return s.replace(",", " ")


# ====== ХЕНДЛЕРЫ БОТА ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! 👋\n\n"
        "Я бот-конвертер валют по официальному курсу ЦБ РФ.\n\n"
        "Отправь мне сумму и валюту — я переведу её в рубли, доллары, тенге и баты по "
        "актуальному курсу на момент запроса.\n\n"
        "Примеры:\n"
        "• <code>100 usd</code>\n"
        "• <code>2500 руб</code>\n"
        "• <code>100 kzt</code>\n"
        "• <code>100 thb</code>\n"
        "• <code>100 доллар сша</code>\n\n"
        "Курсы берутся с сайта Банка России."
    )
    await update.message.reply_html(text)


async def handle_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()

    amount, raw_currency = parse_amount_and_currency(user_text)
    if amount is None:
        await update.message.reply_text(
            "Не смог распознать сумму. Отправь, пожалуйста, в формате:\n"
            "<число> <валюта>\n\nНапример: 100 usd"
        )
        return

    if raw_currency is None:
        await update.message.reply_text(
            "Не нашёл название валюты. Напиши, пожалуйста, в формате:\n"
            "<число> <валюта>\n\nНапример: 100 usd, 2500 руб, 100 kzt"
        )
        return

    try:
        rates, names, cbr_date = fetch_cbr_rates()
    except Exception as e:
        logger.exception("Error while fetching CBR rates")
        await update.message.reply_text(
            "Не удалось получить курсы валют ЦБ РФ. Попробуй ещё раз чуть позже."
        )
        return

    currency_code = detect_currency_code(raw_currency, rates, names)
    if currency_code is None:
        await update.message.reply_text(
            f"Не понимаю валюту «{raw_currency}» 🤔\n"
            "Попробуй указать ISO-код (например, USD, KZT, THB) "
            "или написать: рубль, доллар, тенге, бат и т.п."
        )
        return

    if currency_code not in rates:
        await update.message.reply_text(
            f"Валюта {currency_code} не найдена в списке курсов ЦБ РФ."
        )
        return

    # Курс рубля к 1 единице исходной валюты
    rub_per_unit = rates[currency_code]

    amount_in_rub = amount * rub_per_unit
    # Курсы рубля к доллару, тенге и батам
    usd_rate = rates.get("USD")
    kzt_rate = rates.get("KZT")
    thb_rate = rates.get("THB")

    if usd_rate is None or kzt_rate is None or thb_rate is None:
        await update.message.reply_text(
            "Не удалось получить курс доллара, тенге или батов от ЦБ РФ."
        )
        return

    amount_in_usd = amount_in_rub / usd_rate
    amount_in_kzt = amount_in_rub / kzt_rate
    amount_in_thb = amount_in_rub / thb_rate

    reply_lines = [
        f"Курс ЦБ РФ на {cbr_date.strftime('%d.%m.%Y')}:",
        "",
        f"{format_amount(amount)} {currency_code} =",
        f"• {format_amount(amount_in_rub)} RUB",
        f"• {format_amount(amount_in_usd)} USD",
        f"• {format_amount(amount_in_kzt)} KZT",
        f"• {format_amount(amount_in_thb)} THB",
    ]

    await update.message.reply_text("\n".join(reply_lines))


# ====== ТОЧКА ВХОДА ======
def main() -> None:
    """
    Точка входа. Запускает бота в режиме long polling.
    Документация по ApplicationBuilder / run_polling в python-telegram-bot. :contentReference[oaicite:1]{index=1}
    """
    # Поддержка .env (если установлен python-dotenv)
    if 'load_dotenv' in globals() and load_dotenv is not None:
        load_dotenv()

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задан токен бота. Установи переменную окружения TELEGRAM_TOKEN."
        )

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_convert))

    logger.info("Bot started. Waiting for updates...")
    application.run_polling()


if __name__ == "__main__":
    main()
