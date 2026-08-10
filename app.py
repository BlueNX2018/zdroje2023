import imaplib
import email
import re
import unicodedata
from pathlib import Path
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from rapidfuzz import fuzz


st.set_page_config(page_title="Kontrola maili", layout="wide")

IMAP_SERVER = "poczta.o2.pl"
IMAP_PORT = 993
MAILBOX = "Sent"
BASE_FILE = Path("baza_nazw_alias.csv")


def decode_mime_header(value):
    if not value:
        return ""

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")

    parts_decoded = []

    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            try:
                decoded_part = part.decode(encoding or "utf-8", errors="replace")
            except Exception:
                decoded_part = part.decode("utf-8", errors="replace")
        else:
            decoded_part = str(part)

        parts_decoded.append(decoded_part)

    return "".join(parts_decoded)

def analyze_bodystructure(bodystructure_text):
    """
    Analizuje BODYSTRUCTURE bez pobierania załączników.
    Zwraca:
    - listę nazw plików, jeśli uda się je odczytać,
    - informację, czy mail ma załącznik,
    - informację, czy mail zawiera obraz.
    """
    if not bodystructure_text:
        return [], False, False

    text_upper = bodystructure_text.upper()

    has_attachment = (
        "ATTACHMENT" in text_upper
        or "FILENAME" in text_upper
        or "NAME" in text_upper
        or "IMAGE" in text_upper
        or "JPEG" in text_upper
        or "JPG" in text_upper
        or "PNG" in text_upper
        or "HEIC" in text_upper
        or "WEBP" in text_upper
    )

    has_image = (
        '"IMAGE"' in text_upper
        or "IMAGE/" in text_upper
        or '"JPEG"' in text_upper
        or '"JPG"' in text_upper
        or '"PNG"' in text_upper
        or '"HEIC"' in text_upper
        or '"WEBP"' in text_upper
    )

    filenames = []

    patterns = [
        r'"FILENAME"\s+"([^"]+)"',
        r'"NAME"\s+"([^"]+)"',
        r'FILENAME\*?=([^;\s\)]+)',
        r'NAME\*?=([^;\s\)]+)',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, bodystructure_text, flags=re.IGNORECASE)

        for match in matches:
            clean = match.strip().strip('"')
            clean = decode_mime_header(clean)

            if clean and clean not in filenames:
                filenames.append(clean)

    return filenames, has_attachment, has_image


def is_image_attachment(filename):
    filename = filename.lower()
    return filename.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp"))


def normalize_text(text):
    """
    Upraszcza tekst do porównań:
    - małe litery,
    - bez polskich znaków,
    - bez nadmiarowych spacji,
    - bez znaków specjalnych.
    """
    if not text:
        return ""

    text = str(text).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


def has_safe_token_match(expected_text, message_text, min_score=88):
    """
    Dodatkowy bezpiecznik dla fuzzy matching.

    Sprawdza, czy w tekście wiadomości istnieje słowo podobne do któregoś
    istotnego słowa z nazwy/aliasu.

    Warunek:
    - słowa muszą mieć podobny początek,
    - podobieństwo musi być wysokie.

    Dzięki temu:
    - Walentynowicz ≈ Walentynowycz -> TAK
    - Kamieńskiego ≈ Kaczyńskiego -> NIE
    """
    expected_tokens = [
        token for token in normalize_text(expected_text).split()
        if len(token) >= 5
    ]

    message_tokens = [
        token for token in normalize_text(message_text).split()
        if len(token) >= 5
    ]

    if not expected_tokens or not message_tokens:
        return False

    for expected in expected_tokens:
        for token in message_tokens:
            # Dla dłuższych słów wymagamy zgodnego początku.
            # To odcina błędne dopasowania typu Kamieńskiego/Kaczyńskiego.
            if expected[:3] != token[:3]:
                continue

            score = fuzz.ratio(expected, token)

            if score >= min_score:
                return True

    return False


