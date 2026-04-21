"""Streamlit-приложение: настройки, уведомления о выкупе, журнал."""
from __future__ import annotations

import io
import os
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import streamlit as st

from config import Settings, load_settings, save_settings
from storage import init_db, is_processed, list_processed, mark_processed, get_processed
from upd_builder import build_upd_xml
from wb_client import WBClient, WBError
from xlsx_parser import extract_xlsx_from_zip, parse_notice_xlsx

st.set_page_config(page_title="Уведомление → УПД", page_icon="🧾", layout="wide")

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
init_db()

# ---- инициализация состояния ---------------------------------------------
if "settings" not in st.session_state:
    st.session_state.settings = load_settings()
# Токен хранится в session_state; по умолчанию — из .env
if "wb_token" not in st.session_state:
    st.session_state.wb_token = os.getenv("WB_API_TOKEN", "")

settings: Settings = st.session_state.settings


# ---- кеш клиента и профиля WB (чтобы не долбить API на каждом rerun) -----
@st.cache_resource
def get_wb_client(token: str) -> WBClient:
    return WBClient(token=token)


@st.cache_data(ttl=3600, show_spinner=False)
def get_wb_profile(token: str) -> dict:
    """Кеш профиля продавца на 1 час. Ключом кеша служит токен —
    при смене токена кеш автоматически промахивается. Исключения
    не кешируются."""
    p = get_wb_client(token).get_seller_profile()
    return {"name": p.name, "inn": p.inn, "trade_mark": p.trade_mark, "sid": p.sid}


