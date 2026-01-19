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
import smtplib
from email.message import EmailMessage
import re

st.set_page_config(page_title="Służba przy wózku", page_icon="👥", layout="centered")

CALENDAR_ID = st.secrets["calendar_id"]
SHEET_ID = st.secrets["sheet_id"]
STORAGE_USER = 'wozki_stored_user'
print(CALENDAR_ID, SHEET_ID)

st.markdown("""
    <style>

/* Główny header aplikacji */
[data-testid="stAppHeader"] {
    display: none;
}

/* Ukrywa status ładowania (ludzik / pływający gif) */
[data-testid="stStatusWidget"] {
    display: none !important;
}

/* Ukrywa branding Streamlit w prawym górnym rogu */
[data-testid="stDecoration"] {
    display: none !important;
}

/* Ukrywa branding Streamlit w prawym górnym rogu */
[data-testid="stMainMenu"] {
    display: none !important;
}



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
    .block-container { padding-top: 2rem; }
    
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
            padding-left: 1rem;
            padding-right: 1rem;
        }
     }
    </style>
""", unsafe_allow_html=True)

components.html("""
<script>
window.addEventListener('load', function() {
    // Ukrywa header
    const header = document.querySelector('[data-testid="stAppHeader"]');
    if(header) { header.style.display = 'none'; }

    // Ukrywa status / ludzik
    const status = document.querySelector('[data-testid="stStatusWidget"]');
    if(status) { status.style.display = 'none'; }

    // Ukrywa czarny Manage App
    const manageBtn = document.querySelector('button[aria-label="Manage app"]');
    if(manageBtn) { manageBtn.style.display = 'none'; }

    // Ukrywa branding w prawym górnym rogu
    const brand = document.querySelector('[data-testid="stDecoration"]');
    if(brand) { brand.style.display = 'none'; }
});
</script>
""", height=0)

conn = st.connection("gsheets", type=GSheetsConnection)

