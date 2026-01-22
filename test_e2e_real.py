import pytest
import datetime
import time
from zoneinfo import ZoneInfo
from googleapiclient.discovery import build
from google.oauth2 import service_account
from unittest.mock import patch
import app 

# --- KONFIGURACJA ---
# !!! WAŻNE: Wpisz tutaj ID pustego kalendarza testowego !!!
TEST_CALENDAR_ID = "miccaldooo@gmail.com" 

TEST_DATE = datetime.date(2030, 1, 1) 
TEST_HOUR_1 = 10 
TEST_HOUR_2 = 12 
NEGATIVE_HOUR = 14

# Nadpisujemy ID kalendarza w aplikacji na czas testów
app.CALENDAR_ID = TEST_CALENDAR_ID

@pytest.fixture(scope="module")
def real_service():
    """Tworzy prawdziwe połączenie z Google API."""
    creds_dict = dict(app.st.secrets["connections"]["gsheets"])
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=['https://www.googleapis.com/auth/calendar']
    )
    return build('calendar', 'v3', credentials=creds)

@pytest.fixture(autouse=True)
def cleanup_test_day(real_service):
    """Czyści kalendarz w dniu testowym PRZED i PO teście."""
    def clean():
        print(f"\n🧹 Czyszczenie dnia {TEST_DATE}...")
        tz = ZoneInfo("Europe/Warsaw")
        start = datetime.datetime.combine(TEST_DATE, datetime.time(0, 0), tzinfo=tz).isoformat()
        end = datetime.datetime.combine(TEST_DATE, datetime.time(23, 59), tzinfo=tz).isoformat()
        
        events = real_service.events().list(
            calendarId=TEST_CALENDAR_ID, timeMin=start, timeMax=end, singleEvents=True
        ).execute().get('items', [])
        
        for e in events:
            real_service.events().delete(calendarId=TEST_CALENDAR_ID, eventId=e['id']).execute()
            
    clean()
    yield
    clean()

# Mockujemy get_users_db, żeby testy nie polegały na prawdziwym Google Sheet
# Musimy zwrócić DataFrame z użytkownikami, których używamy w testach
@pytest.fixture
def mock_users_db():
    import pandas as pd
    data = {
        'Imię': ['Jan', 'Anna', 'Marek', 'Zofia'],
        'Nazwisko': ['Kowalski', 'Nowak', 'Marecki', 'Zofinska'],
        'Email': ['jan.kowalski@test.pl', 'anna.nowak@test.pl', 'marek@test.pl', 'zofia@test.pl'],
        'Płeć': ['M', 'K', 'M', 'K'],
        'Ulubione': ['', '', '', '']
    }
    return pd.DataFrame(data)

def test_full_booking_lifecycle_hybrid(mock_session, real_service, mock_users_db):
    """Testuje nową logikę hybrydową (identyfikacja po nazwisku)."""
    
    # Patchujemy get_users_db, żeby aplikacja widziała naszych testowych userów
    with patch('app.get_users_db', return_value=mock_users_db), \
         patch('app.send_notification_email') as mock_email:
        
        # === SCENARIUSZ A: Jan zapisuje się sam ===
        print("\n➡️ Scenariusz A: Jan zapisuje się sam")
        mock_session['user_email'] = 'jan.kowalski@test.pl'
        mock_session['user_name'] = 'Jan Kowalski'
        mock_session['user_gender'] = 'M'
        
        assert app.book_event(TEST_DATE, TEST_HOUR_1) is True
        
        # Weryfikacja Google: Tytuł to tylko imię i nazwisko
        ev = get_event_at(real_service, TEST_HOUR_1)
        assert ev['summary'] == 'Jan Kowalski'
        assert ev.get('description', '') == '' # Opis powinien być pusty

        
        # === SCENARIUSZ B: Anna dołącza do Jana ===
        print("➡️ Scenariusz B: Anna dołącza do Jana")
        mock_session['user_email'] = 'anna.nowak@test.pl'
        mock_session['user_name'] = 'Anna Nowak'
        mock_session['user_gender'] = 'K'
        
        # Aplikacja musi rozpoznać Jana w tytule "Jan Kowalski" używając mock_users_db
        assert app.book_event(TEST_DATE, TEST_HOUR_1) is True
        
        # Weryfikacja Google
        ev = get_event_at(real_service, TEST_HOUR_1)
        assert 'Jan Kowalski' in ev['summary']
        assert 'Anna Nowak' in ev['summary']
        
        # Sprawdzamy maila (czy system znalazł Jana w bazie i wysłał mu powiadomienie)
        mock_email.assert_called_once()
        args, _ = mock_email.call_args
        assert args[0] == 'jan.kowalski@test.pl' # Do Jana
        mock_email.reset_mock()


        # === SCENARIUSZ C: Anna rezygnuje ===
        print("➡️ Scenariusz C: Anna rezygnuje")
        # Sesja nadal na Annę. Aplikacja musi znaleźć "Anna Nowak" w tytule i ją wyciąć.
        assert app.cancel_booking(TEST_DATE, TEST_HOUR_1) is True
        
        # Weryfikacja Google
        ev = get_event_at(real_service, TEST_HOUR_1)
        assert ev['summary'] == 'Jan Kowalski' # Anna zniknęła
        
        # Mail do Jana
        mock_email.assert_called() 
        args, _ = mock_email.call_args
        assert args[0] == 'jan.kowalski@test.pl'
        mock_email.reset_mock()
        
        
        # === SCENARIUSZ D: Jan usuwa resztę ===
        print("➡️ Scenariusz D: Jan usuwa")
        mock_session['user_email'] = 'jan.kowalski@test.pl'
        mock_session['user_name'] = 'Jan Kowalski'
        
        assert app.cancel_booking(TEST_DATE, TEST_HOUR_1, delete_entirely=True) is True
        assert get_event_at(real_service, TEST_HOUR_1) is None


        # === SCENARIUSZ E: Marek z Zofią (Para od razu) ===
        print("\n➡️ Scenariusz E: Marek z Zofią")
        mock_session['user_email'] = 'marek@test.pl'
        mock_session['user_name'] = 'Marek Marecki'
        
        partner = {
            'Imię': 'Zofia', 'Nazwisko': 'Zofinska', 'Email': 'zofia@test.pl'
        }
        
        assert app.book_event(TEST_DATE, TEST_HOUR_2, second_preacher_obj=partner) is True
        
        ev = get_event_at(real_service, TEST_HOUR_2)
        assert 'Marek Marecki' in ev['summary']
        assert 'Zofia Zofinska' in ev['summary']


        # === SCENARIUSZ F: Marek usuwa parę ===
        print("➡️ Scenariusz F: Marek usuwa parę")
        assert app.cancel_booking(TEST_DATE, TEST_HOUR_2, delete_entirely=True) is True
        
        # Mail do Zofii (z bazy)
        mock_email.assert_called()
        args, _ = mock_email.call_args
        assert args[0] == 'zofia@test.pl'
        
        print("✅ Scenariusze pozytywne zaliczone!")

