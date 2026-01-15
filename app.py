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

st.set_page_config(page_title="Wózki Ujeścisko", page_icon="🛒", layout="centered")

CALENDAR_ID = st.secrets["calendar_id"]
SHEET_ID = st.secrets["sheet_id"]
STORAGE_USER = 'wozki_stored_user'
print(CALENDAR_ID, SHEET_ID)

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
    [data-testid="stHeaderActionElements"] {
        display: none !important;
    }
    h1, h2, h3 { color: #5d3b87; }
    .block-container { padding-top: 2rem;
    
    @media (max-width: 768px) {
        h1 {
            font-size: 1.8rem !important; /* Zmniejszamy tytuł */
        }
        h2 {
            font-size: 1.4rem !important; /* Zmniejszamy podtytuły */
        }
        h3 {
            font-size: 1.2rem !important;
        }
        /* Opcjonalnie: zmniejszamy padding, żeby na telefonie było więcej miejsca */
        .block-container {
            padding-top: 0.5rem;
            padding-left: 0.5rem;
            padding-right: 0.5rem;
        }
     }
    </style>
""", unsafe_allow_html=True)

conn = st.connection("gsheets", type=GSheetsConnection)

def get_users_db():
    try:
        df = conn.read(worksheet="ACL", usecols=[0, 1, 2, 3, 4], ttl=60)
        return df
    except Exception as e:
        st.error(f"Błąd bazy danych: {e}")
        return pd.DataFrame()

def update_user_db(df):
    try:
        conn.update(worksheet="ACL", data=df)
        st.cache_data.clear()
        st.toast("Zapisano zmiany w bazie!", icon="✅")
    except Exception as e:
        st.error(f"Błąd zapisu: {e}")

def get_calendar_service():
    """Tworzy klienta API Kalendarza używając credentials z secrets.toml."""
    creds_dict = dict(st.secrets["connections"]["gsheets"])
    
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    service = build('calendar', 'v3', credentials=creds)
    return service

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
        return {}, []

    all_slots = range(start_h, end_h)
    
    available_slots = {}
    my_booked_hours = []
    
    slot_occupancy = {h: [] for h in all_slots}

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
        
        if current_user_email in emails:
            my_booked_hours.append(ev_hour)
            continue

        count = len(emails)
        print(count)
        if count >= 2:
            slot_occupancy[ev_hour] = "FULL"
        else:
            slot_occupancy[ev_hour] = title

    for h in all_slots:
        status = slot_occupancy[h]
        
        if h in my_booked_hours:
            continue 

        if status == "FULL":
            continue 
            
        if status == []:
            available_slots[h] = "Wolne"
        else:
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
    
    events_existing = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True
    ).execute().get('items', [])
    
    target_event = None
    for ev in events_existing:
        if 'email:' in ev.get('description', ''):
             target_event = ev
             break

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

    else:
        if second_preacher_obj:
            st.error("Nie można dodać pary (2 osób) do slotu, w którym już ktoś jest. Wybierz pustą godzinę lub zapisz się sam.")
            return False

        current_desc = target_event.get('description', '')
        current_title = target_event.get('summary', '')
        
        emails = [e.strip() for e in current_desc.replace('email:', '').split(',')]
        if len(emails) >= 2:
            st.error("Ten termin został właśnie zajęty przez kogoś innego.")
            return False
            
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

    if emails[0] == user_email:
        has_partner = len(emails) > 1
        
        if not has_partner:
            service.events().delete(calendarId=CALENDAR_ID, eventId=target_event['id']).execute()
            return True
            
        if has_partner and delete_entirely:
            service.events().delete(calendarId=CALENDAR_ID, eventId=target_event['id']).execute()
            return True
            
        if has_partner and not delete_entirely:
            new_desc = f"email:{emails[1]}"
            
            new_title = title
            if ' i ' in title:
                parts = title.split(' i ')
                if len(parts) > 1:
                    new_title = parts[1].strip()
            
            target_event['summary'] = new_title
            target_event['description'] = new_desc
            
            service.events().update(calendarId=CALENDAR_ID, eventId=target_event['id'], body=target_event).execute()
            return True

    elif len(emails) > 1 and emails[1] == user_email:
        new_desc = f"email:{emails[0]}"

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

    # 1. Obliczamy koniec miesiąca (Początek następnego)
    if month == 12:
        end_date = datetime.datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end_date = datetime.datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)

    # 2. Obliczamy start (To jest ta zmiana)
    now = datetime.datetime.now(tz)
    
    # Jeśli sprawdzamy bieżący miesiąc i rok -> startujemy od DZISIAJ (od północy)
    if year == now.year and month == now.month:
        start_date = datetime.datetime(year, month, now.day, 0, 0, 0, tzinfo=tz)
    else:
        # W innym przypadku (np. przyszły miesiąc) startujemy od 1. dnia
        start_date = datetime.datetime(year, month, 1, 0, 0, 0, tzinfo=tz)

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

        clean_desc = desc.replace('email:', '')
        emails = [e.strip().lower() for e in clean_desc.split(',')]

        if user_email in emails:
            start_str = event['start'].get('dateTime')
            if not start_str: continue 
            
            dt_obj = datetime.datetime.fromisoformat(start_str).astimezone(tz)
            
            # Formatowanie daty i godziny
            date_str = dt_obj.strftime("%d-%m-%Y") 
            time_str = f"{dt_obj.hour}:00 - {dt_obj.hour + 1}:00"

            title = event.get('summary', '')
            
            display_info = title

            my_events.append({
                "Data": date_str,
                "Godzina": time_str,
                "Szczegóły (Kto)": display_info
            })
            
    return pd.DataFrame(my_events)

def load_users():
    df = get_users_db()
    if df.empty:
        return df

    df = df.dropna(subset=['Imię', 'Nazwisko'])
    df['Imię'] = df['Imię'].astype(str)
    df['Nazwisko'] = df['Nazwisko'].astype(str)
    return df


def main():
    ls = LocalStorage()
    
    # 1. POBIERANIE BAZY UŻYTKOWNIKÓW
    df_users = load_users()
    
    if df_users.empty:
        st.error("Nie udało się załadować listy użytkowników z Arkusza ACL.")
        st.stop()
        
    # --- CZYSZCZENIE DANYCH (Bez tworzenia kolumny Display) ---
    # Upewniamy się, że imię i nazwisko to stringi
    df_users['Imię'] = df_users['Imię'].astype(str).str.strip()
    df_users['Nazwisko'] = df_users['Nazwisko'].astype(str).str.strip()
    
    # Tworzymy pomocniczą listę stringów TYLKO do wyświetlania w UI
    # Nie dodajemy jej do df_users na stałe
    # Używamy zip, żeby iterować szybciej niż iterrows
    all_full_names = sorted([f"{i} {n}" for i, n in zip(df_users['Imię'], df_users['Nazwisko'])])

    # --- SILENT AUTO-LOGIN ---
    stored_email = ls.getItem(STORAGE_USER)
    
    if stored_email and not st.session_state.get('user_email'):
        user_match = df_users[df_users['Email'] == stored_email]
        if not user_match.empty:
            found_user = user_match.iloc[0]
            st.session_state['user_email'] = found_user['Email']
            # Tutaj też łączymy imię i nazwisko tylko na potrzeby sesji
            st.session_state['user_name'] = f"{found_user['Imię']} {found_user['Nazwisko']}"
            st.session_state['user_role'] = found_user['Rola']
            st.rerun()

    # --- SIDEBAR: LOGOWANIE ---
    
    pre_selected_index = None
    if 'user_name' in st.session_state:
        # user_name w sesji ma format "Imię Nazwisko"
        current_full_name = st.session_state['user_name']
        try:
            pre_selected_index = all_full_names.index(current_full_name)
        except ValueError:
            pre_selected_index = None

    st.sidebar.header("👤 Zaloguj się")
    
    selected_full_name = st.sidebar.selectbox(
        "Wybierz siebie z listy", 
        all_full_names, 
        index=pre_selected_index, 
        placeholder="Kliknij, aby wybrać..."
    )
    
    # OBSŁUGA WYBORU UŻYTKOWNIKA
    if selected_full_name:
        mask = (df_users['Imię'] + ' ' + df_users['Nazwisko']) == selected_full_name
        matching_users = df_users[mask]
        
        if not matching_users.empty:
            user_data = matching_users.iloc[0]
            new_email = user_data['Email']

            if st.session_state.get('user_email') != new_email:
                st.session_state['user_email'] = new_email
                st.session_state['user_name'] = f"{user_data['Imię']} {user_data['Nazwisko']}"
                st.session_state['user_role'] = user_data['Rola']
                
                if 'available_slots_cache' in st.session_state:
                    del st.session_state['available_slots_cache']
                if 'last_fetched_date' in st.session_state:
                    del st.session_state['last_fetched_date']

                ls.setItem(STORAGE_USER, new_email)
                st.toast(f"Zalogowano: {st.session_state['user_name']}", icon="✅")
                
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

                time.sleep(0.5) # Krótka pauza, żeby Toast zdążył mignąć
                st.rerun()


    # OBSŁUGA WYLOGOWANIA
    elif selected_full_name is None:
        if 'user_email' in st.session_state:
            del st.session_state['user_email']
            ls.deleteItem(STORAGE_USER)
            st.rerun()
            
        st.title("Służba przy wózku - zapisy 📝")
        st.caption("Gdańsk Ujeścisko - Wschód")
        st.info("⬅️ Aby rozpocząć, wybierz siebie z listy w panelu po lewej stronie.")
        st.stop()
    
    menu = ["Nowe zgłoszenie"]
    allowed_roles = ['owner', 'writer', 'admin']
    current_role = str(st.session_state.get('user_role', '')).strip().lower()

    if current_role in allowed_roles:
        menu.append("Ustawienia")

    choice = st.sidebar.radio("Menu", menu)

    if choice == "Nowe zgłoszenie":
        st.title("Służba przy wózku 📝")
        st.caption("Gdańsk Ujeścisko - Wschód")
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown(f"Cześć, **{st.session_state['user_name']}**")
        
        today = datetime.date.today()
        
        with st.expander(f"📅 Twoje zapisy w tym miesiącu ({today.month}/{today.year})", expanded=False):
            with st.spinner("Pobieram Twoje zapisy..."):
                df_my_events = get_user_events_for_month(today.year, today.month)
            
            if not df_my_events.empty:
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
        
        with st.expander("📝 Formularz zgłoszeniowy", expanded=True):
            st.selectbox("Lokalizacja", ["Piotrkowska"], index=0, disabled=True)
            request_type = st.radio("Rodzaj zgłoszenia", ["Zapis", "Rezygnacja"], horizontal=True)

        if request_type == "Zapis":
            st.subheader("📅 Zapis na służbę przy wózku")
            
            col1, col2 = st.columns(2)
            with col1:
                selected_date = st.date_input("Wybierz datę", min_value=datetime.date.today(), format="DD-MM-YYYY")
            
            with col2:
                other_users_df = df_users[df_users['Email'] != st.session_state['user_email']]
                other_users_names = sorted([f"{i} {n}" for i, n in zip(other_users_df['Imię'], other_users_df['Nazwisko'])])
                
                second_preacher_name = st.selectbox("Drugi głosiciel (opcjonalnie)", ["Brak"] + other_users_names)

            if selected_date:
                if st.session_state.get('last_fetched_date') != selected_date:
                    with st.spinner("Sprawdzam grafik..."):
                        d = datetime.datetime.combine(selected_date, datetime.time(0,0))
                        fetched_slots, _ = get_slots_for_day(d)
                        st.session_state['available_slots_cache'] = fetched_slots
                        st.session_state['last_fetched_date'] = selected_date
                
                available_slots = st.session_state.get('available_slots_cache', {})
                
                if not available_slots:
                    st.warning("Brak wolnych terminów w tym dniu")
                else:
                    sorted_hours = sorted(available_slots.keys())
                    
                    def format_hour_label(h):
                        time_range = f"{h}:00 - {h+1}:00"
                        status = available_slots[h]
                        if status == "Wolne":
                            return f"{time_range}  🟢 {status}"
                        else:
                            return f"{time_range}  🤝 {status}"

                    selected_hour = st.selectbox("Wybierz godzinę", options=sorted_hours, format_func=format_hour_label)
                    
                    slot_status = available_slots[selected_hour]
                    is_joining = "Dołącz do" in slot_status
                    can_proceed = True
                    
                    if is_joining and second_preacher_name != "Brak":
                        st.error("⛔ Nie możesz zapisać drugiej osoby, ponieważ w tej godzinie jest już tylko 1 wolne miejsce.")
                        can_proceed = False
                    elif is_joining:
                         st.info(f"ℹ️ Dołączasz do: {slot_status.replace('Dołącz do: ', '')}")

                    if st.button("✅ Zapisz się", disabled=not can_proceed):
                        with st.spinner("Zapisywanie..."):
                            d_booking = datetime.datetime.combine(selected_date, datetime.time(0,0))
                            
                            sec_data = None
                            if second_preacher_name != "Brak":
                                # Znajdujemy dane drugiego głosiciela (konstrukcja maski w locie)
                                mask_sec = (df_users['Imię'] + ' ' + df_users['Nazwisko']) == second_preacher_name
                                sec_match = df_users[mask_sec]
                                if not sec_match.empty:
                                    sec_data = sec_match.iloc[0].to_dict()
                            
                            success = book_event(d_booking, selected_hour, sec_data)
                            if success:
                                st.success("Pomyślnie zapisano!")
                                if 'last_fetched_date' in st.session_state:
                                    del st.session_state['last_fetched_date']
                                time.sleep(1.5)
                                st.rerun()
                            else:
                                st.error("Wystąpił błąd podczas zapisu.")

        elif request_type == "Rezygnacja":
            st.subheader("❌ Rezygnacja ze służby przy wózku")
            
            cancel_date = st.date_input("Wybierz datę, z której chcesz zrezygnować", min_value=datetime.date.today(), format="DD-MM-YYYY")
            
            if cancel_date:
                with st.spinner("Szukam Twoich terminów..."):
                    d = datetime.datetime.combine(cancel_date, datetime.time(0,0))
                    _, my_hours = get_slots_for_day(d)
                
                if not my_hours:
                    st.info("Nie masz żadnych terminów w tym dniu.")
                else:
                    hour_options = {h: f"{h}:00 - {h+1}:00" for h in my_hours}
                    hour_to_cancel = st.selectbox(
                        "Wybierz godzinę do anulowania", 
                        options=list(hour_options.keys()), 
                        format_func=lambda x: hour_options[x]
                    )
                    
                    show_delete_all_option = False
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
                    
                    for ev in check_events:
                        if 'email:' in ev.get('description', ''):
                            desc = ev.get('description', '')
                            emails = [e.strip().lower() for e in desc.replace('email:', '').split(',')]
                            if emails and emails[0] == st.session_state['user_email']:
                                if len(emails) > 1:
                                    show_delete_all_option = True
                            break
                    
                    delete_entirely = False
                    if show_delete_all_option:
                        st.markdown(f"🗓️ *W tym terminie pełni z Tobą służbę druga osoba.*")
                        delete_entirely = st.checkbox(
                            "⚠️ Usuń całkowicie wydarzenie (odwołaj służbę również dla drugiej osoby)",
                            value=False,
                            help="Jeśli zaznaczysz, całe wydarzenie zniknie."
                        )
                    
                    if st.button("⛔ Odwołaj służbę"):
                        with st.spinner("Usuwanie..."):
                            success = cancel_booking(d, hour_to_cancel, delete_entirely=delete_entirely)
                            if success:
                                if delete_entirely:
                                    st.success("Całe wydarzenie zostało usunięte.")
                                else:
                                    st.success("Odwołano służbę przy wózku.")
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.error("Nie udało się odwołać służby przy wózku.")

    elif choice == "Ustawienia":
        if current_role not in allowed_roles:
            st.error("⛔ Brak uprawnień do tej sekcji.")
            st.stop()

        st.title("🛠️ Lista głosicieli")
        
        if st.button("Odśwież dane", icon=":material/sync:"):
            st.cache_data.clear()
            st.rerun()
            
        edited_df = st.data_editor(df_users, num_rows="dynamic")
        
        if st.button("Zapisz zmiany w bazie"):
            update_user_db(edited_df)

if __name__ == "__main__":
    main()