def _save_token_to_env(token: str) -> None:
    """Сохраняет WB_API_TOKEN в .env рядом с app.py (создаёт или обновляет)."""
    env_path = Path(__file__).parent / ".env"
    lines: list[str] = []
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if not line.strip().startswith("WB_API_TOKEN="):
                lines.append(line)
    lines.append(f"WB_API_TOKEN={token}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- сайдбар: статус подключения к WB ------------------------------------
def _render_token_form(error_msg: str | None = None) -> None:
    """Форма ввода токена WB. Показывается, если токена нет или он неверный."""
    if error_msg:
        st.error(error_msg)

    with st.expander("📋 Как создать токен (пошагово)", expanded=False):
        st.markdown(
            """
1. В кабинете продавца WB: **Профиль → Интеграции по API** → **«Создать токен»**.
2. Вкладка **«Для интеграции вручную»**.
3. Тип токена: **«Персональный токен»** (только для своих решений).
   ⚠️ Именно персональный, не «Базовый» — у базового ограничен доступ
   к `/seller-info` и быстро ловит 429.
4. **Название**: любое, например `wb upd`.
5. **К каким категориям будет доступ**: поставь галку только на **«Документы»**.
6. **Уровень доступа**: **«Только чтение»** — этого достаточно.
7. **«Создать токен»** → скопируй появившуюся JWT-строку (длинная, начинается
   на `eyJ…`).
8. Вставь её в поле ниже и нажми «Подключиться».

> Токен хранится **только у тебя на компьютере** (в `.env` и/или в памяти
> браузера). Никуда не отправляется, кроме самого WB API.
"""
        )

    # В Streamlit нет прямого способа отключить autocomplete у text_input,
    # поэтому используем обычное (не password) поле — Chrome не станет
    # предлагать сгенерировать пароль и не подсунет autofill.
    # JWT-токен и так не содержит персональных данных, видимых на экране.
    with st.form("wb_token_form", clear_on_submit=False):
        new_tok = st.text_input(
            "WB API токен",
            value=st.session_state.wb_token,
            placeholder="eyJhbGciOi...",
            help="JWT-строка из кабинета WB. Хранится только у тебя.",
            autocomplete="off",
        )
        save_to_env = st.checkbox(
            "Сохранить в .env (чтобы не вводить снова)", value=True
        )
        submitted = st.form_submit_button(
            "✅ Подключиться", type="primary", use_container_width=True
        )
        if submitted:
            tok = new_tok.strip()
            if not tok:
                st.warning("Токен не должен быть пустым.")
            else:
                st.session_state.wb_token = tok
                if save_to_env:
                    try:
                        _save_token_to_env(tok)
                    except Exception as e:
                        st.warning(f"Не удалось записать в .env: {e}")
                get_wb_client.clear()
                get_wb_profile.clear()
                st.rerun()


with st.sidebar:
    st.markdown("### 🔑 Подключение к WB")
    client = None
    profile = None
    token = st.session_state.wb_token

    if not token:
        _render_token_form("Токен WB не задан.")
    else:
        # Клиент создаём всегда — он валиден, пока есть токен.
        # Профиль продавца подтягиваем опционально: ручка /seller-info
        # очень жёстко лимитирована WB (~1 rpm), и требовать её работу
        # при каждом заходе — значит ломать весь UI из-за 429.
        try:
            client = get_wb_client(token)
        except WBError as e:
            client = None
            _render_token_form(str(e))

        if client:
            try:
                prof = get_wb_profile(token)
                from types import SimpleNamespace
                profile = SimpleNamespace(**prof)
                st.success(f"{profile.name}\n\nИНН: `{profile.inn}`")
                st.caption(f"Бренд: {profile.trade_mark}")
            except Exception as e:
                msg = str(e)
                if "401" in msg or "403" in msg:
                    # Токен действительно битый → перезапрашиваем
                    client = None
                    _render_token_form(
                        "Токен не принят WB API (401/403). Проверь, что "
                        "токен живой и у него есть право «Документы»."
                    )
                elif "429" in msg:
                    st.success("✅ Токен сохранён")
                    st.warning(
                        "Профиль продавца сейчас не подтянулся — WB временно "
                        "ограничил запросы к `/seller-info` (это нормальная "
                        "реакция WB ~1 раз/мин). Можно спокойно идти на "
                        "вкладку «Уведомления» — там другие лимиты."
                    )
                else:
                    st.warning(f"Профиль не загрузился: {msg}")

        if client:
            c_r, c_t = st.columns(2)
            if c_r.button("🔄 Обновить", use_container_width=True):
                get_wb_profile.clear()
                st.rerun()
            if c_t.button("🔑 Сменить токен", use_container_width=True):
                st.session_state.wb_token = ""
                get_wb_client.clear()
                get_wb_profile.clear()
                st.rerun()

    st.divider()
    st.markdown("### 📁 Папка вывода")
    st.caption(f"`{OUTPUT_DIR}`")
    st.caption("Сгенерированные УПД кладутся сюда — дальше вручную загружаешь в Диадок.")


# ---- вкладки -------------------------------------------------------------
tab_settings, tab_notices, tab_log = st.tabs(
    ["⚙️ Настройки", "📥 Уведомления о выкупе", "📋 Журнал"]
)


# ═══════════ ВКЛАДКА НАСТРОЕК ═════════════════════════════════════════════
with tab_settings:
    st.header("Реквизиты и налоги")
    st.caption(
        "Эти данные попадут в УПД. ФИО и ИНН автоподтягиваются из WB API — "
        "всё остальное заполняется вручную и сохраняется в `settings.yaml`."
    )

    col_auto1, col_auto2 = st.columns([1, 3])
    with col_auto1:
        pull_clicked = st.button(
            "🔄 Подтянуть из WB API",
            use_container_width=True,
            disabled=client is None,
            help="Заберёт ИНН, фамилию и бренд из WB (ручка /seller-info; "
                 "WB жёстко её лимитирует — будет несколько ретраев)",
        )
        if pull_clicked and client is not None:
            # Тянем профиль прямо здесь с собственными ретраями,
            # чтобы 429 на /seller-info не блокировал весь сайдбар.
            with st.spinner("Запрашиваю профиль у WB (до ~1 минуты)…"):
                prof_data = None
                last_err = None
                for attempt in range(4):
                    try:
                        prof_data = get_wb_profile(token)
                        break
                    except Exception as e:
                        last_err = e
                        if "429" in str(e) and attempt < 3:
                            time.sleep(15 * (attempt + 1))  # 15 / 30 / 45 сек
                            get_wb_profile.clear()
                            continue
                        break
            if prof_data:
                settings.seller.trade_mark = prof_data.get("trade_mark", "")
                settings.seller.inn = prof_data.get("inn", "")
                nm = prof_data.get("name", "").replace("ИП", "").strip()
                parts = [p.strip(". ") for p in nm.replace(".", " ").split() if p.strip(". ")]
                if parts:
                    settings.seller.last_name = parts[0]
                    if len(parts) > 1 and len(parts[1]) > 1:
                        settings.seller.first_name = parts[1]
                    if len(parts) > 2 and len(parts[2]) > 1:
                        settings.seller.middle_name = parts[2]
                st.success(
                    f"Подтянуто: **{prof_data.get('name','')}**, ИНН "
                    f"`{prof_data.get('inn','')}`. Остальные поля заполни "
                    "вручную."
                )
            else:
                msg = str(last_err) if last_err else "неизвестная ошибка"
                if "429" in msg:
                    st.error(
                        "WB и после нескольких ретраев возвращает 429 на "
                        "`/seller-info`. Подожди 2–5 минут и попробуй ещё "
                        "раз. Или заполни ФИО/ИНН вручную — это ровно то, "
                        "что WB отдаёт, и больше ничего."
                    )
                else:
                    st.error(f"Не получилось: {msg}")
    with col_auto2:
        if profile:
            st.info(
                f"WB API возвращает только: **{profile.name}**, ИНН "
                f"`{profile.inn}`. Остальные реквизиты (полное ФИО, адрес, "
                "ОГРНИП, банк) заполняются вручную — один раз, потом "
                "сохранятся в `settings.yaml`."
            )
        else:
            st.info(
                "Нажми «🔄 Подтянуть из WB API» чтобы автоматически заполнить "
                "ИНН, фамилию и бренд (остальное — вручную). Или просто "
                "заполни всё сам в полях ниже."
            )

    st.divider()

    # ═══ ПРОДАВЕЦ ══════════════════════════════════════════════════════════
    st.subheader("🏪 Продавец (ИП)")
    c1, c2, c3 = st.columns(3)
    settings.seller.last_name = c1.text_input("Фамилия", settings.seller.last_name)
    settings.seller.first_name = c2.text_input("Имя", settings.seller.first_name)
    settings.seller.middle_name = c3.text_input("Отчество", settings.seller.middle_name)

    c1, c2, c3 = st.columns(3)
    settings.seller.inn = c1.text_input("ИНН", settings.seller.inn)
    settings.seller.ogrnip = c2.text_input("ОГРНИП", settings.seller.ogrnip)
    settings.seller.trade_mark = c3.text_input("Бренд", settings.seller.trade_mark)

    st.markdown("**Адрес**")
    c1, c2 = st.columns([1, 2])
    settings.seller.region_code = c1.text_input("Код региона", settings.seller.region_code, help="2 цифры, напр. 63")
    settings.seller.region_name = c2.text_input("Название региона", settings.seller.region_name)

    c1, c2, c3 = st.columns([1, 2, 2])
    settings.seller.postal_code = c1.text_input("Индекс", settings.seller.postal_code)
    settings.seller.city = c2.text_input("Город", settings.seller.city)
    settings.seller.locality = c3.text_input("Нас. пункт / район (опц.)", settings.seller.locality)

    c1, c2, c3 = st.columns([3, 1, 1])
    settings.seller.street = c1.text_input("Улица", settings.seller.street)
    settings.seller.house = c2.text_input("Дом", settings.seller.house)
    settings.seller.apartment = c3.text_input("Кв./офис", settings.seller.apartment)

    st.divider()

    st.subheader("🏦 Банковские реквизиты")
    c1, c2 = st.columns(2)
    settings.bank.bank_name = c1.text_input("Наименование банка", settings.bank.bank_name)
    settings.bank.bik = c2.text_input("БИК", settings.bank.bik)

    c1, c2 = st.columns(2)
    settings.bank.corr_account = c1.text_input("Корр. счёт", settings.bank.corr_account)
    settings.bank.account = c2.text_input("Расчётный счёт", settings.bank.account)

    st.divider()

    st.subheader("💸 Налогообложение")
    c1, c2 = st.columns(2)
    regimes = ["ОСНО", "УСН доходы", "УСН доходы минус расходы", "НПД"]
    settings.tax.regime = c1.selectbox(
        "Режим", regimes, index=regimes.index(settings.tax.regime)
    )
    vat_rates = ["без НДС", "0%", "5%", "7%", "10%", "20%"]
    settings.tax.vat_rate = c2.selectbox(
        "Ставка НДС", vat_rates, index=vat_rates.index(settings.tax.vat_rate),
        help="Для УСН с 2025 доступны льготные 5% / 7% при обороте 60–450 млн"
    )
    if settings.tax.regime == "НПД":
        st.warning("Самозанятые не выставляют УПД — используется чек из «Мой налог».")

    st.divider()

    st.subheader("✍️ Подписант УПД")
    c1, c2, c3 = st.columns(3)
    settings.signer.last_name = c1.text_input("Фамилия ", settings.signer.last_name, key="sign_ln")
    settings.signer.first_name = c2.text_input("Имя ", settings.signer.first_name, key="sign_fn")
    settings.signer.middle_name = c3.text_input("Отчество ", settings.signer.middle_name, key="sign_mn")

    settings.signer.position = st.text_input("Должность", settings.signer.position)

    auth_options = {"1": "Лицо действует без доверенности (сам ИП)", "6": "По МЧД"}
    settings.signer.auth_method = st.selectbox(
        "Способ подтверждения полномочий",
        list(auth_options.keys()),
        index=list(auth_options.keys()).index(settings.signer.auth_method),
        format_func=lambda k: auth_options[k],
    )
    if settings.signer.auth_method == "6":
        c1, c2, c3 = st.columns(3)
        settings.signer.mchd_number = c1.text_input("Номер МЧД", settings.signer.mchd_number)
        settings.signer.mchd_date = c2.text_input("Дата МЧД (ДД.ММ.ГГГГ)", settings.signer.mchd_date)
        settings.signer.mchd_issuer_inn = c3.text_input("ИНН доверителя", settings.signer.mchd_issuer_inn)

    st.divider()

    with st.expander("👜 Покупатель / грузополучатель (ООО РВБ — значения по умолчанию)"):
        c1, c2, c3 = st.columns(3)
        settings.buyer.name = c1.text_input("Наименование", settings.buyer.name)
        settings.buyer.inn = c2.text_input("ИНН ", settings.buyer.inn, key="buyer_inn")
        settings.buyer.kpp = c3.text_input("КПП", settings.buyer.kpp)

        c1, c2 = st.columns(2)
        settings.buyer.region_code = c1.text_input("Код региона ", settings.buyer.region_code, key="buyer_rc")
        settings.buyer.region_name = c2.text_input("Название региона ", settings.buyer.region_name, key="buyer_rn")

        c1, c2, c3 = st.columns(3)
        settings.buyer.postal_code = c1.text_input("Индекс ", settings.buyer.postal_code, key="buyer_pc")
        settings.buyer.city = c2.text_input("Город ", settings.buyer.city, key="buyer_city")
        settings.buyer.locality = c3.text_input("Нас. пункт", settings.buyer.locality)

        c1, c2, c3 = st.columns([3, 1, 1])
        settings.buyer.street = c1.text_input("Улица ", settings.buyer.street, key="buyer_street")
        settings.buyer.house = c2.text_input("Дом ", settings.buyer.house, key="buyer_house")
        settings.buyer.building = c3.text_input("Корпус", settings.buyer.building)

    st.divider()
    if st.button("💾 Сохранить настройки", type="primary"):
        save_settings(settings)
        st.success("Сохранено в settings.yaml")


# ═══════════ ВКЛАДКА УВЕДОМЛЕНИЙ ══════════════════════════════════════════
def _generate_upd_for(entry, client) -> tuple[Path, dict]:
    """Скачивает уведомление, строит XML, сохраняет и пишет в журнал.
    Возвращает путь к файлу и сводку."""
    file_name, zip_bytes = client.download_document(entry.service_name)
    xlsx_bytes = extract_xlsx_from_zip(zip_bytes)
    notice = parse_notice_xlsx(xlsx_bytes)
    xml_bytes = build_upd_xml(notice, settings)

    out_path = OUTPUT_DIR / f"УПД_{notice.number}_{notice.notice_date.strftime('%Y-%m-%d')}.xml"
    out_path.write_bytes(xml_bytes)

    total_sum = sum(i.sum_with_vat for i in notice.items)
    mark_processed(
        redemption_id=notice.number,
        service_name=entry.service_name,
        notice_name=entry.name,
        notice_date=notice.notice_date.isoformat(),
        upd_number=notice.number,
        upd_date=datetime.now().strftime("%Y-%m-%d"),
        xml_path=str(out_path),
        total_sum=total_sum,
        items_count=len(notice.items),
    )
    return out_path, {
        "number": notice.number,
        "date": notice.notice_date,
        "items": len(notice.items),
        "total": total_sum,
    }


def _zip_all_upds(records: list[dict]) -> bytes:
    """Упаковывает все существующие XML из журнала в один zip-архив."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in records:
            p = Path(r["xml_path"])
            if p.exists():
                zf.write(p, arcname=p.name)
    return buf.getvalue()


with tab_notices:
    st.header("Уведомления о выкупе")
    st.caption("Список из WB API. Сгенерированные УПД сохраняются в папку `output/`.")

    default_since = date(date.today().year, 1, 1)
    col1, col_since, col2, col_dl, _ = st.columns([1, 1.2, 1.4, 1.4, 2])
    if col1.button("🔄 Обновить список"):
        st.session_state.pop("notices", None)
    since = col_since.date_input(
        "С даты",
        value=st.session_state.get("notices_since", default_since),
        format="DD.MM.YYYY",
        key="notices_since_input",
    )
    if since != st.session_state.get("notices_since"):
        st.session_state.notices_since = since
        st.session_state.pop("notices", None)

    if client and profile:
        if "notices" not in st.session_state:
            with st.spinner("Загружаю список из WB…"):
                try:
                    st.session_state.notices = client.list_redemption_notices(since=since)
                except Exception as e:
                    st.error(f"Не удалось получить список: {e}")
                    st.session_state.notices = []

        notices = st.session_state.get("notices", [])

        # --- Скачать все сформированные УПД одним zip ---
        all_records = list_processed()
        existing = [r for r in all_records if Path(r["xml_path"]).exists()]
        if existing:
            zip_name = f"УПД_все_{date.today().strftime('%Y-%m-%d')}.zip"
            col_dl.download_button(
                f"📦 Скачать все ({len(existing)})",
                data=_zip_all_upds(existing),
                file_name=zip_name,
                mime="application/zip",
                use_container_width=True,
            )
        else:
            col_dl.button("📦 Скачать все (0)", disabled=True, use_container_width=True)

        if col2.button("⚡ Сформировать УПД для всех новых", type="primary", disabled=not notices):
            new_ones = [n for n in notices if n.redemption_id and not is_processed(n.redemption_id)]
            if not new_ones:
                st.info("Новых уведомлений нет — все уже обработаны.")
            else:
                progress = st.progress(0.0, text=f"0 / {len(new_ones)}")
                errors = []
                for i, entry in enumerate(new_ones, 1):
                    if i > 1:
                        time.sleep(1.0)  # не долбим WB чаще ~1 rps
                    try:
                        path, summary = _generate_upd_for(entry, client)
                        st.toast(f"✓ УПД {summary['number']} · {summary['items']} поз. · {summary['total']:.2f} ₽")
                    except Exception as e:
                        errors.append((entry.name, str(e)))
                    progress.progress(i / len(new_ones), text=f"{i} / {len(new_ones)}")
                if errors:
                    st.error("Ошибки:\n" + "\n".join(f"• {n}: {e}" for n, e in errors))
                else:
                    st.success(f"Готово. Обработано: {len(new_ones)}. Папка: `{OUTPUT_DIR}`")
                    st.rerun()

        if not notices:
            st.info(f"Уведомлений о выкупе с {since.strftime('%d.%m.%Y')} не найдено.")
        else:
            st.write(f"Найдено: **{len(notices)}**")
            for n in notices:
                already = is_processed(n.redemption_id or "")
                record = get_processed(n.redemption_id) if already else None

                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([4, 2, 2, 2])

                    title = f"**{n.name}**"
                    if already:
                        title += "  ✅ _обработано_"
                    c1.markdown(title)
                    c1.caption(f"ID: `{n.redemption_id}` · создано: {n.creation_time}")

                    # --- Скачать zip-архив из WB (реквизиты + sig + mchd) ---
                    zip_key = f"zip_{n.service_name}"
                    if c2.button("📄 Архив ВБ", key=f"fetch_{n.service_name}"):
                        with st.spinner("Скачиваю…"):
                            try:
                                fname, raw = client.download_document(n.service_name)
                                st.session_state[zip_key] = (fname, raw)
                            except Exception as e:
                                c2.error(str(e))
                    if zip_key in st.session_state:
                        fname, raw = st.session_state[zip_key]
                        c2.download_button(
                            "💾 Сохранить zip", raw, file_name=fname,
                            mime="application/zip", key=f"save_{n.service_name}",
                        )

                    # --- Сформировать УПД (или пересоздать) ---
                    btn_label = "🔁 Пересоздать УПД" if already else "🧾 Сформировать УПД"
                    if c3.button(btn_label, key=f"gen_{n.service_name}", type="primary"):
                        with st.spinner("Собираю УПД…"):
                            try:
                                path, summary = _generate_upd_for(n, client)
                                st.toast(f"✓ УПД {summary['number']} · {summary['items']} поз. · {summary['total']:.2f} ₽")
                                st.rerun()
                            except Exception as e:
                                c3.error(f"Ошибка: {e}")

                    # --- Скачать готовый XML УПД ---
                    if record:
                        xml_path = Path(record["xml_path"])
                        if xml_path.exists():
                            c4.download_button(
                                "📥 XML УПД",
                                xml_path.read_bytes(),
                                file_name=xml_path.name,
                                mime="application/xml",
                                key=f"dl_xml_{n.service_name}",
                                use_container_width=True,
                            )
                            c4.caption(
                                f"{record['items_count']} поз. · {record['total_sum']:.2f} ₽"
                            )
    else:
        st.warning("Сначала настройте доступ к WB API.")


# ═══════════ ВКЛАДКА ЖУРНАЛА ═════════════════════════════════════════════
with tab_log:
    st.header("Журнал обработанных документов")
    records = list_processed()
    if not records:
        st.info("Пока ничего не сгенерировано. Перейди на вкладку «Уведомления о выкупе».")
    else:
        existing = [r for r in records if Path(r["xml_path"]).exists()]
        c_info, c_dl = st.columns([3, 1])
        c_info.write(f"Всего записей: **{len(records)}** · файлов на диске: **{len(existing)}**")
        if existing:
            c_dl.download_button(
                f"📦 Скачать все ({len(existing)}) zip",
                data=_zip_all_upds(existing),
                file_name=f"УПД_все_{date.today().strftime('%Y-%m-%d')}.zip",
                mime="application/zip",
                use_container_width=True,
            )
        for r in records:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
                c1.markdown(f"**УПД №{r['upd_number']}**")
                c1.caption(f"Уведомление: {r['notice_name']}")
                c2.metric("Позиций", r['items_count'])
                c3.metric("Сумма, ₽", f"{r['total_sum']:.2f}")
                xml_path = Path(r['xml_path'])
                if xml_path.exists():
                    c4.download_button(
                        "📥",
                        xml_path.read_bytes(),
                        file_name=xml_path.name,
                        mime="application/xml",
                        key=f"dl_log_{r['redemption_id']}",
                        help="Скачать XML",
                    )
                st.caption(
                    f"Статус: `{r['status']}` · Сгенерировано: {r['processed_at']} · Файл: `{xml_path.name}`"
                )
