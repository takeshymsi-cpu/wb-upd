"""Работа с настройками: загрузка/сохранение YAML-конфига с реквизитами."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

CONFIG_PATH = Path(__file__).parent / "settings.yaml"


class SellerInfo(BaseModel):
    """Реквизиты продавца (ИП)."""
    full_name: str = ""          # «Иванов Иван Иванович»
    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    inn: str = ""
    ogrnip: str = ""
    trade_mark: str = ""         # бренд, информативно
    # Адрес
    region_code: str = ""        # «77»
    region_name: str = ""        # «г. Москва»
    postal_code: str = ""        # «101000»
    city: str = ""               # «Москва»
    street: str = ""             # «ул. Такая-то»
    house: str = ""              # «1»
    apartment: str = ""          # «10»
    # Прочее
    locality: str = ""           # «мкр. ..., ...» — опциональное поле для АдрРФ


class BankInfo(BaseModel):
    """Банковские реквизиты продавца."""
    bank_name: str = ""
    bik: str = ""
    corr_account: str = ""
    account: str = ""            # расчётный счёт


class TaxInfo(BaseModel):
    """Налоговый режим и ставка НДС."""
    regime: Literal["ОСНО", "УСН доходы", "УСН доходы минус расходы", "НПД"] = "УСН доходы"
    vat_rate: Literal["без НДС", "0%", "5%", "7%", "10%", "20%"] = "5%"


class SignerInfo(BaseModel):
    """Подписант УПД."""
    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    position: str = "Индивидуальный предприниматель"
    # Способ подтверждения полномочий (код из справочника ФНС):
    # 1 — лицо, действующее без доверенности
    # 6 — МЧД
    auth_method: Literal["1", "6"] = "1"
    mchd_number: str = ""        # если auth_method=6
    mchd_date: str = ""          # ДД.ММ.ГГГГ
    mchd_issuer_inn: str = ""


class BuyerInfo(BaseModel):
    """Грузополучатель и покупатель. По умолчанию — ООО РВБ (Wildberries)."""
    name: str = "ООО РВБ"
    inn: str = "9714053621"
    kpp: str = "507401001"
    region_code: str = "50"
    region_name: str = "Московская область"
    postal_code: str = "142181"
    city: str = "Подольск"
    locality: str = "Коледино"
    street: str = "Индустриальный парк"
    house: str = "6"
    building: str = "1"


class Settings(BaseModel):
    seller: SellerInfo = Field(default_factory=SellerInfo)
    bank: BankInfo = Field(default_factory=BankInfo)
    tax: TaxInfo = Field(default_factory=TaxInfo)
    signer: SignerInfo = Field(default_factory=SignerInfo)
    buyer: BuyerInfo = Field(default_factory=BuyerInfo)


def load_settings() -> Settings:
    if not CONFIG_PATH.exists():
        return Settings()
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Settings(**data)


def save_settings(settings: Settings) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            settings.model_dump(),
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
