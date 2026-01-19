import pytest
import toml
import sys
from unittest.mock import MagicMock

# 1. Importujemy PRAWDZIWY Streamlit (to naprawia błąd "is not a package")
import streamlit as st

# 2. Ładujemy sekrety z pliku
try:
    with open(".streamlit/secrets.toml", "r") as f:
        secrets_data = toml.load(f)
except FileNotFoundError:
    print("⚠️ OSTRZEŻENIE: Nie znaleziono .streamlit/secrets.toml. Testy mogą nie działać.")
    secrets_data = {}

# 3. PATCHOWANIE STREAMLIT (Nadpisujemy atrybuty na żywym module)

# A. Wstrzykujemy sekrety, żeby app.py mogło je odczytać
st.secrets = secrets_data

# B. Podmieniamy session_state na zwykły słownik
# (W środowisku testowym normalny session_state rzuca błędy o braku kontekstu)
st.session_state = {}

# C. Podmieniamy query_params na słownik
st.query_params = {}

# D. Mockujemy funkcje UI, które nie mogą działać bez przeglądarki
# Dzięki temu app.py nie wywali się na st.set_page_config czy st.markdown
st.set_page_config = MagicMock()
st.markdown = MagicMock()
st.title = MagicMock()
st.header = MagicMock()
st.subheader = MagicMock()
st.caption = MagicMock()
st.text = MagicMock()
st.info = MagicMock()
st.success = MagicMock()
st.warning = MagicMock()
st.error = MagicMock()
st.toast = MagicMock()
st.button = MagicMock(return_value=False) # Przyciski domyślnie niekliknięte
st.selectbox = MagicMock(return_value=None)
st.radio = MagicMock(return_value="Zapis") # Domyślna wartość radia
st.date_input = MagicMock()
st.dataframe = MagicMock()
st.expander = MagicMock()
st.spinner = MagicMock()
st.columns = MagicMock(return_value=[MagicMock(), MagicMock()]) # Zwraca 2 atrapy kolumn

# E. Mockujemy komponenty HTML/JS (np. zamykanie sidebaru)
st.components = MagicMock()
st.components.v1 = MagicMock()
st.components.v1.html = MagicMock()

# F. Mockujemy LocalStorage (bibliotekę zewnętrzną)
# Musimy to zrobić w sys.modules, bo jest importowana w app.py
mock_ls_module = MagicMock()
sys.modules["streamlit_local_storage"] = mock_ls_module
mock_ls_instance = MagicMock()
# Symulujemy, że LocalStorage zwraca None (brak zapisanego usera)
mock_ls_instance.getItem.return_value = None 
mock_ls_module.LocalStorage.return_value = mock_ls_instance


# 4. FIXTURE DLA TESTÓW
@pytest.fixture
def mock_session():
    """Pozwala testom manipulować sesją."""
    # Czyścimy stan przed każdym testem
    st.session_state.clear()
    return st.session_state