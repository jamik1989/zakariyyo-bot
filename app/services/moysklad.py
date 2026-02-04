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


def ms_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        r = requests.get(_url(path), headers=_headers(), params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        _raise_http_error(e)


def ms_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = requests.post(_url(path), headers=_headers(), json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        _raise_http_error(e)


def ms_put(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = requests.put(_url(path), headers=_headers(), json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        _raise_http_error(e)


# ================= BASIC =================

def get_default_organization() -> Dict[str, Any]:
    data = ms_get("/entity/organization", params={"limit": 1})
    rows = data.get("rows", [])
    if not rows:
        raise MoySkladError("Organization topilmadi.")
    return rows[0]


# ================= SALES CHANNEL =================

def get_sales_channels(limit: int = 50) -> List[Dict[str, Any]]:
    data = ms_get("/entity/saleschannel", params={"limit": limit})
    return data.get("rows", [])


# ================= COUNTERPARTY =================

def _norm_phone_digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def find_counterparty_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    p = _norm_phone_digits(phone)
    if not p:
        return None
    data = ms_get("/entity/counterparty", params={"filter": f"phone~{p}", "limit": 1})
    rows = data.get("rows", [])
    return rows[0] if rows else None


def get_or_create_counterparty(name: str, phone: Optional[str] = None) -> Dict[str, Any]:
    """
    phone: +998... ko‘rinishida kelsa ham saqlaymiz.
    Qidiruv esa digits bilan (phone~digits).
    """
    name = (name or "").strip()
    phone_raw = (phone or "").strip()
    phone_digits = _norm_phone_digits(phone_raw)

    # 1) phone bilan topish
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

    # 2) name bilan topish
    if name:
        data = ms_get("/entity/counterparty", params={"search": name, "limit": 1})
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

    # 3) create
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


# ================= FILE ATTACH =================

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
