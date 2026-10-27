"""Carga products.json en memoria y ofrece una búsqueda simple por texto.

Los nombres, códigos, categorías, precios, colores, imágenes y links vienen
del catálogo cacheado (products.json, generado por scraper.py).

El catálogo se actualiza solo: start_auto_refresh() vuelve a scrapear el
sitio cada N horas en segundo plano, y cada búsqueda recarga products.json
si el archivo cambió (sirve también si corrés scraper.py a mano, y para que
todos los workers de gunicorn vean la versión nueva).
"""

import json
import logging
import os
import random
import threading
import time
import unicodedata

import scraper

log = logging.getLogger(__name__)

_PRODUCTS_PATH = scraper.OUTPUT_FILE

# Si un scrapeo trae menos de esta fracción del catálogo actual, se descarta:
# probablemente el sitio falló o cambió su estructura, y es mejor seguir con
# la lista anterior que dejar al bot sin productos.
_MIN_REFRESH_RATIO = 0.5


def _normalize(text: str) -> str:
    """Minúsculas y sin acentos ("RIÑONERA" -> "rinonera")."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _variants(term: str) -> list[str]:
    """El término y su posible singular ("mochilas" -> "mochila", "morrales" -> "morral")."""
    variants = [term]
    if term.endswith("es") and len(term) > 5:
        variants.append(term[:-2])
    if term.endswith("s") and len(term) > 4:
        variants.append(term[:-1])
    return variants


def _load() -> tuple[float, list[dict], list[str], dict[str, dict]]:
    mtime = os.path.getmtime(_PRODUCTS_PATH)
    with open(_PRODUCTS_PATH, encoding="utf-8") as f:
        products = json.load(f)
    # Texto de búsqueda precalculado por producto
    haystacks = [
        _normalize(f"{p['name']} {p['category']} {p.get('code', '')} {' '.join(p.get('colors', []))}")
        for p in products
    ]
    by_url = {p["url"]: p for p in products if p.get("url")}
    return mtime, products, haystacks, by_url


# Se reemplaza la tupla entera de una vez, así una búsqueda nunca ve un
# catálogo a medio cargar.
_catalog = _load()
_reload_lock = threading.Lock()


def _reload_if_changed() -> None:
    global _catalog
    try:
        mtime = os.path.getmtime(_PRODUCTS_PATH)
    except OSError:
        return
    if mtime == _catalog[0]:
        return
    with _reload_lock:
        if mtime == _catalog[0]:
            return
        try:
            _catalog = _load()
            log.info("Catálogo recargado: %d productos", len(_catalog[1]))
        except (OSError, ValueError):
            log.exception("No se pudo recargar products.json; sigo con el catálogo anterior")


def search_products(query: str = "", limit: int = 10) -> list[dict]:
    """Busca productos cuyo nombre/categoría/código/colores contengan todas las palabras de la búsqueda.

    Tolera plurales y acentos. Los resultados se ordenan por cantidad de
    coincidencias.
    """
    _reload_if_changed()
    _, products, haystacks, _ = _catalog

    terms = [_variants(t) for t in _normalize(query).split()]
    if not terms:
        return []

    scored = []
    for product, haystack in zip(products, haystacks):
        score = 0
        for variants in terms:
            hits = max(haystack.count(v) for v in variants)
            if not hits:
                break
            score += hits
        else:
            scored.append((score, product))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [product for _, product in scored[:limit]]


def get_product_by_url(url: str) -> dict | None:
    """Devuelve el producto cuyo link es exactamente url (o None)."""
    _reload_if_changed()
    return _catalog[3].get(url)


def list_categories() -> list[str]:
    _reload_if_changed()
    return sorted({p["category"] for p in _catalog[1]})


def refresh_catalog() -> None:
    """Vuelve a scrapear el sitio y, si el resultado es razonable, reemplaza products.json."""
    log.info("Actualizando catálogo desde lsydstore.com.ar...")
    # Con el catálogo actual solo se leen las fichas de los productos nuevos
    products = scraper.scrape_catalog(log=log.debug, previous=_catalog[1])

    current = len(_catalog[1])
    if len(products) < current * _MIN_REFRESH_RATIO:
        raise RuntimeError(
            f"El scrapeo trajo {len(products)} productos contra {current} actuales; "
            "lo descarto por las dudas"
        )

    scraper.save_products(products, _PRODUCTS_PATH)
    _reload_if_changed()
    log.info("Catálogo actualizado: %d productos", len(products))


def _auto_refresh_loop(interval_seconds: float) -> None:
    while True:
        # Espera hasta que el archivo cumpla la antigüedad pedida. El margen
        # al azar evita que varios workers de gunicorn scrapeen a la vez: el
        # primero que termina actualiza el archivo y los demás, al despertar,
        # ven que ya es reciente y siguen esperando.
        age = time.time() - os.path.getmtime(_PRODUCTS_PATH)
        wait = max(0.0, interval_seconds - age) + random.uniform(0, 600)
        time.sleep(wait)

        if time.time() - os.path.getmtime(_PRODUCTS_PATH) < interval_seconds:
            _reload_if_changed()
            continue
        try:
            refresh_catalog()
        except Exception:
            log.exception("Falló la actualización del catálogo; reintento en 1 hora")
            time.sleep(3600)


def start_auto_refresh(hours: float) -> None:
    """Arranca la actualización periódica del catálogo en segundo plano (0 = desactivada)."""
    if hours <= 0:
        return
    thread = threading.Thread(
        target=_auto_refresh_loop, args=(hours * 3600,), name="catalog-refresh", daemon=True
    )
    thread.start()
    log.info("Actualización automática del catálogo cada %g horas", hours)
