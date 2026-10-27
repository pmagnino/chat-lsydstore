"""
Scraper del catálogo de productos de lsydstore.com.ar.

Lee las categorías del menú del sitio (cada una con sus subcategorías),
recorre cada subcategoría (con paginación ?page=N) y extrae nombre, código,
categoría, precio, colores, imagen y URL de cada producto. Al final recorre
también la categoría completa ("Ver todos") para sumar los productos que no
estén en ninguna subcategoría. Después entra a la página de cada producto
nuevo para leer medidas y peso. Guarda todo en products.json para que
app.py lo use como base de conocimiento del chatbot.

LSyD Store es minorista y publica los precios (con IVA incluido). Los
precios salen de los listados, así que se actualizan en cada scrapeo.

Uso:
    python scraper.py
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.lsydstore.com.ar"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LSyDCatalogBot/1.0)"}
REQUEST_DELAY = 0.3  # segundos entre pedidos, para no sobrecargar el sitio
MAX_PAGES_PER_CATEGORY = 60  # límite de seguridad para la paginación
DETAIL_WORKERS = 4  # fichas de producto que se leen a la vez
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "products.json")


def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.content, "html.parser", from_encoding="utf-8")


def absolute(url: str) -> str:
    return BASE_URL + url if url.startswith("/") else url


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def discover_categories(session: requests.Session) -> list[dict]:
    """Arma la lista de categorías a recorrer a partir del menú de la home.

    Cada categoría principal (div.mob_cat_row) tiene sus subcategorías en
    div#mob_childs_ID; la última es "Ver todos", con el link a la categoría
    completa. Se devuelven primero las subcategorías (para que cada producto
    quede con la más específica) y después las categorías completas.
    """
    soup = get_soup(session, BASE_URL + "/")

    subcategories, parents = [], []
    seen_urls = set()
    for row in soup.select("div.mob_cat_row[id^=mob_cat_]"):
        cat_id = row["id"].removeprefix("mob_cat_")
        label_el = row.find("span")
        childs = soup.find(id=f"mob_childs_{cat_id}")
        if not label_el or not childs:
            continue
        label = _clean(label_el.get_text())

        for a in childs.select("a[href^='/ecommerce/']"):
            url = absolute(a["href"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if a.find_parent(class_="mob_cat_see_all"):
                parents.append({"label": label, "url": url})
            else:
                sub = _clean(a.get_text())
                subcategories.append({"label": f"{label} > {sub}", "url": url})

    return subcategories + parents


def _price(card, selector: str) -> str | None:
    el = card.select_one(selector)
    if not el:
        return None
    text = _clean(el.get_text())
    return text if re.fullmatch(r"\$\s?[\d.]+(,\d+)?", text) else None


def parse_card(card) -> dict | None:
    """Datos de una tarjeta de producto (div.producto) de un listado."""
    a = card.find("a", href=True)
    name_el = card.select_one(".nombre_producto")
    if not a or not name_el:
        return None
    match = re.search(r"-(\d+)/?$", a["href"])
    if not match:
        return None

    code = card.select_one(".codigo_producto")
    code = _clean(code.get_text()).removeprefix("Cód.").strip() if code else ""
    img = card.select_one(".img_producto img[src]")
    image = img["src"].strip() if img else ""
    if "img_no_disponible" in image:
        image = ""

    product = {
        "id": match.group(1),
        "code": code,
        "name": _clean(name_el.get_text()),
        "url": absolute(a["href"]),
        "image": absolute(image) if image else None,
    }

    # Precio actual; si está en oferta, también el de lista y el descuento
    price = _price(card, ".price_new_prod")
    if price:
        product["price"] = price
        list_price = _price(card, ".price_old_prod")
        if list_price and list_price != price:
            product["list_price"] = list_price
            discount = card.select_one(".precio_old_detalle")
            if discount:
                product["discount"] = _clean(discount.get_text())

    colors = card.select_one(".color_options")
    if colors:
        product["colors"] = [c.strip() for c in colors.get_text().split("|") if c.strip()]
    return product


def scrape_category(session: requests.Session, category: dict, seen_ids: set) -> list[dict]:
    products = []

    for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
        url = category["url"] if page == 1 else f"{category['url']}?page={page}"
        try:
            soup = get_soup(session, url)
        except requests.RequestException as e:
            print(f"  ! error en {url}: {e}")
            break

        cards = soup.select("#contentProducts div.producto") or soup.select("div.producto")
        if not cards:
            break

        for card in cards:
            product = parse_card(card)
            if not product or product["id"] in seen_ids:
                continue
            seen_ids.add(product["id"])
            product["category"] = category["label"]
            products.append(product)

        time.sleep(REQUEST_DELAY)

    return products


def _number(value: str) -> str:
    """ "19.5" -> "19,5", "13.00" -> "13" (formato argentino, sin ceros de más)."""
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.replace(".", ",")


def scrape_details(session: requests.Session, product: dict) -> dict:
    """Lee de la página del producto las "Características" (medidas, peso...).

    Devuelve un dict (vacío si la página no trae esos datos); lanza
    requests.RequestException si no se pudo leer la página.
    """
    soup = get_soup(session, product["url"])
    details = {}
    for block in soup.select("div.wrapper_category_detalle"):
        title = block.select_one(".title_category_detalle")
        text = block.select_one(".text_category_detalle")
        if not title or not text:
            continue
        title = _clean(title.get_text()).lower()
        value = _clean(text.get_text(" "))

        if title == "medidas":
            # "38 cm de alto x 46 cm de ancho x 19.5 cm de prof"
            nums = re.findall(r"([\d.,]+)\s*cm de (alto|ancho|prof)", value)
            if nums and any(float(n.replace(",", ".")) for n, _ in nums):
                names = {"alto": "alto", "ancho": "ancho", "prof": "profundidad"}
                details["medidas"] = " x ".join(f"{_number(n)} cm de {names[k]}" for n, k in nums)
        elif title == "peso":
            match = re.search(r"([\d.,]+)\s*kg", value, re.IGNORECASE)
            if match and match.group(1).strip("0,.") != "":
                details["peso"] = f"{_number(match.group(1))} kg"
        elif value:
            details[title] = value[:500]
    return details


def add_details(session: requests.Session, products: list[dict], previous: list[dict], log=print) -> None:
    """Agrega "details" a cada producto.

    Reutiliza lo ya leído en el catálogo anterior y solo visita la página de
    los productos nuevos (o los que fallaron la vez pasada), así cada
    actualización no recorre todas las fichas.
    """
    known = {p["id"]: p["details"] for p in previous if "details" in p}
    pending = []
    for product in products:
        if product["id"] in known:
            product["details"] = known[product["id"]]
        else:
            pending.append(product)

    log(f"Leyendo fichas de {len(pending)} productos nuevos...")

    def fetch(product: dict) -> bool:
        try:
            product["details"] = scrape_details(session, product)
            return True
        except requests.RequestException as e:
            # Sin "details" se reintenta en la próxima actualización
            log(f"  ! error en {product['url']}: {e}")
            return False
        finally:
            time.sleep(REQUEST_DELAY)

    # Cada ficha tarda 1-2 segundos en bajar: se leen de a DETAIL_WORKERS a la vez
    failed = 0
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for i, ok in enumerate(pool.map(fetch, pending), 1):
            failed += not ok
            if i % 100 == 0:
                log(f"  {i}/{len(pending)}")
    if failed:
        log(f"  {failed} fichas no se pudieron leer; se reintentan la próxima vez")


def scrape_catalog(log=print, previous: list[dict] | None = None, on_listed=None) -> list[dict]:
    """Recorre todo el sitio y devuelve la lista de productos.

    previous es el catálogo anterior: de ahí se reutilizan las fichas ya leídas.
    on_listed, si se pasa, recibe la lista apenas se leyeron los listados
    (antes de entrar a las fichas, que es lo que más tarda).
    """
    session = requests.Session()

    log("Leyendo categorías...")
    categories = discover_categories(session)
    log(f"  {len(categories)} categorías y subcategorías encontradas")
    if not categories:
        raise RuntimeError("No se encontraron categorías: ¿cambió la estructura del sitio?")

    all_products = []
    seen_ids: set[str] = set()

    for i, category in enumerate(categories, 1):
        log(f"[{i}/{len(categories)}] {category['label']}")
        products = scrape_category(session, category, seen_ids)
        log(f"  {len(products)} productos nuevos")
        all_products.extend(products)
        time.sleep(REQUEST_DELAY)

    if on_listed:
        on_listed(all_products)
    add_details(session, all_products, previous or [], log=log)
    return all_products


def save_products(products: list[dict], path: str = OUTPUT_FILE) -> None:
    # Se escribe a un archivo temporal y después se reemplaza, para que un
    # error a mitad de camino no deje un products.json roto. El pid en el
    # nombre evita choques si dos procesos guardan a la vez.
    tmp_file = f"{path}.{os.getpid()}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, path)


def main():
    try:
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = []
    def save_listed(products):
        # Sin catálogo previo se guarda ya, así el bot se puede usar mientras
        # se leen las fichas (las medidas y el peso aparecen al terminar)
        if not previous:
            save_products(products)
            print(f"Listados guardados: {len(products)} productos (faltan medidas y peso)")

    products = scrape_catalog(previous=previous, on_listed=save_listed)
    save_products(products)
    print(f"\nListo: {len(products)} productos guardados en {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
