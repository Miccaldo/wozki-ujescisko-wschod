import streamlit as st
import pandas as pd
from streamlit_gsheets import GSheetsConnection
from google.oauth2 import service_account
from googleapiclient.discovery import build
import datetime
import re
from zoneinfo import ZoneInfo
import streamlit.components.v1 as components
import time
from streamlit_local_storage import LocalStorage

# --- KONFIGURACJA ---
st.set_page_config(page_title="Wózki Ujeścisko", page_icon="🛒", layout="centered")

# Pobranie ID z sekretów
CALENDAR_ID = st.secrets["calendar_id"]
SHEET_ID = st.secrets["sheet_id"] # Używane przez connection

print(CALENDAR_ID, SHEET_ID)

# --- STYLE CSS (Material Look) ---
st.markdown("""
    <style>
    .stButton>button {
        background-color: #5d3b87;
        color: white;
        width: 100%;
        border-radius: 8px;
        height: 3em;
    }
    .stButton>button:hover {
        background-color: #4c3170;
        color: white;
    }
    h1, h2, h3 { color: #5d3b87; }
    .block-container { padding-top: 2rem; }
    </style>
""", unsafe_allow_html=True)

# --- POŁĄCZENIE Z GOOGLE SHEETS (BAZA UŻYTKOWNIKÓW) ---
conn = st.connection("gsheets", type=GSheetsConnection)

def get_users_db():
    """Pobiera listę użytkowników z zakładki ACL."""
    try:
        # Pobieramy dane z Arkusza (zakładamy, że link jest w secrets.toml)
        # Czytamy zakładkę 'ACL' (lub pierwszą, jeśli nie podano)
        df = conn.read(worksheet="ACL", usecols=[0, 1, 2, 3, 4], ttl=60) 
        # Oczekiwane kolumny w arkuszu ACL: Email, Rola, Typ, Imię, Nazwisko
        print(df)
        return df
    except Exception as e:
        st.error(f"Błąd bazy danych: {e}")
        return pd.DataFrame()

def update_user_db(df):
    """Aktualizuje dane w zakładce ACL."""
    try:
        conn.update(worksheet="ACL", data=df)
        st.cache_data.clear()
        st.toast("Zapisano zmiany w bazie!", icon="✅")
    except Exception as e:
        st.error(f"Błąd zapisu: {e}")

# --- POŁĄCZENIE Z GOOGLE CALENDAR ---
def get_calendar_service():
    """Tworzy klienta API Kalendarza używając credentials z secrets.toml."""
    creds_dict = dict(st.secrets["connections"]["gsheets"])
    # Musimy dostosować format kluczy z TOML do formatu oczekiwanego przez google.auth
    # streamlit-gsheets używa trochę innych nazw kluczy niż standardowy json google
    # Ale zazwyczaj wystarczy przekazać to co jest.
    
    # Tworzymy obiekt Credentials
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    service = build('calendar', 'v3', credentials=creds)
    return service

# --- LOGIKA BIZNESOWA (Port z Google Apps Script) ---

def parse_hours_from_title(title):
    """Wyciąga godziny z tytułu wydarzenia cyklicznego np. '7:00-18:00'."""
    match = re.search(r'(\d{1,2}:\d{2})-(\d{1,2}:\d{2})', title)
    if match:
        return match.group(1), match.group(2)
    return None, None

