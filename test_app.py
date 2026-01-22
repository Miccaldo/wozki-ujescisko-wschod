import pytest
from unittest.mock import MagicMock, patch
import datetime
import pandas as pd
import app  

# --- FIXTURY ---

@pytest.fixture
def mock_session_state():
    """Mockuje st.session_state z nowymi polami (gender, role)."""
    with patch('app.st.session_state', dict()) as mock_state:
        mock_state['user_email'] = 'ja@test.com'
        mock_state['user_name'] = 'Testowy User'
        mock_state['user_gender'] = 'M'
        yield mock_state

@pytest.fixture
def mock_service():
    """Mockuje serwis Google Calendar."""
    with patch('app.get_calendar_service') as mock_get_service:
        service_mock = MagicMock()
        mock_get_service.return_value = service_mock
        yield service_mock

@pytest.fixture
def mock_users_db():
    """Mockuje bazę danych (potrzebne do identyfikacji po nazwisku)."""
    data = {
        'Imię': ['Jan', 'Testowy'],
        'Nazwisko': ['Nowak', 'User'],
        'Email': ['jan@other.com', 'ja@test.com'],
        'Płeć': ['M', 'M']
    }
    df = pd.DataFrame(data)
    with patch('app.get_users_db', return_value=df):
        yield df

# --- TESTY JEDNOSTKOWE ---

def test_parse_hours():
    assert app.parse_hours_from_title("Dyżur 7:00-18:00") == ("7:00", "18:00")
    assert app.parse_hours_from_title("Spotkanie") == (None, None)

def test_get_participants_logic(mock_users_db):
    """Testuje nową logikę rozpoznawania osób w tytule."""
    df = mock_users_db
    
    # Przypadek 1: Pełne nazwisko i imię
    emails, unknown = app.get_participants_from_title("Jan Nowak", df)
    assert 'jan@other.com' in emails
    assert not unknown
    
    # Przypadek 2: Odwrócona kolejność
    emails, unknown = app.get_participants_from_title("Nowak Jan", df)
    assert 'jan@other.com' in emails
    
    # Przypadek 3: Zdrobnienie (Janusz) - powinno znaleźć bo 2 litery pasują
    emails, unknown = app.get_participants_from_title("Nowak Janusz", df)
    assert 'jan@other.com' in emails
    
    # Przypadek 4: Ktoś obcy
    emails, unknown = app.get_participants_from_title("Obcy Człowiek", df)
    assert len(emails) == 0
    assert unknown is True

# --- TESTY LOGIKI DOSTĘPNOŚCI (GET SLOTS) ---

def test_get_slots_logic(mock_service, mock_session_state, mock_users_db):
    # Mockujemy eventy w kalendarzu
    main_event = {
        'id': 'main', 'summary': 'Dyżur 10:00-12:00', 
        'start': {'dateTime': '2030-01-01T10:00:00+01:00'}
    }
    # Event zajęty przez Jana Nowaka (rozpoznany po nazwisku)
    event_10 = {
        'id': 'e1', 'summary': 'Jan Nowak', 
        'start': {'dateTime': '2030-01-01T10:00:00+01:00'},
        'description': '' # Pusty opis!
    }
    
    mock_service.events().list().execute.return_value = {
        'items': [main_event, event_10]
    }

    slots, my_hours = app.get_slots_for_day(datetime.date(2030, 1, 1))

    # 10:00 -> Powinna być "Dołącz do: Jan Nowak"
    assert 10 in slots
    assert "Dołącz do: Jan Nowak" in slots[10]
    
    # 11:00 -> Powinna być "Wolne"
    assert 11 in slots
    assert slots[11] == "Wolne"

# --- TESTY LOGIKI ZAPISU (BOOK EVENT) ---

def test_book_event_new(mock_service, mock_session_state):
    """Nowy zapis (Insert z pustym opisem)."""
    mock_service.events().list().execute.return_value = {'items': []}
    
    app.book_event(datetime.date(2030, 1, 1), 10)
    
    mock_service.events().insert.assert_called_once()
    body = mock_service.events().insert.call_args[1]['body']
    
    # Sprawdzamy czy tytuł to Imię Nazwisko z sesji
    assert body['summary'] == 'Testowy User'
    # Opis powinien być pusty
    assert body['description'] == ''

def test_book_event_join(mock_service, mock_session_state, mock_users_db):
    """Dołączenie do kogoś (Update tytułu)."""
    existing = {
        'id': 'e1', 'summary': 'Jan Nowak', 
        'start': {'dateTime': '...'}
    }
    # Mockujemy listę eventów w danej godzinie
    mock_service.events().list().execute.return_value = {'items': [existing]}
    
    app.book_event(datetime.date(2030, 1, 1), 10)
    
    mock_service.events().update.assert_called_once()
    body = mock_service.events().update.call_args[1]['body']
    
    # Tytuł powinien być połączony
    assert body['summary'] == 'Jan Nowak i Testowy User'

# --- TESTY LOGIKI REZYGNACJI (CANCEL BOOKING) ---

def test_cancel_booking_im_second(mock_service, mock_session_state, mock_users_db):
    """Jestem drugi w tytule 'Jan Nowak i Testowy User'."""
    event = {
        'id': 'e1', 'summary': 'Jan Nowak i Testowy User',
        'start': {'dateTime': '...'}
    }
    mock_service.events().list().execute.return_value = {'items': [event]}
    
    app.cancel_booking(datetime.date(2030, 1, 1), 10)
    
    # Powinien być UPDATE
    mock_service.events().update.assert_called_once()
    body = mock_service.events().update.call_args[1]['body']
    
    # Moje nazwisko powinno zniknąć
    assert body['summary'] == 'Jan Nowak'

def test_cancel_booking_delete_all(mock_service, mock_session_state, mock_users_db):
    """Usuwam wszystko (jestem pierwszy + flaga)."""
    event = {
        'id': 'e1', 'summary': 'Testowy User i Jan Nowak',
        'start': {'dateTime': '...'}
    }
    mock_service.events().list().execute.return_value = {'items': [event]}
    
    app.cancel_booking(datetime.date(2030, 1, 1), 10, delete_entirely=True)
    
    # Powinien być DELETE
    mock_service.events().delete.assert_called_once()