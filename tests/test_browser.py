"""Tests para src/browser/ package."""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.browser.launcher import _is_port_open, ensure_browser, get_page
from src.browser.navigation import (
    browser_navigate, browser_click, browser_type, browser_read, browser_scroll,
)


# --- Launcher tests ---

def test_is_port_open_true():
    with patch('socket.create_connection') as mock_conn:
        mock_conn.return_value.__enter__ = MagicMock()
        mock_conn.return_value.__exit__ = MagicMock()
        assert _is_port_open(9222) is True


def test_is_port_open_false():
    with patch('socket.create_connection', side_effect=ConnectionRefusedError):
        assert _is_port_open(9222) is False


@pytest.mark.asyncio
async def test_ensure_browser_already_running():
    """Si el puerto ya está abierto, no lanza Chromium."""
    with patch('src.browser.launcher._is_port_open', return_value=True):
        result = await ensure_browser()
    assert result is None


@pytest.mark.asyncio
async def test_ensure_browser_no_chromium():
    """Si no hay Chromium instalado, devuelve error."""
    with patch('src.browser.launcher._is_port_open', return_value=False):
        with patch('shutil.which', return_value=None):
            result = await ensure_browser()
    assert "No se encontró Chromium" in result


@pytest.mark.asyncio
async def test_ensure_browser_launches():
    """Lanza Chromium y espera a que el puerto responda."""
    with patch('src.browser.launcher._is_port_open', side_effect=[False, True]):
        with patch('shutil.which', return_value='/usr/bin/chromium'):
            with patch('os.makedirs'):
                with patch('subprocess.Popen'):
                    result = await ensure_browser()
    assert result is None


@pytest.mark.asyncio
async def test_get_page():
    """get_page obtiene una página desde dentro de un playwright context."""
    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_context.pages = [mock_page]
    mock_browser.contexts = [mock_context]

    mock_playwright = AsyncMock()
    mock_playwright.chromium.connect_over_cdp.return_value = mock_browser

    page = await get_page(mock_playwright)
    assert page == mock_page
    mock_playwright.chromium.connect_over_cdp.assert_called_once()


# --- Navigation tests ---

@pytest.mark.asyncio
async def test_browser_navigate_success():
    mock_page = AsyncMock()
    mock_page.title.return_value = "Google"
    result = await browser_navigate(mock_page, "https://google.com")
    assert "Navegado correctamente" in result
    mock_page.goto.assert_called_once()


@pytest.mark.asyncio
async def test_browser_navigate_adds_https():
    mock_page = AsyncMock()
    mock_page.title.return_value = "Example"
    result = await browser_navigate(mock_page, "example.com")
    assert "https://" in result


@pytest.mark.asyncio
async def test_browser_navigate_empty_target():
    mock_page = AsyncMock()
    result = await browser_navigate(mock_page, "")
    assert "Error" in result


@pytest.mark.asyncio
async def test_browser_click_success():
    mock_page = AsyncMock()
    result = await browser_click(mock_page, "button.submit")
    assert "Se hizo clic" in result
    mock_page.click.assert_called_once_with("button.submit")


@pytest.mark.asyncio
async def test_browser_click_empty_target():
    mock_page = AsyncMock()
    result = await browser_click(mock_page, "")
    assert "Error" in result


@pytest.mark.asyncio
async def test_browser_type_success():
    mock_page = AsyncMock()
    result = await browser_type(mock_page, "input#search", "hello")
    assert "Se escribió correctamente" in result
    mock_page.type.assert_called_once_with("input#search", "hello", delay=50)


@pytest.mark.asyncio
async def test_browser_read():
    mock_page = AsyncMock()
    mock_page.title.return_value = "Test Page"
    mock_page.url = "https://example.com"
    mock_page.evaluate.return_value = "Hello world content"
    result = await browser_read(mock_page)
    assert "Test Page" in result
    assert "Hello world" in result


@pytest.mark.asyncio
async def test_browser_scroll_default():
    mock_page = AsyncMock()
    result = await browser_scroll(mock_page, "")
    assert "500" in result
    mock_page.evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_browser_scroll_custom():
    mock_page = AsyncMock()
    result = await browser_scroll(mock_page, "-300")
    assert "-300" in result