def get_users_db():
    try:
        # Zmieniamy usecols na [0, 1, 2, 3, 4, 5, 6] (doszło Ulubione)
        df = conn.read(worksheet="ACL", usecols=[0, 1, 2, 3, 4, 5, 6], ttl=60)

        df['Imię'] = df['Imię'].astype(str).str.strip()
        df['Nazwisko'] = df['Nazwisko'].astype(str).str.strip()
        
        if 'Płeć' not in df.columns: df['Płeć'] = 'M'
        else: df['Płeć'] = df['Płeć'].fillna('M').astype(str).str.upper().str.strip()

        # Obsługa kolumny Ulubione
        if 'Ulubione' not in df.columns:
            df['Ulubione'] = ''
        else:
            df['Ulubione'] = df['Ulubione'].fillna('').astype(str)

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
        else:
            slot_occupancy[ev_hour] = "FULL"
            continue
        
        if current_user_email in emails:
            my_booked_hours.append(ev_hour)
            continue

        count = len(emails)
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

    gender = st.session_state.get('user_gender', 'M')

    verb_signed = "zapisała" if gender == "K" else "zapisał"
    verb_joined = "dołączyła" if gender == "K" else "dołączył"

    style_b = 'style="color: #000000; font-weight: bold;"'
    
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

            if second_preacher_obj:
                subj = "Służba przy wózku - Nowy termin"
                body = (f"Cześć!\n\n"
                        f"{user_name} {verb_signed} Ciebie do współpracy.\n"
                        f"Data: <b {style_b}>{d.strftime('%d-%m-%Y')}</b>\n"
                        f"Godzina: <b {style_b}>{hour}:00 - {hour+1}:00</b>\n\n"
                        f"Do zobaczenia!")
                send_notification_email(second_preacher_obj['Email'], subj, body)
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
        organizer_email = current_desc.replace('email:', '').split(',')[0].strip()
        
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
            if organizer_email:
                subj = "Służba przy wózku - Ktoś dołączył!"
                body = (f"Cześć!\n\n"
                        f"{user_name} {verb_joined} do Ciebie do współpracy.\n"
                        f"Data: <b {style_b}>{d.strftime('%d-%m-%Y')}</b>\n"
                        f"Godzina: <b {style_b}>{hour}:00 - {hour+1}:00</b>\n\n"
                        f"Do zobaczenia!")
                send_notification_email(organizer_email, subj, body)
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
    user_name = st.session_state['user_name']
    gender = st.session_state.get('user_gender', 'M')

    verb_canceled = "odwołała" if gender == "K" else "odwołał"
    verb_unsigned = "wypisała" if gender == "K" else "wypisał"

    style_b = 'style="color: #000000; font-weight: bold;"'

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
            
            partner_email = emails[1]
            subj = "Służba przy wózku - Odwołano termin"
            body = (f"Cześć,\n\n"
                    f"{user_name} {verb_canceled} Waszą służbę przy wózku.\n"
                    f"Data: <b {style_b}>{date_part.strftime('%d-%m-%Y')}</b>\n"
                    f"Godzina: <b {style_b}>{hour}:00 - {hour+1}:00</b>\n\n"
                    f"Termin został usunięty z grafiku.")
            send_notification_email(partner_email, subj, body)
            
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
            
            partner_email = emails[1]
            subj = "Służba na wózku - Zmiana w grafiku"
            body = (f"Cześć,\n\n"
                    f"{user_name} {verb_unsigned} się z Waszego terminu.\n"
                    f"Data: <b {style_b}>{date_part.strftime('%d-%m-%Y')}</b>\n"
                    f"Godzina: <b {style_b}>{hour}:00 - {hour+1}:00</b>\n\n"
                    f"Twój termin jest otwarty na współpracę z innym głosicielem.")
            send_notification_email(partner_email, subj, body)

            return True

    elif len(emails) > 1 and emails[1] == user_email:
        new_desc = f"email:{emails[0]}"

        new_title = title
        if ' i ' in title:
            new_title = title.split(' i ')[0].strip()
            
        target_event['summary'] = new_title
        target_event['description'] = new_desc
        
        service.events().update(calendarId=CALENDAR_ID, eventId=target_event['id'], body=target_event).execute()
        
        organizer_email = emails[0]
        subj = "Służba na wózku - Zmiana w grafiku"
        body = (f"Cześć,\n\n"
                f"{user_name} {verb_unsigned} się z Waszego terminu.\n"
                f"Data: <b {style_b}>{date_part.strftime('%d-%m-%Y')}</b>\n"
                f"Godzina: <b {style_b}>{hour}:00 - {hour+1}:00</b>\n\n"
                f"Twój termin jest otwarty na współpracę z innym głosicielem.")
        send_notification_email(organizer_email, subj, body)
        
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


