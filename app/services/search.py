"""Product search (Part I §3.1, §15; Part II §7.4).

§15 asks for typo-tolerant autocomplete with Arabic/English normalisation. Part
II §7.4 recommends Meilisearch or Typesense at catalog scale, because plain SQL
``LIKE`` cannot do either.

Both live behind one :class:`SearchBackend` protocol, so the storefront never
knows which is running and switching is a config change:

* :class:`SqlBackend` — the default. Queries the precomputed
  ``search_text_ar`` / ``search_text_en`` projection, so Arabic normalisation
  applies to *stored* text as well as to the query, then re-ranks a bounded
  candidate set in Python for typo tolerance. Good to a few thousand products.
* :class:`MeilisearchBackend` — the production path once the catalog outgrows
  that, configured with Arabic-aware settings.

The normalisation itself lives in ``services/search_text.py``; the reason it
matters is spelled out there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import clean_text
from app.models.catalog import Product, ProductTag, Publisher, Tag
from app.services import search_text
from app.services.catalog import (
    MAX_PAGE_SIZE,
    apply_sort,
    normalize_page_size,
    normalize_sort,
    visible_products_stmt,
)

log = get_logger(__name__)

#: How many rows the SQL backend re-ranks in Python. Large enough that ranking
#: is meaningful, small enough that a pathological query cannot stall a request.
CANDIDATE_LIMIT = 400

#: Below this many exact-ish hits, widen the search so typos can be caught.
FUZZY_FALLBACK_THRESHOLD = 5

#: Ceiling on the widened fuzzy sweep. This is the honest limit of the SQL
#: backend: past a few thousand products, move to Meilisearch (Part II §7.4),
#: which does typo tolerance in the engine rather than in Python.
FUZZY_SCAN_LIMIT = 1000


@dataclass(slots=True)
class SearchResult:
    products: list[Product]
    total: int
    q: str
    page: int
    page_size: int
    sort: str


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def build_index_text(db: Session, product: Product) -> tuple[str, str]:
    """The normalised searchable blobs for one product, per language.

    Includes everything a shopper might reasonably type: the name, the short
    description, the ISBN, the publisher and the product's tags.
    """
    publisher = product.publisher
    tags = db.scalars(
        select(Tag)
        .join(ProductTag, ProductTag.fk_tag_id == Tag.pk_tag_id)
        .where(
            ProductTag.fk_product_id == product.pk_product_id,
            ProductTag.scd_active_flag.is_(True),
            Tag.scd_active_flag.is_(True),
        )
    ).all()

    arabic = search_text.index_text(
        product.name_ar,
        product.short_description_ar,
        product.isbn,
        publisher.name_ar if publisher else None,
        *[tag.name_ar for tag in tags],
    )
    english = search_text.index_text(
        product.name_en,
        product.short_description_en,
        product.isbn,
        publisher.name_en if publisher else None,
        *[tag.name_en for tag in tags],
    )
    return arabic, english


def reindex_product(db: Session, product: Product) -> Product:
    """Refresh one product's search projection. Call on every catalog write."""
    product.search_text_ar, product.search_text_en = build_index_text(db, product)
    return product


def reindex_all(db: Session, *, batch_size: int = 500) -> int:
    """Rebuild the whole index. Safe to run repeatedly."""
    total = 0
    offset = 0

    while True:
        products = list(
            db.scalars(
                select(Product)
                .where(Product.scd_active_flag.is_(True))
                .order_by(Product.pk_product_id)
                .offset(offset)
                .limit(batch_size)
            ).all()
        )
        if not products:
            break

        for product in products:
            reindex_product(db, product)
        db.flush()

        total += len(products)
        offset += batch_size

    log.info("search_reindexed", extra={"products": total})
    return total


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class SearchBackend(Protocol):
    def search(
        self,
        db: Session,
        *,
        q: str,
        language: str,
        page: int,
        page_size: int,
        sort: str,
    ) -> SearchResult: ...

    def suggest(
        self, db: Session, *, q: str, language: str, limit: int
    ) -> list[Product]: ...


