# app/services/moysklad.py
from typing import Any, Dict, Optional, List
import os
import mimetypes
import logging
import requests

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
    """
    Eslatma: ba'zi endpointlar dict qaytaradi, ba'zilari list qaytaradi.
    Shuning uchun return type'ni qat'iy Dict qilmaymiz.
    """
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


# ================= COUNTERPARTY =================

def _norm_phone_digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def find_counterparty_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    p = _norm_phone_digits(phone)
    if not p:
        return None
    data = ms_get("/entity/counterparty", params={"filter": f"phone~{p}", "limit": 1})
    if not isinstance(data, dict):
        return None
    rows = data.get("rows", [])
    return rows[0] if rows else None


def get_or_create_counterparty(name: str, phone: Optional[str] = None) -> Dict[str, Any]:
    name = (name or "").strip()
    phone_raw = (phone or "").strip()
    phone_digits = _norm_phone_digits(phone_raw)

    if phone_digits:
        found = find_counterparty_by_phone(phone_digits)
        if found:
            cp_id = found.get("id")
            updates: Dict[str, Any] = {}

            if name and (found.get("name") or "").strip() != name:
                updates["name"] = name

            if phone_raw and (found.get("phone") or "").strip() != phone_raw:
                updates["phone"] = phone_raw

            if updates and cp_id:
                return ms_put(f"/entity/counterparty/{cp_id}", updates)
            return found

    if name:
        data = ms_get("/entity/counterparty", params={"search": name, "limit": 1})
        if isinstance(data, dict):
            rows = data.get("rows", [])
            if rows:
                cp = rows[0]
                cp_id = cp.get("id")
                updates: Dict[str, Any] = {}
                if phone_raw and (cp.get("phone") or "").strip() != phone_raw:
                    updates["phone"] = phone_raw
                if updates and cp_id:
                    return ms_put(f"/entity/counterparty/{cp_id}", updates)
                return cp

    payload: Dict[str, Any] = {"name": name or phone_raw or "NoName"}
    if phone_raw:
        payload["phone"] = phone_raw
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
    """
    Bu funksiya /files endpoint orqali attachment sifatida yuboradi (Файлы bo'limiga tushadi).
    PaymentIn/CashIn/CustomerOrder uchun aynan shu kerak.
    """
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


# ==================== PRICE TYPES (Цены продажа) ====================

def get_price_types(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Ayrim MoySklad akkauntlarda javob:
      1) {"rows":[...]} (dict)
      2) [...] (list)
    Ikkalasini ham qo'llaymiz.
    """
    data = ms_get("/context/companysettings/pricetype", params={"limit": limit})

    # Case 1: dict with rows
    if isinstance(data, dict):
        rows = data.get("rows", [])
        return rows if isinstance(rows, list) else []

    # Case 2: plain list
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    return []


def find_price_type_meta_by_name(name: str) -> Optional[Dict[str, Any]]:
    name = (name or "").strip()
    if not name:
        return None

    rows = get_price_types(limit=200)

    # exact match
    for r in rows:
        if (r.get("name") or "").strip() == name and r.get("meta"):
            return r["meta"]

    # contains match
    nlow = name.lower()
    for r in rows:
        if nlow in (r.get("name") or "").lower() and r.get("meta"):
            return r["meta"]

    return None


def get_or_create_price_type_meta(name: str) -> Optional[Dict[str, Any]]:
    """
    Sizda create endpoint yo'q (1005). Shu sabab create qilmaymiz.
    Faqat companysettings'dan topamiz.
    """
    return find_price_type_meta_by_name(name)


# ==================== PRODUCT FOLDERS (Группы) ====================

def get_product_folders(limit: int = 50) -> List[Dict[str, Any]]:
    data = ms_get("/entity/productfolder", params={"limit": limit})
    if not isinstance(data, dict):
        return []
    return data.get("rows", []) or []


# ==================== PRODUCT (+ Создать новый товар) ====================

def create_product(
    name: str,
    productfolder_meta: Dict[str, Any],
    sale_price_uzs: int,
    price_type_meta: Optional[Dict[str, Any]] = None,
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
        # eng foydali debug: mavjud price typelarni logga chiqaramiz
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
                "value": int(sale_price_uzs) * 100,  # tiyin
                "priceType": {"meta": pt_meta},      # MUHIM: object meta bo'lishi shart
            }
        ],
    }
    return ms_post("/entity/product", payload)


# ==================== PRODUCT IMAGE (Изображения) ====================

def attach_image_to_product(product_id: str, file_path: str) -> Optional[Dict[str, Any]]:
    """
    Product карточкасидаги "Изображения" bo‘limiga yuklash.
    Agar "file" field ishlamasa, "image" field bilan qayta urinadi.
    """
    if not product_id or not file_path or not os.path.exists(file_path):
        logger.warning("Product image: missing product_id or file not found. product=%s file=%s", product_id, file_path)
        return None

    url = _url(f"/entity/product/{product_id}/images")
    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"

    headers = _headers().copy()
    headers.pop("Content-Type", None)  # multipart uchun

    def _try(field_name: str) -> Optional[Dict[str, Any]]:
        try:
            with open(file_path, "rb") as f:
                files = {field_name: (filename, f, mime)}
                r = requests.post(url, headers=headers, files=files, timeout=TIMEOUT)

            # Muhim debug:
            if not r.ok:
                logger.warning(
                    "Product image upload HTTP %s. field=%s url=%s body=%s",
                    r.status_code, field_name, url, r.text[:2000]
                )
                return None

            return r.json() if r.text else {"ok": True}
        except Exception as e:
            logger.warning("Product image upload failed: field=%s product=%s file=%s err=%s", field_name, product_id, file_path, e)
            return None

    # 1) Avval standart 'file'
    res = _try("file")
    if res is not None:
        return res

    # 2) Fallback: ba'zi akkauntlarda field nomi 'image' bo'lishi mumkin
    res2 = _try("image")
    if res2 is not None:
        return res2

    return None


# ==================== CUSTOMER ORDER (Продажи → Заказы покупателей) ====================

def create_customerorder(
    organization_meta: Dict[str, Any],
    agent_meta: Dict[str, Any],
    moment_iso: str,
    description: str,
    sales_channel_meta: Optional[Dict[str, Any]] = None,
    positions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "organization": {"meta": organization_meta},
        "agent": {"meta": agent_meta},
        "moment": moment_iso,
        "description": description,
        "applicable": False,  # черновик
    }
    if sales_channel_meta:
        payload["salesChannel"] = {"meta": sales_channel_meta}
    if positions:
        payload["positions"] = positions
    return ms_post("/entity/customerorder", payload)
