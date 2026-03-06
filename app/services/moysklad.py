# app/services/moysklad.py
from typing import Any, Dict, Optional, List
import os
import mimetypes
import logging
import requests
import base64
from datetime import datetime

from ..config import MOYSKLAD_BASE_URL, MOYSKLAD_TOKEN

TIMEOUT = 20
logger = logging.getLogger(__name__)


class MoySkladError(RuntimeError):
    pass


def _headers() -> Dict[str, str]:
    if not MOYSKLAD_TOKEN:
        raise RuntimeError("MOYSKLAD_TOKEN topilmadi. .env / Railway Variables ga MOYSKLAD_TOKEN kiriting.")
    return {
        "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json;charset=utf-8",
    }


def _url(path: str) -> str:
    return f"{MOYSKLAD_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _raise_http_error(e: requests.HTTPError) -> None:
    resp = e.response
    if resp is not None:
        raise MoySkladError(
            f"HTTP {resp.status_code} {resp.reason}. URL: {resp.url}. BODY: {resp.text}"
        ) from e
    raise


def ms_get(path: str, params: Optional[Dict[str, Any]] = None):
    try:
        r = requests.get(_url(path), headers=_headers(), params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        _raise_http_error(e)


def ms_post(path: str, payload: Dict[str, Any]):
    try:
        r = requests.post(_url(path), headers=_headers(), json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        _raise_http_error(e)


def ms_put(path: str, payload: Dict[str, Any]):
    try:
        r = requests.put(_url(path), headers=_headers(), json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        _raise_http_error(e)


# ================= BASIC =================

def get_default_organization() -> Dict[str, Any]:
    data = ms_get("/entity/organization", params={"limit": 1})
    if not isinstance(data, dict):
        raise MoySkladError("Organization endpoint kutilmagan format qaytardi.")
    rows = data.get("rows", [])
    if not rows:
        raise MoySkladError("Organization topilmadi.")
    return rows[0]


# ================= SALES CHANNEL =================

def get_sales_channels(limit: int = 50) -> List[Dict[str, Any]]:
    data = ms_get("/entity/saleschannel", params={"limit": limit})
    if not isinstance(data, dict):
        return []
    return data.get("rows", []) or []


# ================= STORE (Склад) =================

def get_stores(limit: int = 1000) -> List[Dict[str, Any]]:
    data = ms_get("/entity/store", params={"limit": limit})
    if not isinstance(data, dict):
        return []
    return data.get("rows", []) or []


def find_store_meta_by_name(name: str) -> Optional[Dict[str, Any]]:
    """
    Store (Склад) meta topish: /entity/store
    """
    name = (name or "").strip()
    if not name:
        return None

    rows = get_stores(limit=2000)

    # 1) exact match
    for r in rows:
        if (r.get("name") or "").strip() == name and r.get("meta"):
            return r["meta"]

    # 2) case-insensitive contains
    nlow = name.lower()
    for r in rows:
        if nlow in (r.get("name") or "").lower() and r.get("meta"):
            return r["meta"]

    return None


# ================= COUNTERPARTY =================

def _norm_phone_digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _norm_phone_plus(phone: str) -> str:
    d = _norm_phone_digits(phone)
    if not d:
        return ""
    if d.startswith("998") and len(d) == 12:
        return "+" + d
    if len(d) == 9:
        return "+998" + d
    return "+" + d


def find_counterparty_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    digits = _norm_phone_digits(phone)
    if not digits:
        return None

    variants = []
    plus = _norm_phone_plus(phone)
    if plus:
        variants.append(plus)
    variants.append(digits)
    if len(digits) >= 9:
        variants.append(digits[-9:])

    for v in variants:
        try:
            data = ms_get("/entity/counterparty", params={"filter": f"phone~{v}", "limit": 1})
            if isinstance(data, dict):
                rows = data.get("rows", []) or []
                if rows:
                    return rows[0]
        except Exception:
            continue

    return None


def _dedupe_rows_by_id(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        cid = str(r.get("id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(r)
    return out


def _counterparty_page(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = ms_get("/entity/counterparty", params=params)
    if not isinstance(data, dict):
        return []
    return data.get("rows", []) or []


def _search_counterparties_paged(
    *,
    filter_expr: Optional[str] = None,
    search_expr: Optional[str] = None,
    limit: int = 50,
    max_total: int = 200,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    max_total = max(1, int(max_total))

    all_rows: List[Dict[str, Any]] = []
    offset = 0

    while len(all_rows) < max_total:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if filter_expr:
            params["filter"] = filter_expr
        if search_expr:
            params["search"] = search_expr

        try:
            chunk = _counterparty_page(params)
        except Exception as e:
            logger.warning("Counterparty paged search failed: %s", e)
            break

        if not chunk:
            break

        all_rows.extend(chunk)
        if len(chunk) < limit:
            break

        offset += limit

    return _dedupe_rows_by_id(all_rows)[:max_total]


def search_counterparties(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Kuchaytirilgan qidiruv:
    - 2 ta harf/raqam bo'lsa ham ishlaydi
    - qisqa query uchun filter=name~ / phone~ ishlatadi
    - uzun query uchun search= ishlatadi
    - pagination bilan ko'proq chiqaradi
    """
    q = (query or "").strip()
    if not q:
        return []

    ui_limit = max(1, int(limit))
    digits = _norm_phone_digits(q)

    if len(q) <= 2:
        if digits:
            rows = _search_counterparties_paged(
                filter_expr=f"phone~{digits}",
                limit=50,
                max_total=max(ui_limit, 200),
            )
            return rows[:ui_limit]

        rows = _search_counterparties_paged(
            filter_expr=f"name~{q}",
            limit=50,
            max_total=max(ui_limit, 200),
        )
        return rows[:ui_limit]

    if digits and len(digits) >= 2:
        rows = _search_counterparties_paged(
            filter_expr=f"phone~{digits}",
            limit=50,
            max_total=max(ui_limit, 200),
        )
        if rows:
            return rows[:ui_limit]

    rows = _search_counterparties_paged(
        search_expr=q,
        limit=50,
        max_total=max(ui_limit, 200),
    )
    return rows[:ui_limit]


def get_or_create_counterparty(name: str, phone: Optional[str] = None) -> Dict[str, Any]:
    name = (name or "").strip()
    phone_raw = (phone or "").strip()

    if phone_raw:
        found = find_counterparty_by_phone(phone_raw)
        if found:
            cp_id = found.get("id")
            updates: Dict[str, Any] = {}

            if name and (found.get("name") or "").strip() != name:
                updates["name"] = name

            phone_plus = _norm_phone_plus(phone_raw) or phone_raw
            if phone_plus and (found.get("phone") or "").strip() != phone_plus:
                updates["phone"] = phone_plus

            if updates and cp_id:
                return ms_put(f"/entity/counterparty/{cp_id}", updates)
            return found

    if name:
        rows = search_counterparties(name, limit=1)
        if rows:
            cp = rows[0]
            cp_id = cp.get("id")
            updates: Dict[str, Any] = {}

            if phone_raw:
                phone_plus = _norm_phone_plus(phone_raw) or phone_raw
                if phone_plus and (cp.get("phone") or "").strip() != phone_plus:
                    updates["phone"] = phone_plus

            if updates and cp_id:
                return ms_put(f"/entity/counterparty/{cp_id}", updates)
            return cp

    payload: Dict[str, Any] = {"name": name or phone_raw or "NoName"}
    if phone_raw:
        payload["phone"] = _norm_phone_plus(phone_raw) or phone_raw
    return ms_post("/entity/counterparty", payload)


# ================= PAYMENT (KARTA) =================

def create_paymentin(
    organization_meta: Dict[str, Any],
    agent_meta: Dict[str, Any],
    sales_channel_meta: Dict[str, Any],
    sum_uzs: int,
    date_iso: str,
    description: str,
    time_hms: Optional[str] = None,
) -> Dict[str, Any]:
    if sum_uzs <= 0:
        raise MoySkladError("Summa 0 dan katta bo‘lishi kerak.")

    moment = f"{date_iso} {time_hms or '00:00:00'}"
    payload: Dict[str, Any] = {
        "organization": {"meta": organization_meta},
        "agent": {"meta": agent_meta},
        "salesChannel": {"meta": sales_channel_meta},
        "sum": int(sum_uzs) * 100,
        "moment": moment,
        "description": description,
        "applicable": False,
    }
    return ms_post("/entity/paymentin", payload)


# ================= CASH IN (NAQT) =================

def create_cashin(
    organization_meta: Dict[str, Any],
    agent_meta: Dict[str, Any],
    sales_channel_meta: Dict[str, Any],
    sum_uzs: int,
    date_iso: str,
    description: str,
    time_hms: Optional[str] = None,
) -> Dict[str, Any]:
    if sum_uzs <= 0:
        raise MoySkladError("Summa 0 dan katta bo‘lishi kerak.")

    moment = f"{date_iso} {time_hms or '00:00:00'}"
    payload: Dict[str, Any] = {
        "organization": {"meta": organization_meta},
        "agent": {"meta": agent_meta},
        "salesChannel": {"meta": sales_channel_meta},
        "sum": int(sum_uzs) * 100,
        "moment": moment,
        "description": description,
        "applicable": False,
    }
    return ms_post("/entity/cashin", payload)


# ================= FILE ATTACH (generic) =================

def _attach_file_generic(entity: str, doc_id: str, file_path: str) -> Optional[Dict[str, Any]]:
    if not doc_id or not file_path or not os.path.exists(file_path):
        return None

    url = _url(f"/entity/{entity}/{doc_id}/files")

    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"

    headers = _headers().copy()
    headers.pop("Content-Type", None)

    try:
        with open(file_path, "rb") as f:
            files = {"file": (filename, f, mime)}
            r = requests.post(url, headers=headers, files=files, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json() if r.text else {"ok": True}
    except Exception as e:
        logger.warning("File attach failed: entity=%s id=%s file=%s err=%s", entity, doc_id, file_path, e)
        return None


def attach_file_to_paymentin(paymentin_id: str, file_path: str) -> Optional[Dict[str, Any]]:
    return _attach_file_generic("paymentin", paymentin_id, file_path)


def attach_file_to_cashin(cashin_id: str, file_path: str) -> Optional[Dict[str, Any]]:
    return _attach_file_generic("cashin", cashin_id, file_path)


def attach_file_to_customerorder(order_id: str, file_path: str) -> Optional[Dict[str, Any]]:
    return _attach_file_generic("customerorder", order_id, file_path)


# ==================== PRICE TYPES ====================

def get_price_types(limit: int = 100) -> List[Dict[str, Any]]:
    data = ms_get("/context/companysettings/pricetype", params={"limit": limit})

    if isinstance(data, dict):
        rows = data.get("rows", [])
        return rows if isinstance(rows, list) else []

    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    return []


def find_price_type_meta_by_name(name: str) -> Optional[Dict[str, Any]]:
    name = (name or "").strip()
    if not name:
        return None

    rows = get_price_types(limit=200)

    for r in rows:
        if (r.get("name") or "").strip() == name and r.get("meta"):
            return r["meta"]

    nlow = name.lower()
    for r in rows:
        if nlow in (r.get("name") or "").lower() and r.get("meta"):
            return r["meta"]

    return None


def get_or_create_price_type_meta(name: str) -> Optional[Dict[str, Any]]:
    return find_price_type_meta_by_name(name)


# ==================== PRODUCT FOLDERS ====================

def get_product_folders(limit: int = 50) -> List[Dict[str, Any]]:
    data = ms_get("/entity/productfolder", params={"limit": limit})
    if not isinstance(data, dict):
        return []
    return data.get("rows", []) or []


# ==================== UOM (Единица измерения) ====================

def get_uoms(limit: int = 1000) -> List[Dict[str, Any]]:
    data = ms_get("/entity/uom", params={"limit": limit})
    if not isinstance(data, dict):
        return []
    return data.get("rows", []) or []


def find_uom_meta_by_name(name_ru: str) -> Optional[Dict[str, Any]]:
    """
    MoySklad'da UOM nomi bilan meta topadi.
    Masalan: 'шт', 'кг', 'рулон', 'м'
    """
    name_ru = (name_ru or "").strip()
    if not name_ru:
        return None

    rows = get_uoms(limit=2000)

    for r in rows:
        if (r.get("name") or "").strip().lower() == name_ru.lower() and r.get("meta"):
            return r["meta"]

    nlow = name_ru.lower()
    for r in rows:
        if nlow in (r.get("name") or "").strip().lower() and r.get("meta"):
            return r["meta"]

    return None


def get_or_create_uom_meta(unit_ru: str) -> Optional[Dict[str, Any]]:
    """
    XAVFSIZ: hozircha CREATE qilmaydi, faqat TOPADI.
    UOM topilmasa None qaytaradi.
    """
    unit_ru = (unit_ru or "").strip()

    mapping = {
        "шт": "шт",
        "штук": "шт",
        "sht": "шт",
        "dona": "шт",
        "kg": "кг",
        "кг": "кг",
        "килограмм": "кг",
        "рулон": "рулон",
        "rulon": "рулон",
        "roll": "рулон",
        "м": "м",
        "metr": "м",
        "meter": "м",
        "metre": "м",
        "m": "м",
    }
    key = unit_ru.lower()
    norm = mapping.get(key, unit_ru)

    meta = find_uom_meta_by_name(norm)
    if not meta:
        logger.warning(
            "UOM not found in MoySklad: %s (normalized=%s). Product will be created without UOM.",
            unit_ru,
            norm,
        )
    return meta


# ==================== PRODUCT ====================

def create_product(
    name: str,
    productfolder_meta: Dict[str, Any],
    sale_price_uzs: int,
    price_type_meta: Optional[Dict[str, Any]] = None,
    uom_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise MoySkladError("Product name bo'sh bo'lmasin.")
    if not productfolder_meta:
        raise MoySkladError("productfolder_meta yo'q (Группа tanlanmagan).")
    if not isinstance(sale_price_uzs, int) or sale_price_uzs <= 0:
        raise MoySkladError("sale_price_uzs noto'g'ri.")

    pt_meta = price_type_meta or get_or_create_price_type_meta("Цена продажи")
    if not pt_meta:
        try:
            names = [r.get("name") for r in get_price_types(200)]
            logger.warning("PriceType not found. Available: %s", names)
        except Exception:
            pass
        raise MoySkladError(
            "PriceType topilmadi. MoySklad → Настройки → Цены bo'limida ishlatilayotgan "
            "priceType nomini tekshiring (masalan: 'Цена продажи', 'Розница', 'Опт')."
        )

    payload: Dict[str, Any] = {
        "name": name,
        "productFolder": {"meta": productfolder_meta},
        "salePrices": [
            {
                "value": int(sale_price_uzs) * 100,
                "priceType": {"meta": pt_meta},
            }
        ],
    }

    if uom_meta:
        payload["uom"] = {"meta": uom_meta}

    return ms_post("/entity/product", payload)


def get_product_by_id(product_id: str) -> Optional[Dict[str, Any]]:
    product_id = (product_id or "").strip()
    if not product_id:
        return None
    try:
        data = ms_get(f"/entity/product/{product_id}")
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning("get_product_by_id failed: %s", e)
    return None


def search_products(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    /takror uchun tovar qidirish.
    Avval search bilan, kerak bo'lsa name~ filter bilan sinaydi.
    """
    q = (query or "").strip()
    if not q:
        return []

    rows: List[Dict[str, Any]] = []

    try:
        data = ms_get("/entity/product", params={"search": q, "limit": max(1, int(limit))})
        if isinstance(data, dict):
            rows = data.get("rows", []) or []
    except Exception as e:
        logger.warning("search_products(search=) failed: %s", e)

    if rows:
        return rows[:limit]

    try:
        data = ms_get("/entity/product", params={"filter": f"name~{q}", "limit": max(1, int(limit))})
        if isinstance(data, dict):
            rows = data.get("rows", []) or []
    except Exception as e:
        logger.warning("search_products(filter=name~) failed: %s", e)

    return rows[:limit]


# ==================== PRODUCT IMAGE ====================

def attach_image_to_product(product_id: str, file_path: str) -> Optional[Dict[str, Any]]:
    """
    Product карточкаси -> "Изображения" га расм тушириш.
    1) Avval JSON + base64 (content) bilan yuboramiz.
    2) Agar kerak bo'lsa multipart ham sinaymiz.
    """
    if not product_id or not file_path or not os.path.exists(file_path):
        logger.warning("Product image: missing product_id or file not found. product=%s file=%s", product_id, file_path)
        return None

    url = _url(f"/entity/product/{product_id}/images")
    filename = os.path.basename(file_path)

    ext = os.path.splitext(filename)[1].lower()
    if ext not in [".jpg", ".jpeg", ".png"]:
        filename = filename + ".jpg"

    try:
        with open(file_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")

        payload = {"filename": filename, "content": content_b64}
        r = requests.post(url, headers=_headers(), json=payload, timeout=TIMEOUT)
        if r.ok:
            return r.json() if r.text else {"ok": True}

        logger.warning("Product image JSON upload HTTP %s url=%s body=%s", r.status_code, url, r.text[:2000])
    except Exception as e:
        logger.warning("Product image JSON upload failed: product=%s file=%s err=%s", product_id, file_path, e)

    try:
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"

        headers = _headers().copy()
        headers.pop("Content-Type", None)

        with open(file_path, "rb") as f:
            files = {"file": (filename, f, mime)}
            r2 = requests.post(url, headers=headers, files=files, timeout=TIMEOUT)

        if r2.ok:
            return r2.json() if r2.text else {"ok": True}

        logger.warning("Product image multipart upload HTTP %s url=%s body=%s", r2.status_code, url, r2.text[:2000])
    except Exception as e:
        logger.warning("Product image multipart upload failed: product=%s file=%s err=%s", product_id, file_path, e)

    return None


# ==================== CUSTOMER ORDER IMAGE ====================

def attach_image_to_customerorder(order_id: str, file_path: str) -> Optional[Dict[str, Any]]:
    if not order_id or not file_path or not os.path.exists(file_path):
        logger.warning("Order image: missing order_id or file not found. order=%s file=%s", order_id, file_path)
        return None

    url = _url(f"/entity/customerorder/{order_id}/images")
    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"

    headers = _headers().copy()
    headers.pop("Content-Type", None)

    def _try(field_name: str) -> Optional[Dict[str, Any]]:
        try:
            with open(file_path, "rb") as f:
                files = {field_name: (filename, f, mime)}
                r = requests.post(url, headers=headers, files=files, timeout=TIMEOUT)

            if not r.ok:
                logger.warning(
                    "Order image upload HTTP %s. field=%s url=%s body=%s",
                    r.status_code, field_name, url, r.text[:2000]
                )
                return None

            return r.json() if r.text else {"ok": True}
        except Exception as e:
            logger.warning("Order image upload failed: field=%s order=%s file=%s err=%s", field_name, order_id, file_path, e)
            return None

    res = _try("file")
    if res is not None:
        return res

    res2 = _try("image")
    if res2 is not None:
        return res2

    return None


# ==================== CUSTOMER ORDER ====================

def create_customerorder(
    organization_meta: Dict[str, Any],
    agent_meta: Dict[str, Any],
    moment_iso: Optional[str],
    description: str,
    sales_channel_meta: Optional[Dict[str, Any]] = None,
    positions: Optional[List[Dict[str, Any]]] = None,
    store_meta: Optional[Dict[str, Any]] = None,
    vat_enabled: bool = False,
    vat_included: bool = False,
) -> Dict[str, Any]:
    # moment berilmasa, hozirgi vaqt qo'yamiz (timezone'siz)
    moment_final = (moment_iso or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    payload: Dict[str, Any] = {
        "organization": {"meta": organization_meta},
        "agent": {"meta": agent_meta},
        "moment": moment_final,
        "description": description,
        "applicable": False,   # черновик
        "vatEnabled": bool(vat_enabled),
        "vatIncluded": bool(vat_included),
    }
    if sales_channel_meta:
        payload["salesChannel"] = {"meta": sales_channel_meta}
    if store_meta:
        payload["store"] = {"meta": store_meta}
    if positions:
        payload["positions"] = positions

    return ms_post("/entity/customerorder", payload)