import logging
import os
import re
import sqlite3
import time
import traceback
import unicodedata
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


SERPAPI_URL = "https://serpapi.com/search.json"
CHECK_INTERVAL_SECONDS = 12 * 60 * 60
SEARCH_RESULT_TTL_SECONDS = 30 * 60
STORES = {
    "Amazon.es": ("amazon.es", "amazon"),
    "PcComponentes": ("pccomponentes",),
    "MediaMarkt": ("mediamarkt",),
    "FNAC": ("fnac.es", "fnac"),
}
DATABASE_PATH = os.getenv("DATABASE_PATH", "pricebot.sqlite3")
logger = logging.getLogger("pricebot")


class SerpApiError(Exception):
    pass


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracked_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                store TEXT NOT NULL,
                product_id TEXT NOT NULL,
                product_link TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, store, product_id)
            );
            CREATE TABLE IF NOT EXISTS search_results (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                store TEXT NOT NULL,
                product_id TEXT NOT NULL,
                product_link TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            """
        )


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def matching_store(source: str) -> str | None:
    normalized = normalize_text(source)
    for store, aliases in STORES.items():
        if any(normalize_text(alias) in normalized for alias in aliases):
            return store
    return None


def price_to_cents(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        cleaned = str(value)
    else:
        cleaned = re.sub(r"[^\d,.\-]", "", str(value))
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
    try:
        price = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0:
        return None
    return int(price * 100)


def format_price(price_cents: int) -> str:
    euros, cents = divmod(price_cents, 100)
    return f"{euros:,}".replace(",", ".") + f",{cents:02d} €"


def shopping_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    seen = set()
    results = data.get("shopping_results", [])
    if not isinstance(results, list):
        return candidates
    for result in results:
        if not isinstance(result, dict):
            continue
        store = matching_store(str(result.get("source", "")))
        product_id = result.get("product_id")
        title = result.get("title")
        link = result.get("product_link")
        price_cents = price_to_cents(result.get("extracted_price"))
        if not store or not product_id or not title or not link or price_cents is None:
            continue
        key = (store, str(product_id))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "store": store,
                "product_id": str(product_id),
                "title": str(title),
                "link": str(link),
                "price_cents": price_cents,
            }
        )
    return candidates[:10]


def seller_offer(data: dict[str, Any], store: str) -> tuple[int, str] | None:
    seller_results = data.get("sellers_results") or {}
    if not isinstance(seller_results, dict):
        return None
    sellers = seller_results.get("online_sellers") or []
    if not isinstance(sellers, list):
        return None
    for seller in sellers:
        if not isinstance(seller, dict):
            continue
        if matching_store(str(seller.get("name", ""))) != store:
            continue
        for field in ("extracted_price", "base_price", "total_price", "price"):
            price_cents = price_to_cents(seller.get(field))
            if price_cents is not None:
                link = seller.get("link") or seller.get("direct_link") or ""
                return price_cents, str(link)
    return None


async def serpapi_search(
    client: httpx.AsyncClient, api_key: str, params: dict[str, str]
) -> dict[str, Any]:
    response = await client.get(
        SERPAPI_URL, params={**params, "api_key": api_key}, timeout=30
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as error:
        raise SerpApiError("SerpApi devolvió una respuesta que no es JSON.") from error
    if not isinstance(data, dict):
        raise SerpApiError("SerpApi devolvió una respuesta inesperada.")
    if data.get("error"):
        raise SerpApiError(str(data["error"]))
    return data


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Hola. Puedo buscar precios en Amazon.es, PcComponentes, MediaMarkt y FNAC "
        "y avisarte cuando cambien.\n\n"
        "Usa /buscar nombre del producto para empezar, /seguimiento para ver "
        "tus productos o /ayuda para ver los comandos."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Comandos disponibles:\n"
        "/buscar <producto> — busca opciones en las tiendas compatibles.\n"
        "/seguimiento — muestra los productos que estás vigilando.\n"
        "/quitar <id> — deja de vigilar un producto (el ID aparece en /seguimiento).\n\n"
        "Los precios se comprueban cada 12 horas. Solo recibirás avisos si cambian."
    )


async def search_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Uso: /buscar nombre del producto")
        return

    api_key = os.environ["SERPAPI_API_KEY"]
    await update.effective_message.reply_text(f"Buscando «{query}»…")
    try:
        async with httpx.AsyncClient() as client:
            data = await serpapi_search(
                client,
                api_key,
                {
                    "engine": "google_shopping",
                    "q": query,
                    "google_domain": "google.es",
                    "gl": "es",
                    "hl": "es",
                    "location": "Madrid,Community of Madrid,Spain",
                },
            )
    except (httpx.HTTPError, SerpApiError) as error:
        logger.warning("Product search failed (%s)", type(error).__name__)
        await update.effective_message.reply_text(
            "No se pudo completar la búsqueda. Comprueba la clave y el estado "
            "de SerpApi e inténtalo de nuevo."
        )
        return

    candidates = shopping_candidates(data)
    if not candidates:
        await update.effective_message.reply_text(
            "No encontré resultados con precio en esas tiendas. Prueba con otro nombre."
        )
        return

    keyboard = []
    chat_id = update.effective_chat.id
    with database() as connection:
        connection.execute(
            "DELETE FROM search_results WHERE expires_at < ?", (int(time.time()),)
        )
        for candidate in candidates:
            result_id = uuid.uuid4().hex[:12]
            connection.execute(
                """
                INSERT INTO search_results
                    (id, chat_id, title, store, product_id, product_link,
                     price_cents, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    chat_id,
                    candidate["title"],
                    candidate["store"],
                    candidate["product_id"],
                    candidate["link"],
                    candidate["price_cents"],
                    int(time.time()) + SEARCH_RESULT_TTL_SECONDS,
                ),
            )
            label = f"Seguir {candidate['store']}: {format_price(candidate['price_cents'])}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        label[:64], callback_data=f"track:{result_id}"
                    ),
                    InlineKeyboardButton("Ver", url=candidate["link"]),
                ]
            )

    await update.effective_message.reply_text(
        "Elige qué resultado quieres vigilar:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def track_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback = update.callback_query
    await callback.answer()
    result_id = callback.data.split(":", 1)[1]
    with database() as connection:
        result = connection.execute(
            "SELECT * FROM search_results WHERE id = ? AND chat_id = ?",
            (result_id, update.effective_chat.id),
        ).fetchone()
        if not result or result["expires_at"] < int(time.time()):
            await callback.message.reply_text(
                "Este resultado ha caducado. Vuelve a buscar el producto."
            )
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO tracked_products
                (chat_id, title, store, product_id, product_link, price_cents)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                result["chat_id"],
                result["title"],
                result["store"],
                result["product_id"],
                result["product_link"],
                result["price_cents"],
            ),
        )
        tracked = connection.execute(
            """
            SELECT id FROM tracked_products
            WHERE chat_id = ? AND store = ? AND product_id = ?
            """,
            (result["chat_id"], result["store"], result["product_id"]),
        ).fetchone()
    await callback.message.reply_text(
        f"Siguiendo #{tracked['id']}: {result['title']} — {result['store']} "
        f"({format_price(result['price_cents'])})."
    )


