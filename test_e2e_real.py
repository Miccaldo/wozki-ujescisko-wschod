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
    """Tworzy prawdziwe połączenie z Google API do weryfikacji."""
    creds_dict = dict(app.st.secrets["connections"]["gsheets"])
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=['https://www.googleapis.com/auth/calendar']
    )
    return build('calendar', 'v3', credentials=creds)

@pytest.fixture(autouse=True)
def cleanup_test_day(real_service):
    """Czyści kalendarz w dniu testowym PRZED i PO każdym teście."""
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

def test_full_booking_lifecycle_with_emails(mock_session, real_service):
    """
    Testuje: 
    - Zapis pojedynczy
    - Dołączanie (Update)
    - Rezygnację (Update)
    - Rezygnację całkowitą (Delete)
    - Zapis pary (Insert złożony)
    - Usuwanie pary (Delete złożony)
    - WYSYŁANIE MAILI (czy funkcja jest wołana)
    """
    
    # Mockujemy funkcję wysyłania maili, żeby nie spamować, ale sprawdzać czy działa
    with patch('app.send_notification_email') as mock_email:
        
        # === SCENARIUSZ A: Jan zapisuje się sam ===
        print("\n➡️ Scenariusz A: Jan zapisuje się sam")
        mock_session['user_email'] = 'jan.kowalski@test.pl'
        mock_session['user_name'] = 'Jan Kowalski'
        mock_session['user_gender'] = 'M'
        
        assert app.book_event(TEST_DATE, TEST_HOUR_1) is True
        
        # Weryfikacja: Brak maila (sam do siebie nie wysyła)
        mock_email.assert_not_called()
        # Weryfikacja Google
        ev = get_event_at(real_service, TEST_HOUR_1)
        assert ev['summary'] == 'Jan Kowalski'

        
        # === SCENARIUSZ B: Anna dołącza do Jana ===
        print("➡️ Scenariusz B: Anna dołącza do Jana")
        mock_session['user_email'] = 'anna.nowak@test.pl'
        mock_session['user_name'] = 'Anna Nowak'
        mock_session['user_gender'] = 'K'
        
        assert app.book_event(TEST_DATE, TEST_HOUR_1) is True
        
        # Weryfikacja: Mail do Jana (organizatora)
        mock_email.assert_called_once()
        args, _ = mock_email.call_args
        assert args[0] == 'jan.kowalski@test.pl' # Do Jana
        assert "Ktoś dołączył" in args[1]       # Temat
        mock_email.reset_mock()
        
        # Weryfikacja Google
        ev = get_event_at(real_service, TEST_HOUR_1)
        assert ev['summary'] == 'Jan Kowalski i Anna Nowak'


        # === SCENARIUSZ C: Anna rezygnuje ===
        print("➡️ Scenariusz C: Anna rezygnuje")
        # Sesja nadal na Annę
        assert app.cancel_booking(TEST_DATE, TEST_HOUR_1) is True
        
        # Weryfikacja: Mail do Jana (został sam)
        mock_email.assert_called_once()
        args, _ = mock_email.call_args
        assert args[0] == 'jan.kowalski@test.pl'
        assert "Zmiana w grafiku" in args[1]
        mock_email.reset_mock()
        
        # Weryfikacja Google
        ev = get_event_at(real_service, TEST_HOUR_1)
        assert ev['summary'] == 'Jan Kowalski'
        
        
        # === SCENARIUSZ D: Jan usuwa resztę ===
        print("➡️ Scenariusz D: Jan usuwa")
        mock_session['user_email'] = 'jan.kowalski@test.pl'
        mock_session['user_name'] = 'Jan Kowalski'
        
        assert app.cancel_booking(TEST_DATE, TEST_HOUR_1, delete_entirely=True) is True
        
        # Weryfikacja: Brak maila (był sam)
        mock_email.assert_not_called()
        # Weryfikacja Google: Pusto
        assert get_event_at(real_service, TEST_HOUR_1) is None


        # === SCENARIUSZ E: Marek zapisuje się z Zofią (Para od razu) ===
        print("\n➡️ Scenariusz E: Marek z Zofią")
        mock_session['user_email'] = 'marek@test.pl'
        mock_session['user_name'] = 'Marek'
        
        partner = {
            'Imię': 'Zofia',
            'Nazwisko': 'Zofinska',
            'Email': 'zofia@test.pl'
        }
        
        assert app.book_event(TEST_DATE, TEST_HOUR_2, second_preacher_obj=partner) is True
        
        # Weryfikacja: Mail zaproszenie do Zofii
        mock_email.assert_called_once()
        args, _ = mock_email.call_args
        assert args[0] == 'zofia@test.pl'
        assert "Nowy termin" in args[1]
        mock_email.reset_mock()
        
        # Weryfikacja Google
        ev = get_event_at(real_service, TEST_HOUR_2)
        assert ev['summary'] == 'Marek i Zofia Zofinska'


        # === SCENARIUSZ F: Marek usuwa parę ===
        print("➡️ Scenariusz F: Marek usuwa parę")
        assert app.cancel_booking(TEST_DATE, TEST_HOUR_2, delete_entirely=True) is True
        
        # Weryfikacja: Mail do Zofii o odwołaniu
        mock_email.assert_called_once()
        args, _ = mock_email.call_args
        assert args[0] == 'zofia@test.pl'
        assert "Odwołano termin" in args[1]
        
        print("✅ Scenariusze pozytywne zaliczone!")

