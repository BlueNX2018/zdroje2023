import imaplib
import email
import re
import unicodedata
from collections import Counter
from pathlib import Path
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from rapidfuzz import fuzz


st.set_page_config(page_title="Kontrola maili", layout="wide")

APP_VERSION = "2026-09-30-imap-smartfetch-cosmetic6"

IMAP_SERVER = "poczta.o2.pl"
IMAP_PORT = 993
MAILBOX = "Sent"
BASE_FILE = Path("baza_nazw_alias.csv")


IMAP_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
]


def format_imap_date(date_value):
    return f"{date_value.day:02d}-{IMAP_MONTHS[date_value.month - 1]}-{date_value.year}"


def bytes_to_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def response_contains_header(content):
    if not isinstance(content, bytes) or not content.strip():
        return False

    upper = content.upper()
    return (
        b"DATE:" in upper
        or b"SUBJECT:" in upper
        or b"TO:" in upper
        or b"FROM:" in upper
        or b"MESSAGE-ID:" in upper
    )


def extract_first_header_from_response(msg_data):
    if not msg_data:
        return b""

    for item in msg_data:
        if isinstance(item, tuple):
            content = item[1]
            if response_contains_header(content):
                return content

    return b""


def fetch_header_attempts(mail, uid):
    """
    Pobiera sam nagłówek wiadomości kilkoma metodami.
    Używane najpierw szybko dla wszystkich UID, a awaryjnie tylko dla brakujących.
    Nie pobiera załączników.
    """
    attempts = [
        ("RFC822.HEADER", "(UID RFC822.HEADER)"),
        ("BODY.PEEK[HEADER]", "(UID BODY.PEEK[HEADER])"),
        ("HEADER.FIELDS", "(UID BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)])"),
        ("PARTIAL 64KB", "(UID BODY.PEEK[]<0.65536>)"),
    ]

    last_status = ""
    last_method = ""
    last_error = ""

    for method_name, query in attempts:
        try:
            status, msg_data = mail.uid("FETCH", uid, query)
        except Exception as exc:
            last_error = str(exc)
            continue

        last_status = status
        last_method = method_name

        if status != "OK":
            continue

        header_bytes = extract_first_header_from_response(msg_data)
        if header_bytes:
            return header_bytes, last_status, method_name, ""

    return b"", last_status, last_method, last_error or "brak nagłówka"


def fetch_header_with_reconnect(uid, login, password):
    """
    Ostatnia próba dla problemowego UID: nowe połączenie IMAP i ponowny FETCH.
    Uruchamiana tylko dla wiadomości, które nie dały nagłówka w głównym połączeniu.
    """
    try:
        retry_mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        retry_mail.login(login, password)
        status, _ = retry_mail.select(MAILBOX, readonly=True)

        if status != "OK":
            retry_mail.logout()
            return b"", status, "reconnect select", "nie udało się otworzyć folderu"

        header_bytes, fetch_status, method, error = fetch_header_attempts(retry_mail, uid)
        retry_mail.logout()
        return header_bytes, fetch_status, f"reconnect: {method}", error

    except Exception as exc:
        return b"", "ERROR", "reconnect", str(exc)


def fetch_bodystructure(mail, uid):
    """
    Pobiera BODYSTRUCTURE dopiero dla wiadomości, które przeszły filtr daty/godziny.
    Dzięki temu nie spowalniamy sprawdzania wiadomości spoza zakresu.
    """
    try:
        status, msg_data = mail.uid("FETCH", uid, "(UID BODYSTRUCTURE)")
    except Exception as exc:
        return "", "ERROR", str(exc)

    if status != "OK":
        return "", status, ""

    parts = []

    for item in msg_data:
        if isinstance(item, tuple):
            meta, content = item
            parts.append(bytes_to_text(meta))
            parts.append(bytes_to_text(content))
        else:
            parts.append(bytes_to_text(item))

    return " ".join(parts), status, ""

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


def normalize_attachment_name(filename):
    """
    Normalizacja nazwy załącznika tylko do kontroli powtórzeń.
    Nie pobiera plików ani nie analizuje zawartości zdjęć.
    """
    if not filename:
        return ""

    return str(filename).strip().casefold()


