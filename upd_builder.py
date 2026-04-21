"""Генератор XML УПД (ФНС, формат 5.03, функция СЧФДОП).

Выход — байты в windows-1251, как требует ФНС.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from xml.sax.saxutils import escape, quoteattr

from config import Settings
from xlsx_parser import RedemptionNotice


def _q2(x: Decimal) -> str:
    """Округление до 2 знаков «банковским» HALF_UP, как принято в УПД."""
    return str(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _vat_fraction(rate: str) -> Decimal:
    """Ставка НДС → дробь для расчёта. «без НДС»/«0%» → 0."""
    if not rate or rate in ("без НДС", "0%"):
        return Decimal(0)
    return Decimal(rate.rstrip("%")) / Decimal(100)


def _attr(v) -> str:
    """Экранирование атрибута XML."""
    return quoteattr(str(v) if v is not None else "")


def build_upd_xml(
    notice: RedemptionNotice,
    settings: Settings,
    doc_number: str | None = None,
    doc_date: datetime | None = None,
) -> bytes:
    """Собирает XML УПД.

    - `doc_number` — номер УПД (по умолчанию = номер уведомления о выкупе)
    - `doc_date` — дата УПД (по умолчанию = текущая дата + время)
    """
    s = settings
    doc_number = doc_number or notice.number
    now = doc_date or datetime.now()
    creation_date = now.strftime("%d.%m.%Y")
    creation_time = now.strftime("%H:%M:%S")
    notice_date_str = notice.notice_date.strftime("%d.%m.%Y")

    vat_rate = s.tax.vat_rate            # например «5%» или «без НДС»
    vat_frac = _vat_fraction(vat_rate)

    # ---- вычисления по таблице ----
    lines_xml: list[str] = []
    total_without_vat = Decimal(0)
    total_with_vat = Decimal(0)
    total_vat = Decimal(0)

    for idx, item in enumerate(notice.items, start=1):
        qty = Decimal(str(item.quantity))
        total_with = Decimal(str(item.sum_with_vat))
        total_without = (total_with / (1 + vat_frac)) if vat_frac else total_with
        vat_amount = total_with - total_without
        unit_price_without_vat = total_without / qty if qty else Decimal(0)

        total_without_vat += total_without
        total_with_vat += total_with
        total_vat += vat_amount

        # В УПД ставка пишется как «без НДС» или «5%» и т.д. — оставляем строкой.
        vat_attr = "без НДС" if vat_frac == 0 else vat_rate
        lines_xml.append(
            f'      <СведТов НомСтр={_attr(idx)} НалСт={_attr(vat_attr)} '
            f'НаимТов={_attr(item.name)} ОКЕИ_Тов="796" НаимЕдИзм="шт" '
            f'КолТов={_attr(f"{qty}")} ЦенаТов={_attr(_q2(unit_price_without_vat))} '
            f'СтТовБезНДС={_attr(_q2(total_without))} СтТовУчНал={_attr(_q2(total_with))}>\n'
            f'        <ДопСведТов КодТов={_attr(item.article)}/>\n'
            f'        <Акциз><БезАкциз>без акциза</БезАкциз></Акциз>\n'
            f'        <СумНал>' + (f'<СумНал>{_q2(vat_amount)}</СумНал>' if vat_frac else '<БезНДС>без НДС</БезНДС>') + f'</СумНал>\n'
            f'      </СведТов>'
        )

    items_block = "\n".join(lines_xml)

    # ---- блок продавца ----
    seller_address = (
        f'<АдрРФ КодРегион={_attr(s.seller.region_code)} '
        f'НаимРегион={_attr(s.seller.region_name)} '
        f'Индекс={_attr(s.seller.postal_code)} '
        f'Город={_attr(s.seller.city)} '
    )
    if s.seller.locality:
        seller_address += f'НаселПункт={_attr(s.seller.locality)} '
    seller_address += (
        f'Улица={_attr(s.seller.street)} '
        f'Дом={_attr(s.seller.house)}'
    )
    if s.seller.apartment:
        seller_address += f' Кварт={_attr(s.seller.apartment)}'
    seller_address += "/>"

    # ---- блок покупателя (ВБ) ----
    buyer_address = (
        f'<АдрРФ КодРегион={_attr(s.buyer.region_code)} '
        f'НаимРегион={_attr(s.buyer.region_name)} '
        f'Индекс={_attr(s.buyer.postal_code)} '
        f'Город={_attr(s.buyer.city)} '
        f'НаселПункт={_attr(s.buyer.locality)} '
        f'Улица={_attr(s.buyer.street)} '
        f'Дом={_attr(s.buyer.house)} '
        f'Корпус={_attr(s.buyer.building)}/>'
    )

    # ---- подписант ----
    mchd_block = ""
    if s.signer.auth_method == "6":
        mchd_block = (
            f'\n      <СвДоверЭлФорм НомДовер={_attr(s.signer.mchd_number)} '
            f'ДатаВыдДовер={_attr(s.signer.mchd_date)} '
            f'ИННДоверит={_attr(s.signer.mchd_issuer_inn)}/>'
        )

    # ---- собираем документ ----
    file_id = (
        f'ON_NSCHFDOPPR_{s.buyer.inn}_{s.buyer.kpp}_'
        f'{now.strftime("%d%m%Y")}_000000_0_0_0_0_0'
    )

    doc_number_safe = doc_number

    xml = f'''<?xml version="1.0" encoding="windows-1251"?>
<Файл ИдФайл={_attr(file_id)} ВерсФорм="5.03" ВерсПрог="WB-UPD-Gen 0.1">
  <Документ КНД="1115131" ВремИнфПр={_attr(creation_time)} ДатаИнфПр={_attr(creation_date)} Функция="СЧФДОП" ПоФактХЖ="Документ об отгрузке товаров" НаимДокОпр="Счет-фактура и документ об отгрузке" НаимЭконСубСост={_attr(f"{s.seller.full_name or ' '.join([s.seller.last_name, s.seller.first_name, s.seller.middle_name]).strip()}, ИНН {s.seller.inn}")}>
    <СвСчФакт НомерДок={_attr(doc_number_safe)} ДатаДок={_attr(creation_date)}>
      <СвПрод>
        <ИдСв><СвИП ИННФЛ={_attr(s.seller.inn)}><ФИО Фамилия={_attr(s.seller.last_name)} Имя={_attr(s.seller.first_name)} Отчество={_attr(s.seller.middle_name)}/></СвИП></ИдСв>
        <Адрес>{seller_address}</Адрес>
        <БанкРекв НомерСчета={_attr(s.bank.account)}>
          <СвБанк НаимБанк={_attr(s.bank.bank_name)} БИК={_attr(s.bank.bik)} КорСчет={_attr(s.bank.corr_account)}/>
        </БанкРекв>
      </СвПрод>
      <ГрузОт><ОнЖе>он же</ОнЖе></ГрузОт>
      <ГрузПолуч>
        <ИдСв><СвЮЛУч НаимОрг={_attr(s.buyer.name)} ИННЮЛ={_attr(s.buyer.inn)} КПП={_attr(s.buyer.kpp)}/></ИдСв>
        <Адрес>{buyer_address}</Адрес>
      </ГрузПолуч>
      <ДокПодтвОтгрНом РеквНаимДок="Счет-фактура и документ об отгрузке" РеквНомерДок={_attr(doc_number_safe)} РеквДатаДок={_attr(creation_date)}/>
      <СвПокуп>
        <ИдСв><СвЮЛУч НаимОрг={_attr(s.buyer.name)} ИННЮЛ={_attr(s.buyer.inn)} КПП={_attr(s.buyer.kpp)}/></ИдСв>
        <Адрес>{buyer_address}</Адрес>
      </СвПокуп>
      <ДенИзм КодОКВ="643" НаимОКВ="Российский рубль"/>
    </СвСчФакт>
    <ТаблСчФакт>
{items_block}
      <ВсегоОпл СтТовБезНДСВсего={_attr(_q2(total_without_vat))} СтТовУчНалВсего={_attr(_q2(total_with_vat))} КолНеттоВс="0">
        <СумНалВсего>{'<СумНал>' + _q2(total_vat) + '</СумНал>' if vat_frac else '<БезНДС>без НДС</БезНДС>'}</СумНалВсего>
      </ВсегоОпл>
    </ТаблСчФакт>
    <СвПродПер>
      <СвПер СодОпер="Реализация на основании уведомления о выкупе" ДатаПер={_attr(creation_date)}>
        <ОснПер РеквНаимДок="Уведомление о выкупе" РеквНомерДок={_attr(notice.number)} РеквДатаДок={_attr(notice_date_str)}/>
      </СвПер>
    </СвПродПер>
    <Подписант СпосПодтПолном={_attr(s.signer.auth_method)} Должн={_attr(s.signer.position)}>
      <ФИО Фамилия={_attr(s.signer.last_name)} Имя={_attr(s.signer.first_name)} Отчество={_attr(s.signer.middle_name)}/>{mchd_block}
    </Подписант>
  </Документ>
</Файл>
'''
    return xml.encode("cp1251", errors="xmlcharrefreplace")