def load_base_names():
    """
    Wczytuje stałą bazę nazw z pliku baza_nazw_alias.csv.

    Plik powinien mieć separator średnik:
    Nazwa;Alias

    Kolumna Nazwa jest obowiązkowa.
    Kolumna Alias jest opcjonalna.

    W kolumnie Alias można wpisać kilka aliasów oddzielonych średnikiem,
    np.:
    Nad Strzyżą; Strzyża; Wyspiańskiego
    """
    if not BASE_FILE.exists():
        st.error("Nie znaleziono pliku baza_nazw_alias.csv w folderze aplikacji.")
        st.stop()

    try:
        df_base = pd.read_csv(BASE_FILE, encoding="utf-8-sig", sep=";")
    except Exception:
        df_base = pd.read_csv(BASE_FILE, encoding="cp1250", sep=";")

    if "Nazwa" not in df_base.columns:
        st.error("Plik baza_nazw_alias.csv musi zawierać kolumnę o nazwie: Nazwa")
        st.stop()

    if "Alias" not in df_base.columns:
        df_base["Alias"] = ""

    base_items = []

    for _, row in df_base.iterrows():
        name = str(row.get("Nazwa", "")).strip()

        if not name or name.lower() == "nan":
            continue

        alias_raw = row.get("Alias", "")

        if pd.isna(alias_raw):
            alias_raw = ""

        aliases = [
            alias.strip()
            for alias in str(alias_raw).split(";")
            if alias.strip()
        ]

        base_items.append({
            "nazwa": name,
            "aliasy": aliases,
        })

    return base_items