async def list_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with database() as connection:
        products = connection.execute(
            """
            SELECT id, title, store, price_cents
            FROM tracked_products WHERE chat_id = ? ORDER BY id
            """,
            (update.effective_chat.id,),
        ).fetchall()
    if not products:
        await update.effective_message.reply_text(
            "Aún no sigues ningún producto. Usa /buscar nombre del producto."
        )
        return
    lines = [
        f"#{product['id']} — {product['title']} ({product['store']}): "
        f"{format_price(product['price_cents'])}"
        for product in products
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def remove_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Uso: /quitar <id>")
        return
    with database() as connection:
        cursor = connection.execute(
            "DELETE FROM tracked_products WHERE id = ? AND chat_id = ?",
            (int(context.args[0]), update.effective_chat.id),
        )
    if cursor.rowcount:
        await update.effective_message.reply_text("Producto eliminado del seguimiento.")
    else:
        await update.effective_message.reply_text(
            "No encontré ese ID entre tus productos."
        )


async def remember_error(
    product_id: int, error: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    with database() as connection:
        row = connection.execute(
            "SELECT last_error FROM tracked_products WHERE id = ?", (product_id,)
        ).fetchone()
        connection.execute(
            "UPDATE tracked_products SET last_error = ? WHERE id = ?",
            (error, product_id),
        )
    if row and row["last_error"] != error:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"No pude comprobar un precio: {error}. Lo intentaré en la "
            "próxima revisión.",
        )


async def check_tracked_product(
    product: sqlite3.Row,
    client: httpx.AsyncClient,
    api_key: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        data = await serpapi_search(
            client,
            api_key,
            {
                "engine": "google_product",
                "product_id": product["product_id"],
                "gl": "es",
                "hl": "es",
            },
        )
    except (httpx.HTTPError, SerpApiError) as error:
        logger.warning(
            "Price check failed for tracked product %s (%s)",
            product["id"],
            type(error).__name__,
        )
        await remember_error(
            product["id"], "falló la consulta a SerpApi", product["chat_id"], context
        )
        return

    offer = seller_offer(data, product["store"])
    if offer is None:
        await remember_error(
            product["id"],
            f"la tienda {product['store']} no aparece en las ofertas actuales",
            product["chat_id"],
            context,
        )
        return

    price_cents, link = offer
    with database() as connection:
        current = connection.execute(
            "SELECT price_cents FROM tracked_products WHERE id = ?",
            (product["id"],),
        ).fetchone()
        if not current:
            return
        previous_price = current["price_cents"]
        connection.execute(
            """
            UPDATE tracked_products
            SET price_cents = ?, product_link = ?, last_error = NULL
            WHERE id = ?
            """,
            (price_cents, link or product["product_link"], product["id"]),
        )
    if price_cents != previous_price:
        direction = "bajó" if price_cents < previous_price else "subió"
        await context.bot.send_message(
            chat_id=product["chat_id"],
            text=(
                f"Cambió el precio de {product['title']} en {product['store']}: "
                f"{format_price(previous_price)} → {format_price(price_cents)} "
                f"(el precio {direction}).\n{link or product['product_link']}"
            ),
        )


async def poll_prices(context: ContextTypes.DEFAULT_TYPE) -> None:
    api_key = os.environ["SERPAPI_API_KEY"]
    with database() as connection:
        products = connection.execute(
            "SELECT * FROM tracked_products ORDER BY id"
        ).fetchall()
    if not products:
        return
    async with httpx.AsyncClient() as client:
        for product in products:
            await check_tracked_product(product, client, api_key, context)


async def post_init(application: Application) -> None:
    application.job_queue.run_repeating(
        poll_prices,
        interval=CHECK_INTERVAL_SECONDS,
        first=CHECK_INTERVAL_SECONDS,
        name="price-checks",
    )


async def handle_application_error(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    error = context.error
    if error is None:
        logger.error("Telegram application reported an error without exception details.")
        return

    details = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    for secret in (
        os.getenv("TELEGRAM_BOT_TOKEN", ""),
        os.getenv("SERPAPI_API_KEY", ""),
    ):
        if secret:
            details = details.replace(secret, "[REDACTED]")
    logger.error("Unhandled exception in Telegram application:\n%s", details)


def main() -> None:
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    serpapi_key = os.environ.get("SERPAPI_API_KEY")
    if not telegram_token:
        raise RuntimeError("Falta configurar TELEGRAM_BOT_TOKEN.")
    if not serpapi_key:
        raise RuntimeError("Falta configurar SERPAPI_API_KEY.")

    initialize_database()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = (
        Application.builder()
        .token(telegram_token)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ayuda", help_command))
    application.add_handler(CommandHandler("buscar", search_products))
    application.add_handler(CommandHandler("seguimiento", list_tracking))
    application.add_handler(CommandHandler("quitar", remove_tracking))
    application.add_handler(CallbackQueryHandler(track_result, pattern=r"^track:"))
    application.add_error_handler(handle_application_error)
    application.run_polling()


if __name__ == "__main__":
    main()