def test_negative_scenarios(mock_session, real_service):
    """Testuje blokady, zabezpieczenia i kolizje."""
    
    # Tutaj też używamy patcha, żeby upewnić się, że system NIE wysłał maila
    # w przypadku błędu/blokady.
    with patch('app.send_notification_email') as mock_email:

        # === SCENARIUSZ G: Blokada 3. osoby ===
        print("\n➡️ Scenariusz G: Blokada 3. osoby")
        
        # 1. Tworzymy pełny slot (U1 + U2)
        mock_session['user_email'] = 'u1@test.pl'
        mock_session['user_name'] = 'U1'
        app.book_event(TEST_DATE, NEGATIVE_HOUR)
        
        mock_session['user_email'] = 'u2@test.pl'
        mock_session['user_name'] = 'U2'
        app.book_event(TEST_DATE, NEGATIVE_HOUR)
        mock_email.reset_mock() # Czyścimy historię maili z setupu
        
        # 2. U3 próbuje wejść
        mock_session['user_email'] = 'u3@test.pl'
        mock_session['user_name'] = 'U3'
        
        success = app.book_event(TEST_DATE, NEGATIVE_HOUR)
        
        assert success is False
        mock_email.assert_not_called() # Żaden mail nie powinien wyjść
        
        # Sprzątanie
        app.cancel_booking(TEST_DATE, NEGATIVE_HOUR, delete_entirely=True)


        # === SCENARIUSZ H: Blokada pary do singla ===
        print("➡️ Scenariusz H: Blokada pary do singla")
        
        # 1. U1 zakłada slot
        mock_session['user_email'] = 'u1@test.pl'
        mock_session['user_name'] = 'U1'
        app.book_event(TEST_DATE, NEGATIVE_HOUR)
        mock_email.reset_mock()
        
        # 2. U2 próbuje dodać siebie + Partnera
        mock_session['user_email'] = 'u2@test.pl'
        mock_session['user_name'] = 'U2'
        partner = {'Imię': 'X', 'Nazwisko': 'Y', 'Email': 'x@y.pl'}
        
        success = app.book_event(TEST_DATE, NEGATIVE_HOUR, second_preacher_obj=partner)
        
        assert success is False
        mock_email.assert_not_called()
        
        # Sprzątanie
        app.cancel_booking(TEST_DATE, NEGATIVE_HOUR, delete_entirely=True)


        # === SCENARIUSZ I: Obcy event (Ręczny wpis) ===
        print("➡️ Scenariusz I: Obcy event")
        
        # Wstawiamy event bez tagu 'email:'
        tz = ZoneInfo("Europe/Warsaw")
        start = datetime.datetime.combine(TEST_DATE, datetime.time(NEGATIVE_HOUR, 0), tzinfo=tz).isoformat()
        end = datetime.datetime.combine(TEST_DATE, datetime.time(NEGATIVE_HOUR+1, 0), tzinfo=tz).isoformat()
        real_service.events().insert(
            calendarId=TEST_CALENDAR_ID, 
            body={'summary': 'Obcy', 'start': {'dateTime': start}, 'end': {'dateTime': end}}
        ).execute()
        
        # Sprawdzamy czy get_slots_for_day go widzi jako dostępny
        # Powinien zostać odfiltrowany lub oznaczony jako FULL
        available, _ = app.get_slots_for_day(TEST_DATE)
        
        assert NEGATIVE_HOUR not in available
        
        print("✅ Scenariusze negatywne zaliczone!")

def get_event_at(service, hour):
    """Pomocnicza do pobierania eventu."""
    tz = ZoneInfo("Europe/Warsaw")
    start = datetime.datetime.combine(TEST_DATE, datetime.time(hour, 0), tzinfo=tz).isoformat()
    end = datetime.datetime.combine(TEST_DATE, datetime.time(hour+1, 0), tzinfo=tz).isoformat()
    items = service.events().list(calendarId=TEST_CALENDAR_ID, timeMin=start, timeMax=end, singleEvents=True).execute().get('items', [])
    return items[0] if items else None