def mark_duplicate_attachments(rows):
    """
    Oznacza wiadomości zawierające powtarzające się nazwy załączników.
    Sprawdzamy wyłącznie nazwy plików odczytane z BODYSTRUCTURE, bez pobierania załączników.

    Jeżeli powtórzenie nazwy załącznika wynika wyłącznie z tego, że ta sama
    wiadomość została wysłana drugi raz, nie pokazujemy osobnego ostrzeżenia
    o załączniku. Taki przypadek jest już opisany jako „Duplikat wiadomości”.
    """
    attachment_counts = Counter()
    attachment_display_names = {}
    attachment_occurrences = {}

    for row_index, row in enumerate(rows):
        for filename in row.get("_attachment_names", []):
            clean_name = str(filename).strip()
            key = normalize_attachment_name(clean_name)

            if not key:
                continue

            attachment_counts[key] += 1
            attachment_display_names.setdefault(key, clean_name)

            occurrence = {
                "RowIndex": row_index,
                "Godzina": row.get("Godzina", ""),
                "Temat": row.get("Temat", ""),
                "DuplicateMessageKey": row.get("_duplicate_message_key", ""),
            }
            attachment_occurrences.setdefault(key, []).append(occurrence)

    repeated_keys = {
        key
        for key, count in attachment_counts.items()
        if count > 1
    }

    actionable_duplicate_keys = set()

    for key in repeated_keys:
        occurrences = attachment_occurrences.get(key, [])
        duplicate_message_keys = {
            str(occurrence.get("DuplicateMessageKey", "")).strip()
            for occurrence in occurrences
            if str(occurrence.get("DuplicateMessageKey", "")).strip()
        }

        # Jeżeli wszystkie wystąpienia tej samej nazwy załącznika należą do jednej
        # grupy duplikatu wiadomości, to powtórzenie załącznika jest skutkiem
        # duplikatu wiadomości. Nie pokazujemy wtedy osobnego ostrzeżenia.
        caused_only_by_message_duplicate = (
            len(duplicate_message_keys) == 1
            and all(
                str(occurrence.get("DuplicateMessageKey", "")).strip() in duplicate_message_keys
                for occurrence in occurrences
            )
        )

        if not caused_only_by_message_duplicate:
            actionable_duplicate_keys.add(key)

    for row in rows:
        duplicate_names = []

        for filename in row.get("_attachment_names", []):
            clean_name = str(filename).strip()
            key = normalize_attachment_name(clean_name)

            if key in actionable_duplicate_keys and clean_name not in duplicate_names:
                duplicate_names.append(clean_name)

        row["Powtórzony załącznik"] = "TAK" if duplicate_names else "NIE"
        row["Powtórzone nazwy załączników"] = ", ".join(duplicate_names)

    duplicate_rows = []

    for key in sorted(actionable_duplicate_keys, key=lambda item: attachment_display_names[item].casefold()):
        occurrences = attachment_occurrences.get(key, [])

        hours = []
        subjects = []
        details = []

        for occurrence in occurrences:
            hour = str(occurrence.get("Godzina", "")).strip()
            subject = str(occurrence.get("Temat", "")).strip()
            if hour:
                hours.append(hour)

            if subject and subject not in subjects:
                subjects.append(subject)

            detail_parts = []
            if hour:
                detail_parts.append(hour)
            if subject:
                detail_parts.append(subject)
            if detail_parts:
                details.append(" — ".join(detail_parts))

        duplicate_rows.append({
            "Nazwa załącznika": attachment_display_names[key],
            "Liczba wystąpień": attachment_counts[key],
            "Godziny wysłania": ", ".join(hours),
            "Tematy wiadomości": "; ".join(subjects),
            "Wystąpienia szczegółowo": "; ".join(details),
        })

    return rows, pd.DataFrame(duplicate_rows)

