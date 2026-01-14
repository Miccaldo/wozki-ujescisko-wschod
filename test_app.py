import pytest
from unittest.mock import MagicMock, patch
import datetime
import app  # Zakładamy, że Twój plik z kodem nazywa się app.py

# --- KONFIGURACJA TESTÓW ---

@pytest.fixture
def mock_session_state():
    """Mockuje st.session_state."""
    with patch('app.st.session_state', dict()) as mock_state:
        mock_state['user_email'] = 'ja@test.com'
        mock_state['user_name'] = 'Testowy User'
        yield mock_state

@pytest.fixture
def mock_service():
    """Mockuje serwis Google Calendar."""
    with patch('app.get_calendar_service') as mock_get_service:
        service_mock = MagicMock()
        mock_get_service.return_value = service_mock
        yield service_mock

# --- TESTY JEDNOSTKOWE (HELPERY) ---

def test_parse_hours():
    assert app.parse_hours_from_title("Dyżur 7:00-18:00") == ("7:00", "18:00")
    assert app.parse_hours_from_title("Spotkanie") == (None, None)

# --- TESTY LOGIKI DOSTĘPNOŚCI (GET SLOTS) ---

def test_get_slots_logic(mock_service, mock_session_state):
    # 1. Przygotowujemy fałszywe dane z Google Calendar
    # Główne wydarzenie definiujące ramy (10:00 - 12:00)
    main_event = {
        'id': 'main', 'summary': 'Dyżur 10:00-12:00', 
        'start': {'dateTime': '2025-01-01T10:00:00+01:00'},
        'description': ''
    }
    # Event w godz. 10:00 - zajęty przez kogoś innego (1 osoba)
    event_10 = {
        'id': 'e1', 'summary': 'Jan Nowak', 
        'start': {'dateTime': '2025-01-01T10:00:00+01:00'},
        'description': 'email:jan@other.com'
    }
    # Godzina 11:00 jest pusta (brak eventu)
    
    # Konfigurujemy mocka, żeby zwracał te eventy
    mock_service.events().list().execute.return_value = {
        'items': [main_event, event_10]
    }

    # 2. Uruchamiamy funkcję
    date_check = datetime.date(2025, 1, 1)
    slots, my_hours = app.get_slots_for_day(date_check)

    # 3. Sprawdzamy wyniki
    # Godzina 10:00 -> Powinna być "Dołącz do: Jan Nowak"
    assert 10 in slots
    assert "Dołącz do: Jan Nowak" in slots[10]
    
    # Godzina 11:00 -> Powinna być "Wolne"
    assert 11 in slots
    assert slots[11] == "Wolne"

# --- TESTY LOGIKI REZYGNACJI (CANCEL BOOKING) ---

def test_cancel_booking_im_second(mock_service, mock_session_state):
    """Scenariusz: Jestem dopisany jako drugi. Rezygnuję."""
    # Mockujemy event: Jan (organizator) i Ja
    event_shared = {
        'id': 'evt123', 'summary': 'Jan Nowak i Testowy User',
        'description': 'email:jan@other.com, ja@test.com',
        'start': {'dateTime': '2025-01-01T10:00:00+01:00'}
    }
    mock_service.events().list().execute.return_value = {'items': [event_shared]}

    # Wywołujemy funkcję
    app.cancel_booking(datetime.date(2025, 1, 1), 10, delete_entirely=False)

    # Sprawdzamy czy zawołano UPDATE (a nie delete)
    mock_service.events().update.assert_called_once()
    
    # Sprawdzamy czy zaktualizowano treść (czy usunięto mnie)
    call_args = mock_service.events().update.call_args[1] # argumenty kluczowe
    body = call_args['body']
    
    assert body['summary'] == 'Jan Nowak' # Tytuł bez mojego nazwiska
    assert body['description'] == 'email:jan@other.com' # Opis bez mojego maila

def test_cancel_booking_im_first_partner_stays(mock_service, mock_session_state):
    """Scenariusz: Jestem organizatorem, mam partnera, ale NIE usuwam całości."""
    event_shared = {
        'id': 'evt123', 'summary': 'Testowy User i Jan Nowak',
        'description': 'email:ja@test.com, jan@other.com',
        'start': {'dateTime': '2025-01-01T10:00:00+01:00'}
    }
    mock_service.events().list().execute.return_value = {'items': [event_shared]}

    # delete_entirely = False
    app.cancel_booking(datetime.date(2025, 1, 1), 10, delete_entirely=False)

    # Powinien być UPDATE (promocja Jana na organizatora)
    mock_service.events().update.assert_called_once()
    body = mock_service.events().update.call_args[1]['body']
    
    assert 'Jan Nowak' in body['summary']
    assert 'Testowy User' not in body['summary']
    assert body['description'] == 'email:jan@other.com'

def test_cancel_booking_im_first_delete_all(mock_service, mock_session_state):
    """Scenariusz: Jestem organizatorem, mam partnera i USUWAM CAŁOŚĆ."""
    event_shared = {
        'id': 'evt123', 'summary': 'Testowy User i Jan Nowak',
        'description': 'email:ja@test.com, jan@other.com',
        'start': {'dateTime': '2025-01-01T10:00:00+01:00'}
    }
    mock_service.events().list().execute.return_value = {'items': [event_shared]}

    # delete_entirely = True
    app.cancel_booking(datetime.date(2025, 1, 1), 10, delete_entirely=True)

    # Powinien być DELETE (całego eventu)
    mock_service.events().delete.assert_called_once_with(
        calendarId=app.CALENDAR_ID, 
        eventId='evt123'
    )
    # Update NIE powinien być wołany
    mock_service.events().update.assert_not_called()

def test_book_event_new(mock_service, mock_session_state):
    """Scenariusz: Nowy zapis (INSERT)."""
    # Lista zwraca pusto (brak eventów w tej godzinie)
    mock_service.events().list().execute.return_value = {'items': []}
    
    app.book_event(datetime.date(2025, 1, 1), 10)
    
    mock_service.events().insert.assert_called_once()
    body = mock_service.events().insert.call_args[1]['body']
    assert body['summary'] == 'Testowy User'
    assert 'ja@test.com' in body['description']

def test_book_event_join(mock_service, mock_session_state):
    """Scenariusz: Dołączenie do kogoś (UPDATE)."""
    existing_event = {
        'id': 'evt1', 'summary': 'Jan Nowak', 
        'description': 'email:jan@other.com',
        'start': {'dateTime': '...'}
    }
    mock_service.events().list().execute.return_value = {'items': [existing_event]}
    
    app.book_event(datetime.date(2025, 1, 1), 10)
    
    mock_service.events().update.assert_called_once()
    body = mock_service.events().update.call_args[1]['body']
    assert body['summary'] == 'Jan Nowak i Testowy User'
    assert 'jan@other.com, ja@test.com' in body['description']