# app/services/moysklad.py
from typing import Any, Dict, Optional, List
import os
import mimetypes
import logging
import re
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
        raise MoySkladError(f"HTTP {resp.status_code} {resp.reason}. URL: {resp.url}. BODY: {resp.text}") from e
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

def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _normalize_phone_plus(phone: str) -> str:
    """
    MoySklad’ga saqlash uchun: +998XXXXXXXXX ko‘rinishi.
    """
    p = (phone or "").strip()
    if not p:
        return ""

    digits = _digits_only(p)
    if not digits:
        return ""

    if len(digits) == 9:
        return "+998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits
    if len(digits) > 12:
        return "+998" + digits[-9:]
    if 9 < len(digits) < 12:
        return "+998" + digits[-9:]
    return "+" + digits


def find_counterparty_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    """
    Qidirishda filter phone~digits ishlatamiz (MoySklad substring).
    """
    digits = _digits_only(phone)
    if not digits:
        return None
    data = ms_get("/entity/counterparty", params={"filter": f"phone~{digits}", "limit": 1})
    rows = data.get("rows", [])
    return rows[0] if rows else None


def get_or_create_counterparty(name: str, phone: Optional[str] = None) -> Dict[str, Any]:
    """
    1) phone bo‘lsa: avval phone bilan topadi
    2) topilmasa: name bilan topadi
    3) topilmasa: yaratadi
    Topilganda phone/name bo‘sh bo‘lsa yangilaydi (yengil update).
    """
    name = (name or "").strip()
    phone_plus = _normalize_phone_plus(phone or "")

    # 1) phone bilan topamiz
    if phone_plus:
        found = find_counterparty_by_phone(phone_plus)
        if found:
            cp_id = found.get("id")
            updates: Dict[str, Any] = {}

            if name and (found.get("name") or "").strip() != name:
                updates["name"] = name

            # agar phone bo‘sh yoki plus ko‘rinish emas bo‘lsa update qilamiz
            found_phone = _normalize_phone_plus(found.get("phone") or "")
            if not found_phone:
                updates["phone"] = phone_plus

            if updates and cp_id:
                return ms_put(f"/entity/counterparty/{cp_id}", updates)
            return found

    # 2) name bilan topamiz
    if name:
        data = ms_get("/entity/counterparty", params={"search": name, "limit": 1})
        rows = data.get("rows", [])
        if rows:
            cp = rows[0]
            cp_id = cp.get("id")
            updates: Dict[str, Any] = {}

            if phone_plus:
                found_phone = _normalize_phone_plus(cp.get("phone") or "")
                if not found_phone:
                    updates["phone"] = phone_plus

            if updates and cp_id:
                return ms_put(f"/entity/counterparty/{cp_id}", updates)
            return cp

    # 3) yaratamiz
    payload: Dict[str, Any] = {"name": name or phone_plus or "NoName"}
    if phone_plus:
        payload["phone"] = phone_plus
    return ms_post("/entity/counterparty", payload)


# ================= PAYMENT (KARTA) =================

def create_paymentin(
    organization_meta: Dict[str, Any],
    agent_meta: Dict[str, Any],
    sales_channel_meta: Dict[str, Any],
    sum_uzs: int,
    date_iso: str,
    time_hms: Optional[str],
    description: str,
) -> Dict[str, Any]:
    """
    Входящий платёж (karta).
    - project yuborilmaydi
    - Черновик: applicable=false
    """
    if sum_uzs <= 0:
        raise MoySkladError("Summa 0 dan katta bo‘lishi kerak.")

    moment_time = (time_hms or "00:00:00").strip() or "00:00:00"

    payload: Dict[str, Any] = {
        "organization": {"meta": organization_meta},
        "agent": {"meta": agent_meta},
        "salesChannel": {"meta": sales_channel_meta},
        "sum": int(sum_uzs) * 100,
        "moment": f"{date_iso} {moment_time}",
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
    time_hms: Optional[str],
    description: str,
) -> Dict[str, Any]:
    """
    Приходный ордер (naqt).
    - project yuborilmaydi
    - Черновик: applicable=false
    """
    if sum_uzs <= 0:
        raise MoySkladError("Summa 0 dan katta bo‘lishi kerak.")

    moment_time = (time_hms or "00:00:00").strip() or "00:00:00"

    payload: Dict[str, Any] = {
        "organization": {"meta": organization_meta},
        "agent": {"meta": agent_meta},
        "salesChannel": {"meta": sales_channel_meta},
        "sum": int(sum_uzs) * 100,
        "moment": f"{date_iso} {moment_time}",
        "description": description,
        "applicable": False,
    }
    return ms_post("/entity/cashin", payload)


# ================= FILE ATTACH (best-effort) =================

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