def make_message_duplicate_key(row):
    """
    Tworzy klucz do wykrywania prawdopodobnie powtórnie wysłanej wiadomości.
    Nie pobieramy treści ani załączników; porównujemy tylko pola już odczytane przez IMAP.
    """
    recipients = normalize_text(row.get("Do", ""))
    subject = normalize_text(row.get("Temat", ""))

    attachment_names = row.get("_attachment_names", [])
    attachment_key = "|".join(
        sorted(
            normalize_attachment_name(name)
            for name in attachment_names
            if normalize_attachment_name(name)
        )
    )

    # Nie oznaczamy jako duplikatu pustych lub niemal pustych wiadomości,
    # żeby uniknąć fałszywych alarmów.
    if not subject and not attachment_key:
        return ""

    return f"{recipients}||{subject}||{attachment_key}"


def mark_duplicate_messages(rows):
    """
    Oznacza prawdopodobnie powtórzone wiadomości.
    Duplikat rozpoznajemy po zestawie: Do + Temat + lista nazw załączników.
    """
    message_counts = Counter()
    message_groups = {}

    for row in rows:
        key = make_message_duplicate_key(row)

        if not key:
            continue

        message_counts[key] += 1
        message_groups.setdefault(key, {
            "Do": row.get("Do", ""),
            "Temat": row.get("Temat", ""),
            "Załączniki": row.get("Załączniki", ""),
            "Godziny": [],
        })

        message_groups[key]["Godziny"].append(row.get("Godzina", ""))

    duplicate_keys = {
        key
        for key, count in message_counts.items()
        if count > 1
    }

    for row in rows:
        key = make_message_duplicate_key(row)

        if key in duplicate_keys:
            row["Podejrzenie duplikatu wiadomości"] = "TAK"
            row["Grupa duplikatu"] = row.get("Temat", "") or row.get("Załączniki", "")
            row["_duplicate_message_key"] = key
        else:
            row["Podejrzenie duplikatu wiadomości"] = "NIE"
            row["Grupa duplikatu"] = ""
            row["_duplicate_message_key"] = ""

    duplicate_rows = []

    for key in sorted(duplicate_keys, key=lambda item: message_groups[item]["Temat"].casefold()):
        group = message_groups[key]
        duplicate_rows.append({
            "Temat": group["Temat"],
            "Załączniki": group["Załączniki"],
            "Liczba wiadomości": message_counts[key],
            "Godziny": ", ".join(str(value) for value in group["Godziny"] if value),
        })

    return rows, pd.DataFrame(duplicate_rows)



def build_recipient_warning_df(rows):
    """
    Sprawdza, czy wszystkie pobrane wiadomości mają tego samego adresata.
    Adresat nie jest normalnie pokazywany w tabelach ostrzeżeń, bo powinien być stały.
    Jeżeli wystąpi więcej niż jeden adresat, zwracamy szczegóły do ostrzeżenia.
    """
    recipient_keys = set()
    details = []

    for row in rows:
        recipient = str(row.get("Do", "")).strip()
        recipient_key = normalize_text(recipient)

        if not recipient_key:
            recipient_key = "__empty__"

        recipient_keys.add(recipient_key)
        details.append({
            "Godzina": row.get("Godzina", ""),
            "Temat": row.get("Temat", ""),
            "Adresat": recipient or "(brak adresata)",
        })

    if len(recipient_keys) <= 1:
        return pd.DataFrame()

    return pd.DataFrame(details)



def format_occurrence_time_subject(time_value, subject_value):
    """Zwraca krótki opis wystąpienia: godzina — temat."""
    time_text = str(time_value or "").strip()
    subject_text = str(subject_value or "").strip()

    if time_text and subject_text:
        return f"{time_text} — {subject_text}"
    if time_text:
        return time_text
    if subject_text:
        return subject_text
    return ""