def build_names_report(base_items, mail_rows):
    """
    Tworzy raport zgodności z bazą nazw.

    Sprawdza kolejno:
    1. dokładne wystąpienie pełnej nazwy,
    2. dokładne wystąpienie aliasu,
    3. podobieństwo pełnej nazwy przez rapidfuzz,
    4. podobieństwo aliasów przez rapidfuzz.

    Statusy:
    - OK
    - OK alias
    - OK z błędem
    - DO WERYFIKACJI
    - BRAK
    """
    report_rows = []
    searchable_messages = []

    for row in mail_rows:
        searchable_text = " ".join([
            row.get("Do", ""),
            row.get("Temat", ""),
            row.get("Załączniki", ""),
        ])

        searchable_messages.append({
            "normalized": normalize_text(searchable_text),
            "raw": searchable_text,
            "godzina": row.get("Godzina", ""),
            "temat": row.get("Temat", ""),
            "zalaczniki": row.get("Załączniki", ""),
        })

    for idx, item in enumerate(base_items, start=1):
        name = item["nazwa"]
        aliases = item.get("aliasy", [])

        normalized_name = normalize_text(name)

        normalized_alias_pairs = []
        for alias in aliases:
            alias_norm = normalize_text(alias)
            if alias_norm:
                normalized_alias_pairs.append((alias, alias_norm))

        best_score = 0
        best_msg = None
        best_match_text = ""
        exact_found = False
        alias_found = False
        used_alias = ""

        for msg in searchable_messages:
            msg_text = msg["normalized"]

            # 1. Dokładne wystąpienie pełnej nazwy
            if normalized_name and normalized_name in msg_text:
                exact_found = True
                best_score = 100
                best_msg = msg
                best_match_text = name
                break

            # 2. Dokładne wystąpienie aliasu
            for alias_raw, alias_norm in normalized_alias_pairs:
                if alias_norm and alias_norm in msg_text:
                    alias_found = True
                    best_score = 100
                    best_msg = msg
                    used_alias = alias_raw
                    best_match_text = alias_raw
                    break

            if alias_found:
                break

            # 3. Fuzzy matching po pełnej nazwie
            if normalized_name:
                score_partial = fuzz.partial_ratio(normalized_name, msg_text)
                score_token = fuzz.token_set_ratio(normalized_name, msg_text)
                score = max(score_partial, score_token)

                if score > best_score:
                    best_score = score
                    best_msg = msg
                    best_match_text = name

            # 4. Fuzzy matching po aliasach
            for alias_raw, alias_norm in normalized_alias_pairs:
                alias_score_partial = fuzz.partial_ratio(alias_norm, msg_text)
                alias_score_token = fuzz.token_set_ratio(alias_norm, msg_text)
                alias_score = max(alias_score_partial, alias_score_token)

                if alias_score > best_score:
                    best_score = alias_score
                    best_msg = msg
                    best_match_text = alias_raw

        if exact_found:
            status = "OK"
            uwagi = "Znaleziono pełną nazwę po normalizacji."
        elif alias_found:
            status = "OK alias"
            uwagi = f"Znaleziono dopuszczalny alias: {used_alias}"
        elif (
            best_score >= 90
            and best_msg
            and has_safe_token_match(best_match_text, best_msg["raw"], min_score=88)
        ):
            status = "OK z błędem"
            uwagi = (
                "Bardzo podobny zapis nazwy lub aliasu — prawdopodobnie literówka, "
                "skrót albo brak polskich znaków."
            )
        elif (
            best_score >= 75
            and best_msg
            and has_safe_token_match(best_match_text, best_msg["raw"], min_score=88)
        ):
            status = "DO WERYFIKACJI"
            uwagi = (
                "Znaleziono podobny zapis nazwy lub aliasu, ale wymaga ręcznego "
                "potwierdzenia."
            )
        else:
            status = "BRAK"
            uwagi = (
                "Nie znaleziono wiarygodnego dopasowania albo podobieństwo wynikało "
                "tylko z podobnej końcówki wyrazu."
            )

        if best_msg and status != "BRAK":
            found_time = best_msg["godzina"]
            found_subject = best_msg["temat"]
            found_attachments = best_msg["zalaczniki"]
            found_raw = best_msg["raw"]
        else:
            found_time = ""
            found_subject = ""
            found_attachments = ""
            found_raw = ""

        report_rows.append({
            "Lp.": idx,
            "Nazwa wymagana": name,
            "Alias": "; ".join(aliases),
            "Status": status,
            "Podobieństwo": round(best_score, 1),
            "Godzina": found_time,
            "Znaleziony tekst": found_raw,
            "Dopasowano przez": best_match_text,
            "Temat maila": found_subject,
            "Załączniki": found_attachments,
            "Uwagi": uwagi,
        })

    return pd.DataFrame(report_rows)


def set_morning_hours():
    st.session_state.start_time = time(4, 0)
    st.session_state.end_time = time(10, 0)


def set_evening_hours():
    st.session_state.start_time = time(16, 0)
    st.session_state.end_time = time(22, 0)


#st.title("Kontrola wysłanych wiadomości")
st.subheader("Kontrola wysłanych wiadomości")
st.write(
    "Pobieranie wiadomości z folderu wysłane, filtrowanie po godzinach "
    "i sprawdzanie bazy nazw z aliasami."
)

base_items = load_base_names()
st.info(f"Wczytano bazę nazw: {len(base_items)} pozycji.")

login_col, haslo_col = st.columns(2)

with login_col:
    login = st.text_input("Login do poczty o2", value="zdroje2023")

with haslo_col:
    haslo = st.text_input("Hasło do poczty o2", type="password")

# Domyślne wartości godzin w stanie aplikacji
if "start_time" not in st.session_state:
    st.session_state.start_time = time(4, 0)

if "end_time" not in st.session_state:
    st.session_state.end_time = time(10, 0)

hour_options = [time(hour, 0) for hour in range(24)]

col1, col2, col3, col4, col5 = st.columns(
    [2, 2, 2, 1, 1],
    vertical_alignment="bottom",
)

with col1:
    selected_date = st.date_input("Data kontroli")