def test_hybrid_identification(mock_session, real_service, mock_users_db):
    """Testuje, czy aplikacja radzi sobie z RĘCZNYMI wpisami w kalendarzu."""
    
    with patch('app.get_users_db', return_value=mock_users_db):
        
        print("\n➡️ Test Hybrydowy: Ręczny wpis w Kalendarzu")
        tz = ZoneInfo("Europe/Warsaw")
        
        # 1. !!! WAŻNE: TWORZYMY RAMY CZASOWE (GŁÓWNY EVENT) !!!
        # Bez tego get_slots_for_day zwraca puste listy
        start_day = datetime.datetime.combine(TEST_DATE, datetime.time(8, 0), tzinfo=tz).isoformat()
        end_day = datetime.datetime.combine(TEST_DATE, datetime.time(20, 0), tzinfo=tz).isoformat()
        
        real_service.events().insert(
            calendarId=TEST_CALENDAR_ID,
            body={
                'summary': 'Wózki 08:00-20:00', # Tytuł musi zawierać godziny
                'start': {'dateTime': start_day},
                'end': {'dateTime': end_day}
            }
        ).execute()

        # 2. Ręczny wpis "Kowalski Jan" (odwrotna kolejność)
        start = datetime.datetime.combine(TEST_DATE, datetime.time(NEGATIVE_HOUR, 0), tzinfo=tz).isoformat()
        end = datetime.datetime.combine(TEST_DATE, datetime.time(NEGATIVE_HOUR+1, 0), tzinfo=tz).isoformat()
        
        real_service.events().insert(
            calendarId=TEST_CALENDAR_ID, 
            body={'summary': 'Kowalski Jan', 'start': {'dateTime': start}, 'end': {'dateTime': end}}
        ).execute()
        
        # Czekamy na API Google
        time.sleep(2) 
        
        # 3. Sprawdzamy czy aplikacja to widzi jako zajęte przez Jana
        mock_session['user_email'] = 'jan.kowalski@test.pl'
        mock_session['user_name'] = 'Jan Kowalski'
        
        slots, my_booked = app.get_slots_for_day(TEST_DATE)
        
        # Teraz powinno zadziałać, bo ramy czasowe istnieją
        assert NEGATIVE_HOUR in my_booked
        
        print("✅ Aplikacja rozpoznała Jana w ręcznym wpisie 'Kowalski Jan'")

def get_event_at(service, hour):
    tz = ZoneInfo("Europe/Warsaw")
    start = datetime.datetime.combine(TEST_DATE, datetime.time(hour, 0), tzinfo=tz).isoformat()
    end = datetime.datetime.combine(TEST_DATE, datetime.time(hour+1, 0), tzinfo=tz).isoformat()
    items = service.events().list(calendarId=TEST_CALENDAR_ID, timeMin=start, timeMax=end, singleEvents=True).execute().get('items', [])
    return items[0] if items else None