def build_warning_summary_df(
    duplicate_attachments_df,
    duplicate_messages_df,
    recipient_warning_df,
    debug_df,
):
    """
    Buduje jedną wspólną tabelę ostrzeżeń po analizie.
    Nie pokazujemy osobnych tabel dla załączników i duplikatów wiadomości.
    """
    warning_rows = []

    if duplicate_attachments_df is not None and not duplicate_attachments_df.empty:
        for _, row in duplicate_attachments_df.iterrows():
            file_name = str(row.get("Nazwa załącznika", "")).strip()
            count = row.get("Liczba wystąpień", "")
            occurrences = str(row.get("Wystąpienia szczegółowo", "")).strip()

            warning_rows.append({
                "Ocena": "Powtórzona nazwa załącznika",
                "Element": file_name,
                "Wystąpienia": occurrences,
                "Uwagi": f"Ta sama nazwa załącznika wystąpiła {count} razy. Sprawdź, czy nie wysłano omyłkowo tego samego zdjęcia.",
            })

    if duplicate_messages_df is not None and not duplicate_messages_df.empty:
        for _, row in duplicate_messages_df.iterrows():
            subject = str(row.get("Temat", "")).strip()
            attachments = str(row.get("Załączniki", "")).strip()
            hours_raw = str(row.get("Godziny", "")).strip()
            count = row.get("Liczba wiadomości", "")

            occurrences = []
            for hour in [part.strip() for part in hours_raw.split(",") if part.strip()]:
                occurrence = format_occurrence_time_subject(hour, subject)
                if occurrence:
                    occurrences.append(occurrence)

            element = subject or attachments or "(brak tematu)"

            warning_rows.append({
                "Ocena": "Duplikat wiadomości",
                "Element": element,
                "Wystąpienia": "; ".join(occurrences),
                "Uwagi": f"Ten sam temat i ten sam zestaw załączników wysłano {count} razy.",
            })

    if recipient_warning_df is not None and not recipient_warning_df.empty:
        occurrences = []
        for _, row in recipient_warning_df.iterrows():
            base = format_occurrence_time_subject(row.get("Godzina", ""), row.get("Temat", ""))
            recipient = str(row.get("Adresat", "")).strip()
            if base and recipient:
                occurrences.append(f"{base} — {recipient}")
            elif recipient:
                occurrences.append(recipient)
            elif base:
                occurrences.append(base)

        warning_rows.append({
            "Ocena": "Różny adresat",
            "Element": "Adresat wiadomości",
            "Wystąpienia": "; ".join(occurrences),
            "Uwagi": "W pobranych wiadomościach wykryto więcej niż jednego adresata. Adresat powinien być stały.",
        })

    if debug_df is not None and not debug_df.empty:
        for _, row in debug_df.iterrows():
            uid = str(row.get("UID", "")).strip()
            decision = str(row.get("Decyzja", "")).strip()
            problem = str(row.get("Problem", "")).strip()
            method = str(row.get("Metoda", "")).strip()

            warning_rows.append({
                "Ocena": "Problem IMAP",
                "Element": f"UID {uid}" if uid else "Wiadomość IMAP",
                "Wystąpienia": decision,
                "Uwagi": f"{problem} Metoda: {method}".strip(),
            })

    if not warning_rows:
        return pd.DataFrame()

    warning_df = pd.DataFrame(warning_rows)
    warning_df.insert(0, "Lp.", range(1, len(warning_df) + 1))
    return warning_df


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


title_col, version_col = st.columns([5, 1], vertical_alignment="bottom")

with title_col:
    st.subheader("Kontrola wysłanych wiadomości")

with version_col:
    st.markdown(
        f"""
        <div style="
            text-align:right;
            color:#777;
            font-size:11px;
            line-height:1;
            padding-bottom:6px;
            white-space:nowrap;
        ">
            v {APP_VERSION}
        </div>
        """,
        unsafe_allow_html=True,
    )

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


button_col, loading_col = st.columns([2, 6], vertical_alignment="center")

with button_col:
    pobierz_clicked = st.button("Pobierz wysłane wiadomości", use_container_width=True)

with loading_col:
    loading_placeholder = st.empty()