with col2:
    start_time = st.selectbox(
        "Godzina od",
        options=hour_options,
        key="start_time",
        format_func=lambda t: t.strftime("%H:%M"),
    )

with col3:
    end_time = st.selectbox(
        "Godzina do",
        options=hour_options,
        key="end_time",
        format_func=lambda t: t.strftime("%H:%M"),
    )

with col4:
    st.button(
        "Rano",
        on_click=set_morning_hours,
        use_container_width=True,
    )

with col5:
    st.button(
        "Wieczór",
        on_click=set_evening_hours,
        use_container_width=True,
    )


if st.button("Pobierz wysłane wiadomości"):
    if not login or not haslo:
        st.warning("Podaj login i hasło.")
    elif start_time > end_time:
        st.error("Godzina początkowa nie może być późniejsza niż godzina końcowa.")
    else:
        try:
            with st.spinner("Łączenie z o2 i pobieranie wiadomości..."):
                mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
                mail.login(login, haslo)

                status, _ = mail.select(MAILBOX, readonly=True)

                if status != "OK":
                    st.error(f"Nie udało się otworzyć folderu: {MAILBOX}")
                    mail.logout()
                    st.stop()

                imap_date = selected_date.strftime("%d-%b-%Y")
                status, data = mail.search(None, f'ON "{imap_date}"')

                if status != "OK":
                    st.error("Nie udało się wyszukać wiadomości.")
                    mail.logout()
                    st.stop()

                message_ids = data[0].split()

                st.info(
                    f"Znaleziono wiadomości dla daty {selected_date}: {len(message_ids)}. "
                    f"Do tabeli trafią tylko wiadomości z godzin {start_time}–{end_time}."
                )

                rows = []

                if message_ids:
                    progress = st.progress(0)

                    for idx, num in enumerate(message_ids, start=1):
                        progress.progress(idx / len(message_ids))

                        fetch_query = (
                            b'(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)] BODYSTRUCTURE)'
                        )

                        status, msg_data = mail.fetch(num, fetch_query)

                        if status != "OK":
                            continue

                        header_bytes = b""
                        bodystructure_parts = []

                        for item in msg_data:
                            if isinstance(item, tuple):
                                meta = item[0]
                                content = item[1]

                                if b"BODY[HEADER.FIELDS" in meta.upper():
                                    header_bytes = content

                                try:
                                    bodystructure_parts.append(
                                        meta.decode("utf-8", errors="replace")
                                    )
                                except Exception:
                                    bodystructure_parts.append(str(meta))

                                try:
                                    bodystructure_parts.append(
                                        content.decode("utf-8", errors="replace")
                                    )
                                except Exception:
                                    bodystructure_parts.append(str(content))
                            else:
                                try:
                                    bodystructure_parts.append(
                                        item.decode("utf-8", errors="replace")
                                    )
                                except Exception:
                                    bodystructure_parts.append(str(item))

                        bodystructure_text = " ".join(bodystructure_parts)
                        msg = email.message_from_bytes(header_bytes)

                        subject = decode_mime_header(msg.get("Subject", ""))
                        sender = decode_mime_header(msg.get("From", ""))
                        recipients = decode_mime_header(msg.get("To", ""))
                        date_raw = msg.get("Date", "")

                        try:
                            dt = parsedate_to_datetime(date_raw)

                            warsaw_tz = ZoneInfo("Europe/Warsaw")

                            if dt.tzinfo is not None:
                                dt_local = dt.astimezone(warsaw_tz)
                            else:
                                dt_local = dt.replace(tzinfo=warsaw_tz)

                            msg_date = dt_local.date()
                            msg_time = dt_local.time().replace(microsecond=0)

                        except Exception:
                            msg_date = None
                            msg_time = None

                        if msg_time is None:
                            continue

                        in_range = start_time <= msg_time <= end_time

                        if not in_range:
                            continue

                        attachments, has_attachment, has_image = analyze_bodystructure(
                            bodystructure_text
                        )

                        image_attachments = [
                            a for a in attachments if is_image_attachment(a)
                        ]

                        if image_attachments:
                            has_image = True

                        if attachments:
                            has_attachment = True

                        rows.append({
                            "Data": str(msg_date) if msg_date else "",
                            "Godzina": str(msg_time) if msg_time else "",
                            # "Od": sender,
                            "Do": recipients,
                            "Temat": subject,
                            "Załącznik": "TAK" if has_attachment else "NIE",
                            "Zdjęcie": "TAK" if has_image else "NIE",
                            "Załączniki": ", ".join(attachments),
                            # "Liczba rozpoznanych nazw załączników": len(attachments),
                        })

                mail.logout()

            if not rows:
                st.warning("Nie znaleziono wiadomości w wybranym zakresie godzin.")
            else:
                df = pd.DataFrame(rows)
                df.insert(0, "Lp.", range(1, len(df) + 1))

                st.success(f"Pobrano wiadomości z wybranego zakresu godzin: {len(df)}")

                report_df = build_names_report(base_items, rows)

                ok_count = (report_df["Status"] == "OK").sum()
                ok_alias_count = (report_df["Status"] == "OK alias").sum()
                ok_error_count = (report_df["Status"] == "OK z błędem").sum()
                review_count = (report_df["Status"] == "DO WERYFIKACJI").sum()
                missing_count = (report_df["Status"] == "BRAK").sum()

                st.markdown(f"""
                <div style="display:flex; width:100%; gap:6px; margin-top:10px; margin-bottom:10px; font-size:16px; font-weight:400;">
                <div style="flex:1; box-sizing:border-box; background-color:#164B2A; color:#7CFF9B; padding:10px 12px; border-radius:6px; text-align:center;">
                    OK: {ok_count}
                </div>
                <div style="flex:1; box-sizing:border-box; background-color:#164B2A; color:#7CFF9B; padding:10px 12px; border-radius:6px; text-align:center;">
                    OK alias: {ok_alias_count}
                </div>
                <div style="flex:1; box-sizing:border-box; background-color:#4A3218; color:#FFCF8A; padding:10px 12px; border-radius:6px; text-align:center;">
                    OK z błędem: {ok_error_count}
                </div>
                <div style="flex:1; box-sizing:border-box; background-color:#2B3038; color:#D0D4DC; padding:10px 12px; border-radius:6px; text-align:center;">
                    DO WERYFIKACJI: {review_count}
                </div>
                <div style="flex:1; box-sizing:border-box; background-color:#4A1F25; color:#FFB3B3; padding:10px 12px; border-radius:6px; text-align:center;">
                    BRAK: {missing_count}
                </div>
                </div>
                """, unsafe_allow_html=True)

                missing_names = report_df.loc[
                    report_df["Status"] == "BRAK",
                    "Nazwa wymagana"
                ].tolist()

                if missing_names:
                    missing_text = ", ".join(missing_names)
                else:
                    missing_text = "brak"

                st.markdown(f"""
                <div style="
                    width:100%;
                    box-sizing:border-box;
                    background-color:#4A1F25;
                    color:#FFB3B3;
                    padding:12px 14px;
                    border-radius:6px;
                    text-align:left;
                    font-size:16px;
                    font-weight:400;
                    margin-top:6px;
                    margin-bottom:10px;
                ">
                    <strong>Braki:</strong> {missing_text}
                </div>
                """, unsafe_allow_html=True)

                with st.expander("Pokaż wiadomości z wybranego zakresu"):
                    st.dataframe(df, use_container_width=True)

                with st.expander("Pokaż raport zgodności z bazą nazw"):
                    st.dataframe(report_df, use_container_width=True)


        except imaplib.IMAP4.error as e:
            st.error("Błąd logowania lub dostępu IMAP.")
            st.code(str(e))

        except Exception as e:
            st.error("Wystąpił nieoczekiwany błąd.")
            st.code(str(e))
