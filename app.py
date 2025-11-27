import streamlit as st
import pandas as pd
from streamlit_gsheets import GSheetsConnection
from google.oauth2 import service_account
from googleapiclient.discovery import build
import datetime
import re
from zoneinfo import ZoneInfo

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
    Główna logika sprawdzania dostępności z uwzględnieniem strefy czasowej.
    """
    service = get_calendar_service()
    tz = ZoneInfo("Europe/Warsaw")
    
    # Upewniamy się, że mamy samą datę
    if isinstance(date_obj, datetime.datetime):
        d = date_obj.date()
    else:
        d = date_obj
    
    # Zakres czasu: od 00:00 do 23:59 czasu POLSKIEGO
    start_of_day = datetime.datetime.combine(d, datetime.time(0, 0), tzinfo=tz)
    end_of_day = datetime.datetime.combine(d, datetime.time(23, 59, 59), tzinfo=tz)

    # Konwersja na ISO format dla Google API
    time_min = start_of_day.isoformat()
    time_max = end_of_day.isoformat()

    print(f"DEBUG: Pobieram eventy od {time_min} do {time_max}")

    events_result = service.events().list(
        calendarId=CALENDAR_ID, 
        timeMin=time_min, 
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    
    events = events_result.get('items', [])
    
    available_hours = []
    my_booked_hours = []
    
    current_user_email = st.session_state.get('user_email', '').strip().lower()
    
    # Szukanie głównego wydarzenia
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
        return [], [] 

    all_slots = range(start_h, end_h)
    busy_hours = set()
    
    for event in events:
        if event['id'] == main_event['id']:
            continue 
            
        # Pobieranie godziny startu (API zwraca czas w ISO, np. 2025-11-27T13:00:00+01:00)
        start_str = event['start'].get('dateTime', event['start'].get('date'))
        
        if 'T' in start_str:
            # Parsujemy datę z uwzględnieniem strefy czasowej, żeby wyciągnąć poprawną godzinę lokalną
            dt_obj = datetime.datetime.fromisoformat(start_str)
            # Konwertujemy na strefę Warszawską (dla pewności)
            dt_warsaw = dt_obj.astimezone(tz)
            ev_hour = dt_warsaw.hour
        else:
            continue

        desc = event.get('description', '')
        
        # Parsowanie emaili
        emails = []
        if 'email:' in desc:
            clean_desc = desc.replace('email:', '')
            emails = [e.strip().lower() for e in clean_desc.split(',')]
            
        first_preacher = emails[0] if len(emails) > 0 else None
        
        if first_preacher != current_user_email:
            busy_hours.add(ev_hour)
        
        if current_user_email in emails:
            my_booked_hours.append(ev_hour)

    final_available = [h for h in all_slots if h not in busy_hours and h not in my_booked_hours]
    
    return final_available, my_booked_hours

def book_event(date_obj, hour, second_preacher_obj=None):
    """Tworzy lub aktualizuje wydarzenie w kalendarzu."""
    service = get_calendar_service()
    user_email = st.session_state['user_email']
    user_name = st.session_state['user_name']
    
    # Ustawiamy strefę czasową
    tz = ZoneInfo("Europe/Warsaw")
    
    # Tworzymy poprawne obiekty czasu ze strefą
    if isinstance(date_obj, datetime.datetime):
        d = date_obj.date()
    else:
        d = date_obj
        
    start_dt = datetime.datetime.combine(d, datetime.time(hour, 0), tzinfo=tz)
    end_dt = start_dt + datetime.timedelta(hours=1)
    
    # Przygotuj dane
    title = f"{user_name}"
    desc = f"email:{user_email}"
    
    if second_preacher_obj:
        sec_name = f"{second_preacher_obj['Imię']} {second_preacher_obj['Nazwisko']}"
        sec_email = second_preacher_obj['Email']
        title += f" i {sec_name}"
        desc += f", {sec_email}"

    # Budujemy zapytanie do API
    event_body = {
        'summary': title,
        'description': desc,
        # WAŻNE: Podajemy dateTime w ISO oraz jawnie timeZone
        'start': {
            'dateTime': start_dt.isoformat(), 
            'timeZone': 'Europe/Warsaw'
        },
        'end': {
            'dateTime': end_dt.isoformat(), 
            'timeZone': 'Europe/Warsaw'
        },
    }
    
    try:
        service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
        return True
    except Exception as e:
        print(f"Błąd zapisu: {e}")
        return False

def cancel_booking(date_obj, hour):
    """Usuwa użytkownika z wydarzenia lub usuwa całe wydarzenie."""
    service = get_calendar_service()
    user_email = st.session_state['user_email'].strip().lower()
    
    # Ustawiamy strefę czasową na Warszawę
    tz = ZoneInfo("Europe/Warsaw")
    
    # Jeśli date_obj jest typu datetime (ma godzinę 00:00), bierzemy samą datę
    if isinstance(date_obj, datetime.datetime):
        date_part = date_obj.date()
    else:
        date_part = date_obj
        
    # Tworzymy ramy czasowe z uwzględnieniem strefy
    start_dt = datetime.datetime.combine(date_part, datetime.time(hour, 0), tzinfo=tz)
    end_dt = start_dt + datetime.timedelta(hours=1)
    
    # Formatujemy do ISO (Google to zrozumie jako np. 13:00+01:00)
    time_min = start_dt.isoformat()
    time_max = end_dt.isoformat()
    
    print(f"DEBUG: Szukam wydarzeń od {time_min} do {time_max}")
    
    events = service.events().list(
        calendarId=CALENDAR_ID, timeMin=time_min, timeMax=time_max, singleEvents=True
    ).execute().get('items', [])
    
    for event in events:
        desc = event.get('description', '')
        print(f"DEBUG: Sprawdzam event '{event.get('summary')}' z opisem: {desc}")
        
        if 'email:' not in desc: 
            continue
        
        clean_desc = desc.replace('email:', '')
        # Czyścimy i normalizujemy emaile
        emails = [e.strip().lower() for e in clean_desc.split(',')]
        
        # Scenariusz 1: Jestem pierwszy -> Usuwam całe wydarzenie
        if len(emails) > 0 and emails[0] == user_email:
            print("DEBUG: Usuwam całe wydarzenie (jestem pierwszy)")
            service.events().delete(calendarId=CALENDAR_ID, eventId=event['id']).execute()
            return True
            
        # Scenariusz 2: Jestem drugi -> Usuwam siebie, pierwszy zostaje
        elif len(emails) > 1 and emails[1] == user_email:
            print("DEBUG: Usuwam siebie (jestem drugi)")
            # Nowy tytuł (bierzemy część przed " i ")
            current_title = event.get('summary', '')
            new_title = current_title.split(' i ')[0].strip()
            
            # Odtwarzamy opis tylko z pierwszym mailem (z oryginału, żeby zachować wielkość liter)
            original_emails = [e.strip() for e in desc.replace('email:', '').split(',')]
            new_desc = f"email:{original_emails[0]}"
            
            event['summary'] = new_title
            event['description'] = new_desc
            
            service.events().update(calendarId=CALENDAR_ID, eventId=event['id'], body=event).execute()
            return True
            
    print("DEBUG: Nie znaleziono pasującego wydarzenia (sprawdź czy email w opisie się zgadza).")
    return False

def get_user_events_for_month(year, month):
    """Pobiera listę dyżurów zalogowanego użytkownika na dany miesiąc."""
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
        st.markdown("## 👋 Witaj w systemie rezerwacji")
        st.info("⬅️ Aby rozpocząć, wybierz swoje nazwisko z listy w panelu po lewej stronie.")
        st.stop() # To zatrzymuje ładowanie reszty strony

    # Pobierz dane wybranego usera
    matching_users = df_users[df_users['Display'] == selected_user_display]
    
    if matching_users.empty:
        st.error("Błąd wyboru użytkownika.")
        st.stop()
        
    user_data = matching_users.iloc[0]
    
    st.session_state['user_email'] = user_data['Email']
    st.session_state['user_name'] = f"{user_data['Imię']} {user_data['Nazwisko']}"
    st.session_state['user_role'] = user_data['Rola']
    
    st.sidebar.success(f"Zalogowano: {st.session_state['user_name']}")
else:
    st.error("Nie udało się załadować listy użytkowników z Arkusza ACL.")
    st.stop()


# 2. MENU GŁÓWNE
menu = ["Nowe Zgłoszenie"]

allowed_roles = ['owner', 'writer', 'admin']
current_role = str(st.session_state.get('user_role', '')).strip().lower()

if current_role in allowed_roles:
    menu.append("Ustawienia")

choice = st.sidebar.radio("Menu", menu)

if choice == "Nowe Zgłoszenie":
    st.title("Wózki Ujeścisko – Wschód")
    st.markdown(f"Witaj, **{st.session_state['user_name']}** ({st.session_state['user_email']})")

    today = datetime.date.today()
    current_month_name = today.strftime("%B") # Nazwa miesiąca (po angielsku, ale ok)
    
    with st.expander(f"📅 Twoje dyżury w tym miesiącu ({today.month}/{today.year})", expanded=False):
        with st.spinner("Pobieram Twoje dyżury..."):
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
                    "Szczegóły (Kto)": st.column_config.TextColumn("Kto pełni dyżur", width="large"),
                }
            )
        else:
            st.info("Nie masz jeszcze żadnych zapisów w tym miesiącu.")
    
    # KROK 1: ZGODA i TYP
    with st.expander("📝 Formularz zgłoszeniowy", expanded=True):
        email_consent = st.checkbox(f"Zapisz {st.session_state['user_email']} jako adres e-mail dołączony do odpowiedzi.", value=True)
        
        request_type = st.radio("Rodzaj zgłoszenia", ["Zapis", "Rezygnacja"], horizontal=True)
        
    if not email_consent:
        st.warning("Wymagana jest zgoda na przetwarzanie adresu e-mail.")
        st.stop()

    # KROK 2: OBSŁUGA ZAPISU
    if request_type == "Zapis":
        st.subheader("📅 Zapis na dyżur")
        
        col1, col2 = st.columns(2)
        with col1:
            # Data
            selected_date = st.date_input("Wybierz datę", min_value=datetime.date.today())
        
        with col2:
            # Drugi głosiciel
            # Filtrujemy listę, żeby nie wybrać siebie
            other_users = df_users[df_users['Email'] != st.session_state['user_email']]
            second_preacher_name = st.selectbox("Drugi głosiciel (opcjonalnie)", ["Brak"] + list(other_users['Display']))

        # Pobieranie dostępnych godzin (Async logic handled by Streamlit rerun)
        if selected_date:
            with st.spinner("Sprawdzam grafik..."):
                # Konwersja na datetime
                d = datetime.datetime.combine(selected_date, datetime.time(0,0))
                available_hours, _ = get_slots_for_day(d)
            
            if not available_hours:
                st.warning("Brak wolnych terminów w tym dniu (lub brak dyżuru).")
            else:
                # Formatowanie godzin do wyboru
                hour_options = {h: f"{h}:00 - {h+1}:00" for h in available_hours}
                selected_hour = st.selectbox("Wybierz godzinę", options=list(hour_options.keys()), format_func=lambda x: hour_options[x])
                
                if st.button("✅ Zapisz się"):
                    with st.spinner("Zapisywanie..."):
                        # Znajdź dane drugiego głosiciela
                        sec_preacher_data = None
                        if second_preacher_name != "Brak":
                            sec_preacher_data = df_users[df_users['Display'] == second_preacher_name].iloc[0].to_dict()
                        
                        success = book_event(d, selected_hour, sec_preacher_data)
                        if success:
                            st.success("Pomyślnie dodano termin!")
                            st.balloons()
                        else:
                            st.error("Wystąpił błąd podczas zapisu.")

    # KROK 3: OBSŁUGA REZYGNACJI
    elif request_type == "Rezygnacja":
        st.subheader("🗑️ Rezygnacja z dyżuru")
        
        cancel_date = st.date_input("Wybierz datę, z której chcesz zrezygnować", min_value=datetime.date.today())
        
        if cancel_date:
            with st.spinner("Szukam Twoich dyżurów..."):
                d = datetime.datetime.combine(cancel_date, datetime.time(0,0))
                _, my_hours = get_slots_for_day(d)
            
            if not my_hours:
                st.info("Nie masz żadnych dyżurów w tym dniu.")
            else:
                hour_options = {h: f"{h}:00 - {h+1}:00" for h in my_hours}
                hour_to_cancel = st.selectbox("Wybierz godzinę do anulowania", options=list(hour_options.keys()), format_func=lambda x: hour_options[x])
                
                if st.button("🚫 Odwołaj dyżur"):
                    with st.spinner("Usuwanie..."):
                        success = cancel_booking(d, hour_to_cancel)
                        if success:
                            st.success("Odwołano dyżur.")
                            st.rerun()
                        else:
                            st.error("Nie udało się odwołać dyżuru.")

elif choice == "Ustawienia":
    if current_role != 'admin':
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