def send_notification_email(to_email, subject, body):
    """Wysyła e-mail HTML używając SMTP Gmaila."""
    try:
        sender = st.secrets["email"]["sender_address"]
        password = st.secrets["email"]["app_password"]
        server_host = st.secrets["email"]["smtp_server"]
        port = st.secrets["email"]["smtp_port"]

        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = to_email

        # 1. Wersja tekstowa (dla starych klientów poczty)
        msg.set_content(body)

        # 2. Wersja HTML (Ładna)
        # Zamieniamy znaki nowej linii \n na <br> dla HTML
        html_body = body.replace('\n', '<br>')
        
        html_template = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333333; margin: 0; padding: 0;">
            <div style="max-width: 600px; margin: 20px auto; border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 5px rgba(0,0,0,0.1);">
              
              <!-- NAGŁÓWEK -->
              <div style="background-color: #5d3b87; padding: 20px; text-align: center;">
                <h2 style="color: #ffffff; margin: 0; font-size: 24px;">Gdańsk Ujeścisko - Wschód</h2>
              </div>
              
              <!-- TREŚĆ -->
              <div style="padding: 30px 20px; background-color: #ffffff;">
                <h3 style="color: #5d3b87; margin-top: 0;">{subject}</h3>
                <p style="font-size: 16px; line-height: 1.6; color: #555555;">
                  {html_body}
                </p>
              </div>
              
              <!-- STOPKA -->
              <div style="background-color: #f8f9fa; padding: 15px; text-align: center; font-size: 12px; color: #888888; border-top: 1px solid #eeeeee;">
                <p style="margin: 0;">Wiadomość wygenerowana automatycznie przez aplikację do zapisów zboru Gdańsk Ujeścisko-Wschód.</p>
              </div>
              
            </div>
          </body>
        </html>
        """

        # Dodajemy wersję HTML jako alternatywę
        msg.add_alternative(html_template, subtype='html')

        with smtplib.SMTP_SSL(server_host, port) as server:
            server.login(sender, password)
            server.send_message(msg)
            print(f"E-mail wysłany do {to_email}")
            return True
            
    except Exception as e:
        print(f"Błąd wysyłania e-maila: {e}")
        return False


def main():
    ls = LocalStorage()
    
    # 1. POBIERANIE BAZY UŻYTKOWNIKÓW
    df_users = load_users()
    
    if df_users.empty:
        st.error("Nie udało się załadować listy użytkowników z Arkusza ACL.")
        st.stop()

    components.html("""
    <script>
        function disableKeyboardOnMobile() {
            const inputs = window.parent.document.querySelectorAll('div[data-baseweb="base-input"] input');
            inputs.forEach(input => {
                input.setAttribute('readonly', 'readonly');
                input.setAttribute('inputmode', 'none');
            });
        }
        setInterval(disableKeyboardOnMobile, 500);
    </script>
    """, height=0)

    components.html("""
    <script>
        function disableKeyboardOnMobile() {
            const inputs = window.parent.document.querySelectorAll('div[data-baseweb="select"] input');
            inputs.forEach(input => {
                input.setAttribute('readonly', 'readonly');
                input.setAttribute('inputmode', 'none');
            });
        }
        setInterval(disableKeyboardOnMobile, 500);
    </script>
    """, height=0)
        
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
            st.session_state['user_gender'] = found_user.get('Płeć', 'M')
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
                st.session_state['user_gender'] = user_data.get('Płeć', 'M')
                
                if 'available_slots_cache' in st.session_state:
                    del st.session_state['available_slots_cache']
                if 'last_fetched_date' in st.session_state:
                    del st.session_state['last_fetched_date']

                st.session_state['request_type_radio'] = "Zapis"
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
            
        st.title("Służba przy wózku 📝")
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
        
        with st.expander(f"📅 Twoje zapisy w tym miesiącu", expanded=False):
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
            request_type = st.radio("Rodzaj zgłoszenia", ["Zapis", "Rezygnacja"], horizontal=True, key="request_type_radio")

        if request_type == "Zapis":
            st.subheader("📅 Zapis na służbę przy wózku")

            # --- DEFINICJE SVG (Data URI) ---
            # 1. SZARE PUSTE (Brak)
            icon_empty_grey = "data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%239ca3af' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z'%3E%3C/path%3E%3C/svg%3E"
            
            # 2. FIOLETOWE PUSTE (Hover)
            icon_empty_purple = "data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%235d3b87' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z'%3E%3C/path%3E%3C/svg%3E"
            
            # 3. FIOLETOWE PEŁNE (Ulubione)
            icon_filled_purple = "data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%235d3b87' stroke='%235d3b87' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z'%3E%3C/path%3E%3C/svg%3E"

            st.markdown(f"""
            <style>
            /* 
               1. SELEKTOR IZOLOWANY 
               Działa TYLKO na przycisk, który jest w kolumnie obok znacznika #heart-marker.
               Nie zepsuje innych przycisków w aplikacji.
            */
            div[data-testid="stColumn"]:has(span#heart-marker) button {{
                border: none !important;
                background-color: transparent !important;
                box-shadow: none !important;
                padding: 0 !important;
                margin: 0 !important;
                height: 100%;
                width: 100%;
                min-height: 42px;
                
                /* Tło SVG */
                background-repeat: no-repeat !important;
                background-position: center !important;
                /* Zmniejszone do 22px, żeby nie ucinało po bokach */
                background-size: 22px 22px !important; 
            }}
            
            /* Ukrywamy tekst wewnątrz TEGO KONKRETNEGO przycisku */
            div[data-testid="stColumn"]:has(span#heart-marker) button p {{
                display: none !important;
            }}

            /* --- LOGIKA STANÓW DLA SERCA --- */

            div[data-testid="stElementContainer"]{{
                min-width: 1.375rem;
            }}
            /* STAN: BRAK (type="secondary") */
            div[data-testid="stColumn"]:has(span#heart-marker) button[kind="secondary"] {{
                background-image: url("{icon_empty_grey}") !important;
                transition: background-image 0.2s;
            }}
            /* Hover */
            div[data-testid="stColumn"]:has(span#heart-marker) button[kind="secondary"]:hover {{
                background-image: url("{icon_empty_purple}") !important;
            }}

            /* STAN: ULUBIONE (type="primary") */
            div[data-testid="stColumn"]:has(span#heart-marker) button[kind="primary"] {{
                background-image: url("{icon_filled_purple}") !important;
            }}
            /* Reset tła systemowego primary */
            div[data-testid="stColumn"]:has(span#heart-marker) button[kind="primary"]:hover,
            div[data-testid="stColumn"]:has(span#heart-marker) button[kind="primary"]:focus {{
                background-color: transparent !important;
            }}

            /* STAN: DISABLED */
            div[data-testid="stColumn"]:has(span#heart-marker) button[disabled] {{
                background-image: url("{icon_empty_grey}") !important;
                opacity: 0.3 !important;
                pointer-events: none !important;
            }}

            /* 2. FIX NA MOBILE */
            @media (max-width: 640px) {{
                [data-testid="stColumn"] [data-testid="stHorizontalBlock"] {{
                    flex-direction: row !important;
                    flex-wrap: nowrap !important;
                }}
                [data-testid="stColumn"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child {{
                    width: 80% !important;
                    min-width: 80% !important;
                    flex: 1 1 auto !important;
                }}
                [data-testid="stColumn"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child {{
                    width: 20% !important;
                    min-width: 20% !important;
                    flex: 1 1 auto !important;
                }}
            }}
            </style>
            """, unsafe_allow_html=True)

            # --- UKŁAD HYBRYDOWY ---
            c_main_left, c_main_right = st.columns([0.5, 0.5])
            
            with c_main_left:
                selected_date = st.date_input("Wybierz datę", min_value=datetime.date.today(), format="DD-MM-YYYY")

            # --- LOGIKA DANYCH ---
            current_user_idx = df_users.index[df_users['Email'] == st.session_state['user_email']].tolist()[0]
            fav_raw = df_users.at[current_user_idx, 'Ulubione']
            current_fav_string = str(fav_raw) if pd.notna(fav_raw) else ""
            my_favorites = [e.strip().lower() for e in current_fav_string.split(',') if '@' in e]

            other_users_df = df_users[df_users['Email'] != st.session_state['user_email']]
            
            fav_list = []
            regular_list = []
            name_to_email_map = {}

            for index, row in other_users_df.iterrows():
                display_name = f"{row['Imię']} {row['Nazwisko']}"
                email = row['Email'].strip().lower()
                name_to_email_map[display_name] = email
                
                if email in my_favorites:
                    fav_list.append(display_name)
                else:
                    regular_list.append(display_name)
            
            final_options = ["Brak"]
            
            if fav_list:
                final_options.append("─── ULUBIONE ───")
                final_options.extend(sorted(fav_list))
            if regular_list:
                final_options.append("───")
                final_options.extend(sorted(regular_list))
            
            # --- PRAWA STRONA ---
            with c_main_right:
                c_sel, c_btn = st.columns([0.85, 0.15], vertical_alignment="bottom")
                
                with c_sel:
                    second_preacher_name = st.selectbox("Drugi głosiciel", final_options)
                
                with c_btn:
                    # --- WAŻNE: ZNACZNIK DLA CSS ---
                    # To pozwala nam celować stylami TYLKO w ten jeden przycisk w tej kolumnie
                    st.markdown('<span id="heart-marker"></span>', unsafe_allow_html=True)
                    
                    selected_email = name_to_email_map.get(second_preacher_name)
                    
                    if selected_email:
                        # LOGIKA PYTHON: Sprawdzamy listę ulubionych
                        # type="primary" -> FIOLETOWE PEŁNE
                        # type="secondary" -> SZARE PUSTE
                        
                        if selected_email in my_favorites:
                            if st.button(" ", type="primary", help="Usuń z ulubionych"):
                                my_favorites.remove(selected_email)
                                new_fav_str = ",".join(my_favorites)
                                df_users.at[current_user_idx, 'Ulubione'] = new_fav_str
                                update_user_db(df_users)
                                st.rerun()
                        else:
                            if st.button(" ", type="secondary", help="Dodaj do ulubionych"):
                                my_favorites.append(selected_email)
                                new_fav_str = ",".join(my_favorites)
                                df_users.at[current_user_idx, 'Ulubione'] = new_fav_str
                                update_user_db(df_users)
                                st.rerun()
                    else:
                        st.button(" ", disabled=True)

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
                        icon = '🟢' if status == 'Wolne' else '🤝'
                        
                        target_length = 15
                        chars_needed = target_length - len(time_range)
                        padding = "\u00A0" * int(chars_needed * 1.8) 
                        
                        return f"{time_range}{padding}{icon} {status}"

                    selected_hour = st.selectbox("Wybierz godzinę", options=sorted_hours, format_func=format_hour_label)
                    
                    slot_status = available_slots[selected_hour]
                    is_joining = "Dołącz do" in slot_status
                    can_proceed = True
                    
                    if "───" in second_preacher_name:
                        st.warning("To jest nagłówek sekcji. Wybierz konkretną osobę z listy.")
                        can_proceed = False
                    
                    # 2. Walidacja slotu (Twoja stara logika)
                    slot_status = available_slots[selected_hour]
                    is_joining = "Dołącz do" in slot_status
                    
                    if is_joining and second_preacher_name != "Brak" and can_proceed:
                        st.error("⛔ Nie możesz zapisać drugiej osoby, ponieważ w tej godzinie jest już tylko 1 wolne miejsce.")
                        can_proceed = False
                    elif is_joining and can_proceed:
                         st.info(f"ℹ️ Dołączasz do: {slot_status.replace('Dołącz do: ', '')}")

                    # Przycisk
                    if st.button("✅ Zapisz się", disabled=not can_proceed):
                        with st.spinner("Zapisywanie..."):
                            d_booking = datetime.datetime.combine(selected_date, datetime.time(0,0))
                            
                            sec_data = None
                            if second_preacher_name != "Brak":
                                # TU JUŻ NIE MUSIMY USUWAĆ GWIAZDEK, BO NAZWISKO JEST CZYSTE
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
                        st.info(f"🗓️ *W tym terminie pełni z Tobą służbę druga osoba.*")
                        delete_entirely = st.checkbox(
                            "⚠️ Usuń całkowicie wydarzenie",
                            value=False,
                            help="Jeśli zaznaczysz, całe wydarzenie zniknie. Odwołasz służbę również dla Twojej pary."
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