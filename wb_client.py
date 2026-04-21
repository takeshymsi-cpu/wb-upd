"""Клиент Wildberries API: получение информации о продавце и уведомлений о выкупе."""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

COMMON_API = "https://common-api.wildberries.ru"
DOCS_API = "https://documents-api.wildberries.ru"
REDEMPTION_CATEGORY = "Notice of redemption"   # значение поля `category` в ответе
REDEMPTION_CATEGORY_NAME = "redeem-notification"  # значение для query-параметра `category`


class WBError(RuntimeError):
    pass


def _creation_before(creation_time: str, since: date) -> bool:
    """True, если creationTime из WB API строго раньше даты `since`."""
    try:
        dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
    except ValueError:
        # если формат неожиданный — не отбрасываем документ
        return False
    return dt.date() < since


@dataclass
class SellerProfile:
    name: str
    inn: str
    trade_mark: str
    sid: str


@dataclass
class DocumentEntry:
    service_name: str       # «redeem-notification-693111100»
    name: str               # «Уведомление о выкупе №693111100 от 2026-04-13»
    category: str
    creation_time: str
    extensions: list[str]

    @property
    def redemption_id(self) -> Optional[str]:
        if self.service_name.startswith("redeem-notification-"):
            return self.service_name.split("-")[-1]
        return None


class WBClient:
    def __init__(self, token: Optional[str] = None, timeout: int = 30):
        self.token = token or os.getenv("WB_API_TOKEN", "")
        if not self.token:
            raise WBError("WB_API_TOKEN не задан (.env или аргумент конструктора).")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = self.token

    # ---- common-api ----------------------------------------------------
    def get_seller_profile(self) -> SellerProfile:
        # `/seller-info` у WB жёстко лимитирован (~1 rpm), поэтому держим
        # несколько ретраев с экспоненциальным backoff на 429.
        for attempt in range(4):
            r = self.session.get(
                f"{COMMON_API}/api/v1/seller-info", timeout=self.timeout
            )
            if r.status_code == 429 and attempt < 3:
                time.sleep(2.0 * (attempt + 1))  # 2 / 4 / 6 сек
                continue
            break
        r.raise_for_status()
        d = r.json()
        return SellerProfile(
            name=d.get("name", ""),
            inn=d.get("tin", ""),
            trade_mark=d.get("tradeMark", ""),
            sid=d.get("sid", ""),
        )

    # ---- documents-api -------------------------------------------------
    def list_documents(
        self,
        limit: int = 50,
        order: str = "desc",
        offset: int = 0,
        category: Optional[str] = None,
    ) -> list[DocumentEntry]:
        """Одна страница списка документов (не больше 50 — ограничение API).

        `category` принимает внутреннее имя (name) из `/documents/categories`,
        например `redeem-notification`, а не человекочитаемый title.
        """
        params: dict = {
            "sort": "date",
            "order": order,
            "limit": min(limit, 50),
            "offset": offset,
        }
        if category:
            params["category"] = category
        # Небольшой backoff при 429: WB лимитит ~1 rps на этом эндпоинте.
        for attempt in range(3):
            r = self.session.get(
                f"{DOCS_API}/api/v1/documents/list",
                params=params,
                timeout=self.timeout,
            )
            if r.status_code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        r.raise_for_status()
        docs = r.json().get("data", {}).get("documents", [])
        return [
            DocumentEntry(
                service_name=d["serviceName"],
                name=d["name"],
                category=d["category"],
                creation_time=d["creationTime"],
                extensions=d.get("extensions", []),
            )
            for d in docs
        ]

    def list_redemption_notices(
        self,
        since: Optional[date] = None,
        max_pages: int = 20,
    ) -> list[DocumentEntry]:
        """Уведомления о выкупе, отфильтрованные на стороне WB по категории.

        Идёт постранично по 50 штук в порядке убывания даты (через offset)
        и останавливается, как только встретит документ старше `since` или
        закончатся страницы.
        """
        result: list[DocumentEntry] = []
        offset = 0
        for i in range(max_pages):
            if i > 0:
                time.sleep(0.8)  # WB ограничивает ~1 rps
            page = self.list_documents(
                limit=50, offset=offset, category=REDEMPTION_CATEGORY_NAME
            )
            if not page:
                break
            stop = False
            for d in page:
                if since and _creation_before(d.creation_time, since):
                    stop = True
                    continue
                result.append(d)
            if stop or len(page) < 50:
                break
            offset += len(page)
        return result

    def download_document(self, service_name: str, extension: str = "zip") -> tuple[str, bytes]:
        """Скачать документ. Возвращает (имя файла, содержимое)."""
        for attempt in range(4):
            r = self.session.get(
                f"{DOCS_API}/api/v1/documents/download",
                params={"serviceName": service_name, "extension": extension},
                timeout=self.timeout,
            )
            if r.status_code == 429 and attempt < 3:
                time.sleep(1.5 * (attempt + 1))  # 1.5 / 3 / 4.5
                continue
            break
        r.raise_for_status()
        data = r.json().get("data", {})
        file_name = data.get("fileName", f"{service_name}.{extension}")
        raw = base64.b64decode(data["document"])
        return file_name, raw