def get_slots_for_day(date_obj):
    """
    Zwraca słownik: {godzina: 'status'}, gdzie status to 'Wolne' lub 'Dołącz do: Imię Nazwisko'.
    """
    service = get_calendar_service()
    tz = ZoneInfo("Europe/Warsaw")
    
    if isinstance(date_obj, datetime.datetime):
        d = date_obj.date()
    else:
        d = date_obj
    
    start_of_day = datetime.datetime.combine(d, datetime.time(0, 0), tzinfo=tz)
    end_of_day = datetime.datetime.combine(d, datetime.time(23, 59, 59), tzinfo=tz)

    events_result = service.events().list(
        calendarId=CALENDAR_ID, 
        timeMin=start_of_day.isoformat(), 
        timeMax=end_of_day.isoformat(),
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    
    events = events_result.get('items', [])
    
    # Znajdź zakres godzin z głównego wydarzenia (np. 7:00-18:00)
    main_event = None
    start_h, end_h = None, None

    for event in events:
        title = event.get('summary', '')
        s, e = parse_hours_from_title(title)
        if s and e:
            main_event = event
            start_h = int(s.split(':')[0])
            end_h = int(e.split(':')[0])
            break
    
    if not main_event:
        return {}, [] # Zwracamy pusty słownik zamiast listy

    all_slots = range(start_h, end_h)
    
    # Słownik dostępności: godzina -> opis (np. "Wolne" lub "Wolne miejsce (Jan Kowalski)")
    available_slots = {}
    my_booked_hours = []
    
    # Mapa zajętości godzin
    slot_occupancy = {h: [] for h in all_slots} # godzina -> lista uczestników (tytuły/imiona)

    current_user_email = st.session_state.get('user_email', '').strip().lower()

    for event in events:
        if event['id'] == main_event['id']:
            continue 
            
        start_str = event['start'].get('dateTime', event['start'].get('date'))
        if 'T' not in start_str: continue
        
        dt_obj = datetime.datetime.fromisoformat(start_str)
        dt_warsaw = dt_obj.astimezone(tz)
        ev_hour = dt_warsaw.hour
        
        if ev_hour not in all_slots:
            continue

        desc = event.get('description', '')
        title = event.get('summary', '')
        
        emails = []
        if 'email:' in desc:
            clean_desc = desc.replace('email:', '')
            emails = [e.strip().lower() for e in clean_desc.split(',')]
        
        # Sprawdzamy, czy ja tu jestem
        if current_user_email in emails:
            my_booked_hours.append(ev_hour)
            continue # Jeśli już tu jestem, nie mogę się dopisać

        # Jeśli mnie nie ma, sprawdzamy ilu jest innych
        count = len(emails)
        print(count)
        if count >= 2:
            # Slot pełny - oznaczamy jako None (niedostępny) w naszej mapie roboczej
            slot_occupancy[ev_hour] = "FULL"
        else:
            # Jest 1 osoba, zapisujemy kto to (żeby wyświetlić w selectboxie)
            # Tytuł eventu to zazwyczaj imię i nazwisko tej jednej osoby
            slot_occupancy[ev_hour] = title

    # Budujemy wynikowy słownik dostępnych godzin
    for h in all_slots:
        status = slot_occupancy[h]
        
        if h in my_booked_hours:
            continue # Nie pokazuj godzin, gdzie już jestem

        if status == "FULL":
            continue # Slot pełny
            
        if status == []:
            # Lista pusta = 0 osób
            available_slots[h] = "Wolne"
        else:
            # Jest tekst (imię osoby) = 1 osoba
            available_slots[h] = f"Dołącz do: {status}"
            
    return available_slots, my_booked_hours

def book_event(date_obj, hour, second_preacher_obj=None):
    """Tworzy nowe wydarzenie LUB aktualizuje istniejące (dopisuje się)."""
    service = get_calendar_service()
    user_email = st.session_state['user_email']
    user_name = st.session_state['user_name']
    tz = ZoneInfo("Europe/Warsaw")
    
    if isinstance(date_obj, datetime.datetime):
        d = date_obj.date()
    else:
        d = date_obj
        
    start_dt = datetime.datetime.combine(d, datetime.time(hour, 0), tzinfo=tz)
    end_dt = start_dt + datetime.timedelta(hours=1)
    
    # 1. Sprawdzamy, czy istnieje już wydarzenie w tej godzinie
    events_existing = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True
    ).execute().get('items', [])
    
    # Filtrujemy, aby pominąć ewentualne wydarzenie "Główne" (całodzienne), szukamy tylko slotów godzinowych
    # Zakładamy, że sloty mają konkretną godzinę startu
    target_event = None
    for ev in events_existing:
        # Pomijamy eventy bez opisu (zazwyczaj główne ramy czasowe) lub bez emaila
        if 'email:' in ev.get('description', ''):
             target_event = ev
             break
    
    # SCENARIUSZ A: Slot jest pusty -> TWORZYMY NOWE
    if not target_event:
        title = f"{user_name}"
        desc = f"email:{user_email}"
        
        if second_preacher_obj:
            sec_name = f"{second_preacher_obj['Imię']} {second_preacher_obj['Nazwisko']}"
            sec_email = second_preacher_obj['Email']
            title += f" i {sec_name}"
            desc += f", {sec_email}"

        event_body = {
            'summary': title,
            'description': desc,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Europe/Warsaw'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Europe/Warsaw'},
        }
        try:
            service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
            return True
        except Exception as e:
            print(f"Błąd insert: {e}")
            return False

    # SCENARIUSZ B: Ktoś już jest -> DOPISUJEMY SIĘ (UPDATE)
    else:
        # Zabezpieczenie: Jeśli próbujesz dopisać drugą osobę (second_preacher_obj), a slot jest zajęty w połowie
        # to robi się tłok (3 osoby). Blokujemy to lub ignorujemy drugiego kaznodzieję.
        if second_preacher_obj:
            st.error("Nie można dodać pary (2 osób) do slotu, w którym już ktoś jest. Wybierz pustą godzinę lub zapisz się sam.")
            return False

        current_desc = target_event.get('description', '')
        current_title = target_event.get('summary', '')
        
        # Sprawdzenie czy nie jest już pełny (na wszelki wypadek)
        emails = [e.strip() for e in current_desc.replace('email:', '').split(',')]
        if len(emails) >= 2:
            st.error("Ten termin został właśnie zajęty przez kogoś innego.")
            return False
            
        # Aktualizacja danych
        new_title = f"{current_title} i {user_name}"
        new_desc = f"{current_desc}, {user_email}"
        
        target_event['summary'] = new_title
        target_event['description'] = new_desc
        
        try:
            service.events().update(calendarId=CALENDAR_ID, eventId=target_event['id'], body=target_event).execute()
            return True
        except Exception as e:
            print(f"Błąd update: {e}")
            return False