class SqlBackend:
    """Normalised-column search with a Python re-rank for typo tolerance.

    Two stages on purpose. SQL narrows to a bounded candidate set cheaply — it
    can match normalised substrings, which handles the common case. Python then
    ranks those candidates, which is where fuzzy matching lives, because no
    portable SQL can express "within two edits".
    """

    def _column(self, language: str):
        return Product.search_text_ar if language == "ar" else Product.search_text_en

    def _candidate_stmt(self, q: str, language: str):
        """Match any query token against either language's index.

        Both languages are searched regardless of the interface language: a
        shopper browsing in Arabic still types "Bible" sometimes.
        """
        stmt = visible_products_stmt().outerjoin(Publisher)
        tokens = search_text.tokens(q)
        if not tokens:
            return stmt

        clauses = []
        for token in tokens:
            like = f"%{token}%"
            clauses.append(Product.search_text_ar.like(like))
            clauses.append(Product.search_text_en.like(like))
        # An exact ISBN is worth matching even though it is not a "token".
        clauses.append(Product.isbn == search_text.normalize(q))
        return stmt.where(or_(*clauses))

    def _ranked(self, db: Session, q: str, language: str) -> list[Product]:
        candidates = list(
            db.scalars(
                self._candidate_stmt(q, language)
                .order_by(Product.purchase_count.desc())
                .limit(CANDIDATE_LIMIT)
            )
            .unique()
            .all()
        )

        # The substring prefilter cannot match a typo — "bibel" appears nowhere
        # in the index — so a misspelling would be discarded before the fuzzy
        # ranker ever saw it. When the precise pass finds little, widen to a
        # bounded sweep so typo tolerance actually gets a chance.
        #
        # Deliberately a fallback rather than the default: it scans rows instead
        # of using the index, so it only runs when the cheap path has already
        # come up short.
        if len(candidates) < FUZZY_FALLBACK_THRESHOLD:
            seen = {p.pk_product_id for p in candidates}
            widened = db.scalars(
                visible_products_stmt()
                .order_by(Product.purchase_count.desc())
                .limit(FUZZY_SCAN_LIMIT)
            ).unique().all()
            candidates.extend(p for p in widened if p.pk_product_id not in seen)

        scored: list[tuple[float, int, Product]] = []
        for product in candidates:
            best = max(
                search_text.score(q, product.search_text_ar or ""),
                search_text.score(q, product.search_text_en or ""),
            )
            if best > 0:
                # Purchase count breaks ties, so equally relevant results lead
                # with what people actually buy.
                scored.append((best, product.purchase_count, product))

        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [product for _, _, product in scored]

    def search(
        self, db: Session, *, q: str, language: str, page: int, page_size: int, sort: str
    ) -> SearchResult:
        if not q:
            # No query: an ordinary listing, paginated in SQL.
            stmt = visible_products_stmt()
            total = db.scalar(
                select(func.count()).select_from(stmt.order_by(None).subquery())
            ) or 0
            products = list(
                db.scalars(
                    apply_sort(stmt, sort, language)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).unique().all()
            )
            return SearchResult(products, total, q, page, page_size, sort)

        ranked = self._ranked(db, q, language)

        # An explicit sort overrides relevance — the shopper asked for it.
        if sort != "default":
            ids = [p.pk_product_id for p in ranked]
            if ids:
                ranked = list(
                    db.scalars(
                        apply_sort(
                            visible_products_stmt().where(Product.pk_product_id.in_(ids)),
                            sort,
                            language,
                        )
                    ).unique().all()
                )

        start = (page - 1) * page_size
        return SearchResult(
            products=ranked[start : start + page_size],
            total=len(ranked),
            q=q,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    def suggest(self, db: Session, *, q: str, language: str, limit: int) -> list[Product]:
        return self._ranked(db, q, language)[:limit]


class MeilisearchBackend:
    """Meilisearch (Part II §7.4).

    Meilisearch does its own typo tolerance and prefix matching, so the query
    goes across untouched — normalising it first would fight the engine's own
    analyser. The stored documents still carry our normalised blobs as extra
    searchable attributes, which helps with the Arabic letter-form folding its
    default analyser does not do.

    ⚠️ Untested against a live server in this environment. Verify before relying
    on it in production.
    """

    INDEX = "products"

    def __init__(self, client=None) -> None:
        self._client = client or self._connect()

    @staticmethod
    def _connect():
        import meilisearch  # imported lazily: an optional dependency

        return meilisearch.Client(settings.meilisearch_url, settings.meilisearch_api_key)

    def configure(self) -> None:
        """Apply index settings. Run once at deploy, and after a schema change."""
        index = self._client.index(self.INDEX)
        index.update_settings(
            {
                "searchableAttributes": [
                    "name_ar", "name_en",
                    "search_text_ar", "search_text_en",
                    "isbn", "publisher_ar", "publisher_en", "tags",
                ],
                "filterableAttributes": ["category_ids", "publisher_id", "price", "in_stock"],
                "sortableAttributes": ["price", "published_at", "purchase_count"],
                # Purchase count as the final tie-breaker, matching the SQL
                # backend so results do not reorder when the backend changes.
                "rankingRules": [
                    "words", "typo", "proximity", "attribute",
                    "sort", "exactness", "purchase_count:desc",
                ],
                "typoTolerance": {
                    "enabled": True,
                    "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 7},
                },
            }
        )

    def index_products(self, db: Session, products: list[Product]) -> None:
        documents = []
        for product in products:
            arabic, english = build_index_text(db, product)
            documents.append(
                {
                    "id": product.pk_product_id,
                    "name_ar": product.name_ar,
                    "name_en": product.name_en,
                    "search_text_ar": arabic,
                    "search_text_en": english,
                    "isbn": product.isbn,
                    "publisher_ar": product.publisher.name_ar if product.publisher else None,
                    "publisher_en": product.publisher.name_en if product.publisher else None,
                    "price": float(product.base_price_amt),
                    "purchase_count": product.purchase_count,
                }
            )
        if documents:
            self._client.index(self.INDEX).add_documents(documents, primary_key="id")

    def _hydrate(self, db: Session, ids: list[int]) -> list[Product]:
        """Fetch the rows for the ids the engine returned, preserving its order.

        The search engine ranks; the database remains the source of truth for
        price, stock and visibility — so results are always re-read here rather
        than rendered from the index.
        """
        if not ids:
            return []
        found = {
            p.pk_product_id: p
            for p in db.scalars(
                visible_products_stmt().where(Product.pk_product_id.in_(ids))
            ).unique().all()
        }
        return [found[i] for i in ids if i in found]

    def search(
        self, db: Session, *, q: str, language: str, page: int, page_size: int, sort: str
    ) -> SearchResult:
        response = self._client.index(self.INDEX).search(
            q, {"limit": page_size, "offset": (page - 1) * page_size}
        )
        ids = [hit["id"] for hit in response.get("hits", [])]
        return SearchResult(
            products=self._hydrate(db, ids),
            total=response.get("estimatedTotalHits", len(ids)),
            q=q,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    def suggest(self, db: Session, *, q: str, language: str, limit: int) -> list[Product]:
        response = self._client.index(self.INDEX).search(q, {"limit": limit})
        return self._hydrate(db, [hit["id"] for hit in response.get("hits", [])])


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

_backend: SearchBackend | None = None


def backend() -> SearchBackend:
    global _backend
    if _backend is None:
        _backend = _build_backend()
    return _backend


def reset_backend(instance: SearchBackend | None = None) -> SearchBackend:
    """Replace the backend — for tests, and after a config change."""
    global _backend
    _backend = instance or _build_backend()
    return _backend


def _build_backend() -> SearchBackend:
    if settings.search_backend == "meilisearch":
        try:
            instance = MeilisearchBackend()
            log.info("search_backend", extra={"backend": "meilisearch"})
            return instance
        except Exception:  # noqa: BLE001 - a search outage must not close the shop
            log.exception(
                "meilisearch_unavailable", extra={"fallback": "sql"}
            )
    return SqlBackend()


# ---------------------------------------------------------------------------
# Public API — what the storefront calls
# ---------------------------------------------------------------------------


def search_products(
    db: Session,
    *,
    q: str | None,
    language: str,
    page: int = 1,
    per_page: str | int | None = None,
    sort: str | None = None,
) -> SearchResult:
    term = clean_text(q or "", max_length=120) or ""
    return backend().search(
        db,
        q=term,
        language=language,
        page=max(page, 1),
        page_size=min(normalize_page_size(per_page), MAX_PAGE_SIZE),
        sort=normalize_sort(sort),
    )


def suggestions(
    db: Session, *, q: str | None, language: str = "ar", limit: int = 6
) -> list[Product]:
    term = clean_text(q or "", max_length=80) or ""
    if len(term) < 2:
        return []
    return backend().suggest(db, q=term, language=language, limit=limit)