if pobierz_clicked:
    if not login or not haslo:
        st.warning("Podaj login i hasło.")
    elif start_time > end_time:
        st.error("Godzina początkowa nie może być późniejsza niż godzina końcowa.")
    else:
        try:
            loading_placeholder.markdown(
                """
                <div style="
                    color:#D0D4DC;
                    font-size:14px;
                    min-height:38px;
                    display:flex;
                    align-items:center;
                    line-height:1.3;
                    padding:0 0 0 2px;
                ">
                    Łączenie z o2 i pobieranie wiadomości...
                </div>
                """,
                unsafe_allow_html=True,
            )

            progress_placeholder = st.empty()
            status_placeholder = st.empty()

            mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            mail.login(login, haslo)

            status, _ = mail.select(MAILBOX, readonly=True)

            if status != "OK":
                st.error(f"Nie udało się otworzyć folderu: {MAILBOX}")
                mail.logout()
                st.stop()

            search_from = selected_date - timedelta(days=1)
            search_to = selected_date + timedelta(days=2)

            imap_from = format_imap_date(search_from)
            imap_to = format_imap_date(search_to)

            search_query = f'(SINCE {imap_from} BEFORE {imap_to})'
            status, data = mail.uid("SEARCH", None, search_query)

            if status != "OK":
                st.error("Nie udało się wyszukać wiadomości.")
                mail.logout()
                st.stop()

            message_ids = data[0].split()

            rows = []
            duplicate_attachments_df = pd.DataFrame()
            duplicate_messages_df = pd.DataFrame()
            technical_debug_rows = []
            skipped_by_date_or_time = 0

            if message_ids:
                progress = progress_placeholder.progress(0)

                status_placeholder.info(
                    f"Wyszukuję wiadomości z dnia {selected_date} "
                    f"w godzinach od {start_time.strftime('%H:%M')} do {end_time.strftime('%H:%M')}. "
                    f"Znalazłem {len(message_ids)} wiadomości. Analizuję."
                )

                for idx, uid in enumerate(message_ids, start=1):
                    progress.progress(idx / len(message_ids))

                    uid_text = bytes_to_text(uid)

                    header_bytes, fetch_status, fetch_method, fetch_error = fetch_header_attempts(mail, uid)

                    if not header_bytes:
                        header_bytes, fetch_status, fetch_method, fetch_error = fetch_header_with_reconnect(
                            uid,
                            login,
                            haslo,
                        )

                    if not header_bytes:
                        technical_debug_rows.append({
                            "UID": uid_text,
                            "Status FETCH": fetch_status,
                            "Metoda": fetch_method,
                            "Problem": fetch_error,
                            "Decyzja": "Pominięto: wiadomość nieodczytana przez IMAP",
                        })
                        continue

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
                        technical_debug_rows.append({
                            "UID": uid_text,
                            "Status FETCH": fetch_status,
                            "Metoda": fetch_method,
                            "Problem": f"Nie udało się odczytać daty z nagłówka: {date_raw}",
                            "Decyzja": "Pominięto: brak poprawnej daty/godziny",
                        })
                        continue

                    if msg_date != selected_date:
                        skipped_by_date_or_time += 1
                        continue

                    in_range = start_time <= msg_time <= end_time

                    if not in_range:
                        skipped_by_date_or_time += 1
                        continue

                    bodystructure_text, body_status, body_error = fetch_bodystructure(mail, uid)

                    if body_status != "OK":
                        technical_debug_rows.append({
                            "UID": uid_text,
                            "Status FETCH": body_status,
                            "Metoda": "BODYSTRUCTURE",
                            "Problem": body_error or "nie udało się pobrać BODYSTRUCTURE",
                            "Decyzja": "Wiadomość dodana, ale bez danych o załącznikach",
                        })

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
                        "_attachment_names": attachments,
                        # "Liczba rozpoznanych nazw załączników": len(attachments),
                    })

            debug_df = pd.DataFrame(technical_debug_rows) if technical_debug_rows else pd.DataFrame()

            status_placeholder.empty()
            progress_placeholder.empty()
            loading_placeholder.empty()

            mail.logout()
            if not rows:
                st.warning("Nie znaleziono wiadomości w wybranym zakresie godzin.")
            else:
                rows, duplicate_messages_df = mark_duplicate_messages(rows)
                rows, duplicate_attachments_df = mark_duplicate_attachments(rows)

                df = pd.DataFrame(rows)

                columns_to_hide_in_messages = [
                    "_attachment_names",
                    "Powtórzony załącznik",
                    "Powtórzone nazwy załączników",
                    "Podejrzenie duplikatu wiadomości",
                    "Grupa duplikatu",
                    "_duplicate_message_key",
                ]

                df = df.drop(
                    columns=[col for col in columns_to_hide_in_messages if col in df.columns]
                )

                df.insert(0, "Lp.", range(1, len(df) + 1))


                report_df = build_names_report(base_items, rows)

                report_display_columns = [
                    "Lp.",
                    "Nazwa wymagana",
                    "Alias",
                    "Status",
                    "Podobieństwo",
                    "Godzina",
                    "Dopasowano przez",
                    "Uwagi",
                ]

                report_display_df = report_df[report_display_columns].copy()
                report_display_df = report_display_df.rename(
                    columns={"Nazwa wymagana": "Pozycja z bazy"}
                )

                ok_count = (report_df["Status"] == "OK").sum()
                ok_alias_count = (report_df["Status"] == "OK alias").sum()
                ok_error_count = (report_df["Status"] == "OK z błędem").sum()
                review_count = (report_df["Status"] == "DO WERYFIKACJI").sum()
                missing_count = (report_df["Status"] == "BRAK").sum()

                total_count = len(report_df)

                missing_names = report_df.loc[
                    report_df["Status"] == "BRAK",
                    "Nazwa wymagana"
                ].tolist()

                if missing_names:
                    missing_text = ", ".join(missing_names)
                else:
                    missing_text = "brak"

                st.markdown(f"""
                <div style="width:100%; margin-top:10px; margin-bottom:10px; font-size:16px; font-weight:400;">
                    <div style="display:flex; width:100%; gap:6px; margin-bottom:6px;">
                        <div style="flex:1; box-sizing:border-box; background-color:#173A5E; color:#B8DCFF; padding:10px 12px; border-radius:6px; text-align:center;">
                            Łącznie: {total_count}
                        </div>
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
                    </div>
                    <div style="display:flex; width:100%; gap:6px;">
                        <div style="flex:1; box-sizing:border-box; background-color:#4A1F25; color:#FFB3B3; padding:10px 12px; border-radius:6px; text-align:center;">
                            BRAK: {missing_count}
                        </div>
                        <div style="flex:4; box-sizing:border-box; background-color:#4A1F25; color:#FFB3B3; padding:10px 14px; border-radius:6px; text-align:left;">
                            <strong>Braki:</strong> {missing_text}
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                recipient_warning_df = build_recipient_warning_df(rows)
                warning_df = build_warning_summary_df(
                    duplicate_attachments_df,
                    duplicate_messages_df,
                    recipient_warning_df,
                    debug_df,
                )

                warning_items = []
                if not recipient_warning_df.empty:
                    warning_items.append(f"różni adresaci wiadomości: {len(recipient_warning_df)} wpisów")
                if not duplicate_attachments_df.empty:
                    warning_items.append(f"powtórzone nazwy załączników: {len(duplicate_attachments_df)}")
                if not duplicate_messages_df.empty:
                    warning_items.append(f"podejrzenie powtórnie wysłanych wiadomości: {len(duplicate_messages_df)}")
                if not debug_df.empty:
                    warning_items.append(f"problemy techniczne IMAP: {len(debug_df)}")

                if warning_items:
                    warning_text = "; ".join(warning_items)
                    st.markdown(f"""
                    <div style="
                        width:100%;
                        box-sizing:border-box;
                        background-color:#4A3218;
                        color:#FFCF8A;
                        padding:12px 14px;
                        border-radius:6px;
                        text-align:left;
                        font-size:16px;
                        font-weight:400;
                        margin-top:6px;
                        margin-bottom:10px;
                    ">
                        <strong>Ostrzeżenia po analizie:</strong> {warning_text}
                    </div>
                    """, unsafe_allow_html=True)

                    with st.expander("Pokaż ostrzeżenia po analizie"):
                        st.dataframe(warning_df, use_container_width=True, hide_index=True)

                with st.expander("Pokaż wiadomości z wybranego zakresu"):
                    st.dataframe(df, use_container_width=True, hide_index=True)

                with st.expander("Pokaż raport zgodności z bazą nazw"):
                    st.dataframe(report_display_df, use_container_width=True, hide_index=True)


        except imaplib.IMAP4.error as e:
            st.error("Błąd logowania lub dostępu IMAP.")
            st.code(str(e))

        except Exception as e:
            st.error("Wystąpił nieoczekiwany błąd.")
            st.exception(e)