def cancel_booking(date_obj, hour, delete_entirely=False):
    """
    Usuwa użytkownika z wydarzenia.
    delete_entirely: Jeśli True i użytkownik jest organizatorem, usuwa całe wydarzenie (nawet z partnerem).
    """
    service = get_calendar_service()
    user_email = st.session_state['user_email'].strip().lower()
    tz = ZoneInfo("Europe/Warsaw")
    
    if isinstance(date_obj, datetime.datetime):
        date_part = date_obj.date()
    else:
        date_part = date_obj
        
    start_dt = datetime.datetime.combine(date_part, datetime.time(hour, 0), tzinfo=tz)
    end_dt = start_dt + datetime.timedelta(hours=1)
    
    events = service.events().list(
        calendarId=CALENDAR_ID, 
        timeMin=start_dt.isoformat(), 
        timeMax=end_dt.isoformat(), 
        singleEvents=True
    ).execute().get('items', [])
    
    target_event = None
    for ev in events:
        if 'email:' in ev.get('description', ''):
             target_event = ev
             break
             
    if not target_event:
        print("DEBUG: Nie znaleziono wydarzenia do anulowania.")
        return False
        
    desc = target_event.get('description', '')
    title = target_event.get('summary', '')
    clean_desc = desc.replace('email:', '')
    emails = [e.strip().lower() for e in clean_desc.split(',')]
    
    if user_email not in emails:
        return False

    # --- SCENARIUSZ 1: Jestem ORGANIZATOREM (pierwszy na liście) ---
    if emails[0] == user_email:
        has_partner = len(emails) > 1
        
        # A. Jestem sam -> Zawsze usuwamy wydarzenie
        if not has_partner:
            service.events().delete(calendarId=CALENDAR_ID, eventId=target_event['id']).execute()
            return True
            
        # B. Mam partnera i wybrano opcję "Usuń całkowicie"
        if has_partner and delete_entirely:
            service.events().delete(calendarId=CALENDAR_ID, eventId=target_event['id']).execute()
            return True
            
        # C. Mam partnera, ale tylko ja rezygnuję (Partner przejmuje ster)
        if has_partner and not delete_entirely:
            # Partner (drugi mail) staje się jedynym/pierwszym
            new_desc = f"email:{emails[1]}"
            
            # Próba naprawy tytułu: "Ja i On" -> "On"
            # Zakładamy format "Imię Nazwisko i Imię Nazwisko"
            new_title = title
            if ' i ' in title:
                parts = title.split(' i ')
                # Bierzemy drugą część (imię partnera)
                if len(parts) > 1:
                    new_title = parts[1].strip()
            
            target_event['summary'] = new_title
            target_event['description'] = new_desc
            
            service.events().update(calendarId=CALENDAR_ID, eventId=target_event['id'], body=target_event).execute()
            return True

    # --- SCENARIUSZ 2: Jestem PARTNEREM (drugi na liście) ---
    elif len(emails) > 1 and emails[1] == user_email:
        # Usuwam tylko siebie, organizator zostaje
        new_desc = f"email:{emails[0]}"
        
        # Tytuł: "On i Ja" -> "On"
        new_title = title
        if ' i ' in title:
            new_title = title.split(' i ')[0].strip()
            
        target_event['summary'] = new_title
        target_event['description'] = new_desc
        
        service.events().update(calendarId=CALENDAR_ID, eventId=target_event['id'], body=target_event).execute()
        return True
            
    return False

def get_user_events_for_month(year, month):
    service = get_calendar_service()
    user_email = st.session_state['user_email'].strip().lower()
    tz = ZoneInfo("Europe/Warsaw")

    # Oblicz zakres dat: od 1. dnia miesiąca do 1. dnia kolejnego miesiąca
    start_date = datetime.datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    
    if month == 12:
        end_date = datetime.datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end_date = datetime.datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)

    time_min = start_date.isoformat()
    time_max = end_date.isoformat()

    events_result = service.events().list(
        calendarId=CALENDAR_ID, 
        timeMin=time_min, 
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    events = events_result.get('items', [])
    my_events = []

    for event in events:
        desc = event.get('description', '')
        if 'email:' not in desc: continue

        # Sprawdź czy user jest w tym wydarzeniu
        clean_desc = desc.replace('email:', '')
        emails = [e.strip().lower() for e in clean_desc.split(',')]

        if user_email in emails:
            # Pobierz datę i godzinę
            start_str = event['start'].get('dateTime')
            if not start_str: continue # Pomijamy całodniowe
            
            dt_obj = datetime.datetime.fromisoformat(start_str).astimezone(tz)
            date_str = dt_obj.strftime("%d-%m-%Y") # np. 27-11-2025
            time_str = f"{dt_obj.hour}:00 - {dt_obj.hour + 1}:00"

            # Wyciągnij nazwisko partnera z tytułu
            title = event.get('summary', '')
            partner = "Brak (Samodzielnie)"
            
            # Tytuł to zazwyczaj "Ja i Partner" lub "Partner i Ja"
            if ' i ' in title:
                parts = title.split(' i ')
                # Jeśli moje nazwisko jest pierwsze, partner jest drugi i odwrotnie
                # Ale prościej: po prostu bierzemy tę część, która NIE jest moim nazwiskiem (z grubsza)
                # Tutaj dla uproszczenia wyświetlamy cały tytuł, bo to czytelne:
                partner = title 
            else:
                # Jeśli jestem sam, tytuł to moje nazwisko. 
                partner = "Samodzielnie"

            my_events.append({
                "Data": date_str,
                "Godzina": time_str,
                "Szczegóły (Kto)": title
            })
            
    return pd.DataFrame(my_events)


def main():
    df_users = get_users_db()
    ls = LocalStorage()
    
    # 1. POBIERANIE UŻYTKOWNIKÓW
    df_users = get_users_db()
    
    if df_users.empty:
        st.error("Nie udało się załadować listy użytkowników.")
        st.stop()

    # 2. CICHY AUTOLOGIN (Silent Restore)
    # Sprawdzamy czy mamy coś w przeglądarce, ale w sesji Streamlit pusto
    stored_email = ls.getItem("wozki_stored_user")
    
    if stored_email and not st.session_state.get('user_email'):
        # Szukamy usera w bazie
        user_match = df_users[df_users['Email'] == stored_email]
        if not user_match.empty:
            found_user = user_match.iloc[0]
            # Ustawiamy sesję po cichu (bez toastów)
            st.session_state['user_email'] = found_user['Email']
            st.session_state['user_name'] = f"{found_user['Imię']} {found_user['Nazwisko']}"
            st.session_state['user_role'] = found_user['Rola']
            # Rerun jest konieczny, aby selectbox poniżej "zauważył", że ma ustawić index
            st.rerun()

    # 3. UI LOGOWANIA (SIDEBAR)
    st.sidebar.header("👤 Zaloguj się")
    
    # Ustalanie indexu selectboxa na podstawie sesji
    pre_selected_index = None
    if 'user_name' in st.session_state:
        current_display = st.session_state['user_name']
        unique_users = sorted(df_users['Display'].unique())
        try:
            pre_selected_index = unique_users.index(current_display)
        except ValueError:
            pre_selected_index = None

    selected_user_display = st.sidebar.selectbox(
        "Wybierz swoje nazwisko", 
        sorted(df_users['Display'].unique()), 
        index=pre_selected_index, 
        placeholder="Kliknij, aby wybrać..."
    )

    # 4. OBSŁUGA ZMIANY WYBORU
    if selected_user_display:
        matching_users = df_users[df_users['Display'] == selected_user_display]
        if not matching_users.empty:
            user_data = matching_users.iloc[0]
            new_email = user_data['Email']

            # Jeśli użytkownik fizycznie zmienił wybór w liście
            if st.session_state.get('user_email') != new_email:
                st.session_state['user_email'] = new_email
                st.session_state['user_name'] = f"{user_data['Imię']} {user_data['Nazwisko']}"
                st.session_state['user_role'] = user_data['Rola']
                
                # ZAPISUJEMY W LOCALSTORAGE
                ls.setItem("wuzki_user", new_email)
                
                # Toast tylko przy ręcznej zmianie
                st.toast(f"Zalogowano: {st.session_state['user_name']}", icon="✅")
                
                # Zamykanie sidebaru
                timestamp = int(time.time())
                js_close_sidebar = f"""
                <script>
                    setTimeout(function() {{
                        const sidebar = window.parent.document.querySelector('[data-testid="stSidebar"]');
                        const toggleBtn = window.parent.document.querySelector('[data-testid="stBaseButton-headerNoPadding"]');
                        if (sidebar && toggleBtn && sidebar.getAttribute('aria-expanded') === "true") {{
                            toggleBtn.click();
                        }}
                    }}, 200); 
                </script>
                """
                components.html(js_close_sidebar, height=0)

    # 5. OBSŁUGA CZYSZCZENIA (X)
    # Jeśli użytkownik usunął wybór, musimy wyczyścić też LocalStorage, 
    # żeby po odświeżeniu nie zalogowało go z powrotem.
    elif selected_user_display is None:
        if 'user_email' in st.session_state:
            del st.session_state['user_email']
            ls.deleteItem("wuzki_user") # Usuwamy z pamięci przeglądarki
            st.rerun()
        
        # Ekran powitalny dla niezalogowanych
        st.title("Służba na wózku - zapisy 📝")
        st.caption("Gdańsk Ujeścisko - Wschód")
        st.info("⬅️ Aby rozpocząć, wybierz swoje nazwisko z listy w panelu po lewej stronie.")
        st.stop()
    
    if not df_users.empty:
        # --- INTERFEJS UŻYTKOWNIKA (Streamlit) ---

        # 1. LOGOWANIE (SIDEBAR)
        df_users = get_users_db()

        if not df_users.empty:
            # --- CZYSZCZENIE DANYCH ---
            df_users = df_users.dropna(subset=['Imię', 'Nazwisko'])
            df_users['Imię'] = df_users['Imię'].astype(str)
            df_users['Nazwisko'] = df_users['Nazwisko'].astype(str)
            df_users = df_users[df_users['Imię'].str.strip() != '']
            # --------------------------

            df_users['Display'] = df_users['Imię'] + ' ' + df_users['Nazwisko']
            
            st.sidebar.header("👤 Zaloguj się")
            
            unique_users = sorted(df_users['Display'].unique())
            
            if not unique_users:
                st.error("Lista użytkowników jest pusta.")
                st.stop()

            # ZMIANA TUTAJ: index=None sprawia, że pole jest puste na starcie
            selected_user_display = st.sidebar.selectbox(
                "Wybierz swoje nazwisko", 
                unique_users, 
                index=None, 
                placeholder="Kliknij, aby wybrać..."
            )
            
            # Jeśli nic nie wybrano - zatrzymaj aplikację i pokaż instrukcję
            if selected_user_display is None:
                st.title("Służba przy wózku - zapisy 📝")
                st.caption("Gdańsk Ujeścisko - Wschód")
                st.info("⬅️ Aby rozpocząć, wybierz swoje nazwisko z listy w panelu po lewej stronie.")
                st.stop() # To zatrzymuje ładowanie reszty strony

            # Pobierz dane wybranego usera
            matching_users = df_users[df_users['Display'] == selected_user_display]
            
            if matching_users.empty:
                st.error("Błąd wyboru użytkownika.")
                st.stop()
                
            user_data = matching_users.iloc[0]

            new_email = user_data['Email']
            
            if st.session_state.get('user_email') != new_email:
                st.session_state['user_email'] = user_data['Email']
                st.session_state['user_name'] = f"{user_data['Imię']} {user_data['Nazwisko']}"
                st.session_state['user_role'] = user_data['Rola']
                
                st.toast(f"Zalogowano pomyślnie: {st.session_state['user_name']}", icon="✅")
                timestamp = int(time.time())

                close_sidebar_script = f"""
                <script>
                    
                    // Unikalny znacznik czasu: {timestamp}
                    // Wymusza na przeglądarce ponowne wykonanie tego bloku
                    
                    setTimeout(function() {{
                        const sidebar = window.parent.document.querySelector('[data-testid="stSidebar"]');
                        
                        // Szukamy przycisku zamykania (zależnie od wersji Streamlit może to być ten selektor)
                        const toggleBtn = window.parent.document.querySelector('[data-testid="stBaseButton-headerNoPadding"]');
                        
                        if (sidebar && toggleBtn) {{
                            // Jeśli sidebar jest otwarty ('true'), kliknij przycisk
                            if (sidebar.getAttribute('aria-expanded') === "true") {{
                                toggleBtn.click();
                            }}
                        }}
                    }}, 200); // Małe opóźnienie, by UI zdążyło się załadować
                </script>
                """
                components.html(close_sidebar_script, height=0)
        else:
            st.error("Nie udało się załadować listy użytkowników z Arkusza ACL.")
            st.stop()


        # 2. MENU GŁÓWNE
        menu = ["Nowe zgłoszenie"]

        allowed_roles = ['owner', 'writer', 'admin']
        current_role = str(st.session_state.get('user_role', '')).strip().lower()

        if current_role in allowed_roles:
            menu.append("Ustawienia")

        choice = st.sidebar.radio("Menu", menu)

        if choice == "Nowe zgłoszenie":
            st.title("Służba przy wózku - zapisy")
            st.markdown(f"Cześć, **{st.session_state['user_name']}** ({st.session_state['user_email']})")
            st.markdown("<br>", unsafe_allow_html=True)
            today = datetime.date.today()
            current_month_name = today.strftime("%B") # Nazwa miesiąca (po angielsku, ale ok)
            
            with st.expander(f"📅 Twoje zapisy na służbę przy wózku w tym miesiącu ({today.month}/{today.year})", expanded=False):
                with st.spinner("Pobieram Twoje terminy służby przy wózku..."):
                    df_my_events = get_user_events_for_month(today.year, today.month)
                
                if not df_my_events.empty:
                    # Wyświetlamy tabelę, ukrywając indeks (numerację wierszy 0,1,2...)
                    st.dataframe(
                        df_my_events, 
                        hide_index=True, 
                        use_container_width=True,
                        column_config={
                            "Data": st.column_config.TextColumn("Data", width="small"),
                            "Godzina": st.column_config.TextColumn("Godzina", width="small"),
                            "Szczegóły (Kto)": st.column_config.TextColumn("Kto pełni służbę", width="large"),
                        }
                    )
                else:
                    st.info("Nie masz jeszcze żadnych zapisów w tym miesiącu.")
            
            # KROK 1: ZGODA i TYP
            with st.expander("📝 Formularz zgłoszeniowy", expanded=True):
                request_type = st.radio("Rodzaj zgłoszenia", ["Zapis", "Rezygnacja"], horizontal=True)

            # KROK 2: OBSŁUGA ZAPISU
            if request_type == "Zapis":
                st.subheader("📅 Zapis na służbę przy wózku")
                
                col1, col2 = st.columns(2)
                with col1:
                    # Data
                    selected_date = st.date_input("Wybierz datę", min_value=datetime.date.today())
                
                with col2:
                    # Drugi głosiciel
                    other_users = df_users[df_users['Email'] != st.session_state['user_email']]
                    second_preacher_name = st.selectbox("Drugi głosiciel (opcjonalnie)", ["Brak"] + list(other_users['Display']))

                if selected_date:
                    # --- CACHING DANYCH DLA DATY ---
                    # Pobieramy dane z API tylko jeśli zmieniliśmy datę
                    if st.session_state.get('last_fetched_date') != selected_date:
                        with st.spinner("Sprawdzam grafik..."):
                            d = datetime.datetime.combine(selected_date, datetime.time(0,0))
                            # Teraz funkcja zwraca słownik {godzina: status}
                            fetched_slots, _ = get_slots_for_day(d)
                            
                            st.session_state['available_slots_cache'] = fetched_slots
                            st.session_state['last_fetched_date'] = selected_date
                    
                    # Pobieramy z cache
                    available_slots = st.session_state.get('available_slots_cache', {})
                    # -------------------------------

                    if not available_slots:
                        st.warning("Brak wolnych terminów w tym dniu")
                    else:
                        # Sortujemy godziny
                        sorted_hours = sorted(available_slots.keys())
                        
                        # Funkcja formatująca wyświetlanie w liście
                        def format_hour_label(h):
                            time_range = f"{h}:00 - {h+1}:00"
                            status = available_slots[h]
                            # Dodajemy ikonkę dla czytelności
                            if status == "Wolne":
                                return f"{time_range}  🟢 {status}"
                            else:
                                return f"{time_range}  🤝 {status}"

                        selected_hour = st.selectbox(
                            "Wybierz godzinę", 
                            options=sorted_hours, 
                            format_func=format_hour_label
                        )
                        
                        # Sprawdzamy status wybranej godziny
                        slot_status = available_slots[selected_hour]
                        is_joining_someone = "Dołącz do" in slot_status

                        # WALIDACJA: Nie można zapisać pary (Ty + Partner), jeśli ktoś już jest w slocie
                        can_proceed = True
                        
                        if is_joining_someone and second_preacher_name != "Brak":
                            st.error("⛔ Nie możesz zapisać drugiej osoby, ponieważ w tej godzinie jest już tylko 1 wolne miejsce.")
                            can_proceed = False
                        elif is_joining_someone:
                            st.info(f"ℹ️ Dołączasz do: {slot_status.replace('Dołącz do: ', '')}")

                        if st.button("✅ Zapisz się", disabled=not can_proceed):
                            with st.spinner("Zapisywanie..."):
                                d_for_booking = datetime.datetime.combine(selected_date, datetime.time(0,0))
                                
                                sec_preacher_data = None
                                if second_preacher_name != "Brak":
                                    sec_preacher_data = df_users[df_users['Display'] == second_preacher_name].iloc[0].to_dict()
                                
                                success = book_event(d_for_booking, selected_hour, sec_preacher_data)
                                
                                if success:
                                    st.success("Pomyślnie dodano termin!")
                                    st.balloons()
                                    # Czyścimy cache, żeby wymusić odświeżenie przy kolejnej akcji
                                    if 'last_fetched_date' in st.session_state:
                                        del st.session_state['last_fetched_date']
                                    time.sleep(1.5)
                                    st.rerun()
                                else:
                                    st.error("Wystąpił błąd. Być może ktoś zajął ten termin przed chwilą.")

            # KROK 3: OBSŁUGA REZYGNACJI
            elif request_type == "Rezygnacja":
                st.subheader("❌ Rezygnacja ze służby przy wózku")
                
                cancel_date = st.date_input("Wybierz datę, z której chcesz zrezygnować", min_value=datetime.date.today())
                
                if cancel_date:
                    # 1. Pobieramy moje godziny (bez zmian)
                    with st.spinner("Szukam Twoich terminów..."):
                        d = datetime.datetime.combine(cancel_date, datetime.time(0,0))
                        # Interesuje nas tylko druga wartość (my_booked_hours)
                        _, my_hours = get_slots_for_day(d)
                    
                    if not my_hours:
                        st.info("Nie masz żadnych dyżurów w tym dniu.")
                    else:
                        hour_options = {h: f"{h}:00 - {h+1}:00" for h in my_hours}
                        hour_to_cancel = st.selectbox(
                            "Wybierz godzinę do anulowania", 
                            options=list(hour_options.keys()), 
                            format_func=lambda x: hour_options[x]
                        )
                        
                        # --- NOWA LOGIKA: SPRAWDZANIE ROLI ---
                        # Musimy sprawdzić, czy user jest Organizatorem (nr 1) i czy jest ktoś jeszcze
                        # Robimy szybkie sprawdzenie on-the-fly dla wybranej godziny
                        
                        show_delete_all_option = False
                        
                        # Szybki lookup (pobieramy event jeszcze raz, ale tylko jeden konkretny zakres)
                        # To lekkie zapytanie, nie obciąży zbytnio
                        tz = ZoneInfo("Europe/Warsaw")
                        check_start = datetime.datetime.combine(cancel_date, datetime.time(hour_to_cancel, 0), tzinfo=tz)
                        check_end = check_start + datetime.timedelta(hours=1)
                        
                        service = get_calendar_service()
                        check_events = service.events().list(
                            calendarId=CALENDAR_ID, 
                            timeMin=check_start.isoformat(), 
                            timeMax=check_end.isoformat(), 
                            singleEvents=True
                        ).execute().get('items', [])
                        
                        current_event_title = ""
                        
                        for ev in check_events:
                            if 'email:' in ev.get('description', ''):
                                desc = ev.get('description', '')
                                emails = [e.strip().lower() for e in desc.replace('email:', '').split(',')]
                                current_event_title = ev.get('summary', '')
                                
                                # Czy jestem pierwszy na liście?
                                if emails and emails[0] == st.session_state['user_email']:
                                    # Czy jest ktoś drugi?
                                    if len(emails) > 1:
                                        show_delete_all_option = True
                                break
                        
                        # Checkbox sterujący
                        delete_entirely = False
                        if show_delete_all_option:
                            st.markdown(f"🗓️ *W tym terminie pełni z Tobą służbę druga osoba.*")
                            delete_entirely = st.checkbox(
                                "⚠️ Usuń całkowicie wydarzenie (odwołaj służbę również dla drugiej osoby)",
                                value=False,
                                help="Jeśli nie zaznaczysz tej opcji, wypiszesz się tylko Ty, a głosiciel pozostanie sam."
                            )
                        
                        if st.button("🚫 Odwołaj służbę"):
                            with st.spinner("Usuwanie..."):
                                # Przekazujemy nową flagę do funkcji backendowej
                                success = cancel_booking(d, hour_to_cancel, delete_entirely=delete_entirely)
                                
                                if success:
                                    if delete_entirely:
                                        st.success("Całe wydarzenie zostało usunięte.")
                                    else:
                                        st.success("Pomyślne wypisanie ze służby przy wózku.")
                                    
                                    time.sleep(1)
                                    st.rerun()
                                else:
                                    st.error("Nie udało się odwołać służby.")

        elif choice == "Ustawienia":
            if current_role not in allowed_roles: 
                st.error("⛔ Brak uprawnień do tej sekcji.")
                st.stop()

            st.title("🛠️ Lista Głosicieli (Baza)")
            
            # Proste uprawnienia - każdy może widzieć, ale edycja tylko jeśli rola to np. 'writer' lub 'owner'
            # Tu zakładamy, że każdy ma dostęp do podglądu
            
            if st.button("Odśwież dane"):
                st.cache_data.clear()
                st.rerun()
                
            # Edytor danych
            edited_df = st.data_editor(df_users, num_rows="dynamic")
            
            if st.button("Zapisz zmiany w bazie"):
                update_user_db(edited_df)
    else:
        st.error("Nie udało się załadować listy użytkowników.")

# Na samym dole pliku dodaj ten warunek:
if __name__ == "__main__":
    main()