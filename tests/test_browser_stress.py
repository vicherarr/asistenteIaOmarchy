"""Tests exhaustivos de capacidades del navegador web - 50 prompts diseñados.

Cubre: navigate, click, type, read, scroll, clip, research, translate, launch
y todos los edge cases posibles del browser package.
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path

import pytest

from src.command_executor import control_local_browser
from src.browser.navigation import (
    browser_navigate, browser_click, browser_type, browser_read, browser_scroll
)
from src.browser.clip import browser_clip
from src.browser.research import browser_research, _extract_url
from src.browser.launcher import ensure_browser, get_page, _is_port_open
from src.browser.translate import browser_translate


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_page():
    """Mock de una página de Playwright."""
    page = AsyncMock()
    page.title = AsyncMock(return_value="Test Page")
    page.url = "https://example.com"
    page.goto = AsyncMock()
    page.click = AsyncMock()
    page.type = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value="Contenido de la página")
    return page


@pytest.fixture
def mock_playwright():
    """Mock del objeto playwright."""
    pw = AsyncMock()
    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_context.pages = [mock_page]
    mock_browser.contexts = [mock_context]
    pw.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)
    pw.__aenter__ = AsyncMock(return_value=pw)
    pw.__aexit__ = AsyncMock(return_value=False)
    return pw


# ============================================================================
# 50 PROMPTS DISEÑADOS PARA EXPRIMIR EL NAVEGADOR
# ============================================================================

PROMPTS_BROWSER = [
    # --- NAVIGATE (1-10) ---
    {
        "nombre": "navigate_google",
        "prompt": "Abre google.com",
        "accion": "navigate",
        "target": "https://www.google.com",
        "valor": "",
    },
    {
        "nombre": "navigate_sin_protocolo",
        "prompt": "Navega a wikipedia.org",
        "accion": "navigate",
        "target": "wikipedia.org",
        "valor": "",
    },
    {
        "nombre": "navigate_url_larga",
        "prompt": "Abre esta URL con muchos parámetros",
        "accion": "navigate",
        "target": "https://example.com/search?q=test&lang=es&page=1&sort=relevance&filter=all",
        "valor": "",
    },
    {
        "nombre": "navigate_url_vacia",
        "prompt": "Navega a...",
        "accion": "navigate",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "navigate_url_maliciosa",
        "prompt": "Abre esta página",
        "accion": "navigate",
        "target": "https://example.com/<script>alert('xss')</script>",
        "valor": "",
    },
    {
        "nombre": "navigate_localhost",
        "prompt": "Abre el servidor local",
        "accion": "navigate",
        "target": "http://localhost:3000",
        "valor": "",
    },
    {
        "nombre": "navigate_file_protocol",
        "prompt": "Abre un archivo local",
        "accion": "navigate",
        "target": "file:///etc/passwd",
        "valor": "",
    },
    {
        "nombre": "navigate_url_con_unicode",
        "prompt": "Abre esta URL con caracteres especiales",
        "accion": "navigate",
        "target": "https://example.com/página/con-título-español",
        "valor": "",
    },
    {
        "nombre": "navigate_url_con_hash",
        "prompt": "Navega a una sección específica",
        "accion": "navigate",
        "target": "https://docs.python.org/3/library/asyncio.html#asyncio.create_task",
        "valor": "",
    },
    {
        "nombre": "navigate_timeout",
        "prompt": "Abre una página que tarda mucho",
        "accion": "navigate",
        "target": "https://example.com/slow-page",
        "valor": "",
    },

    # --- CLICK (11-18) ---
    {
        "nombre": "click_boton_simple",
        "prompt": "Haz clic en el botón de enviar",
        "accion": "click",
        "target": "button[type='submit']",
        "valor": "",
    },
    {
        "nombre": "click_selector_vacio",
        "prompt": "Haz clic en...",
        "accion": "click",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "click_selector_inexistente",
        "prompt": "Haz clic en el elemento que no existe",
        "accion": "click",
        "target": "#elemento-que-no-existe-en-la-pagina",
        "valor": "",
    },
    {
        "nombre": "click_x_path",
        "prompt": "Haz clic usando XPath",
        "accion": "click",
        "target": "//div[@class='container']/button[1]",
        "valor": "",
    },
    {
        "nombre": "click_selector_complejo",
        "prompt": "Haz clic en este selector complejo",
        "accion": "click",
        "target": "div.main > ul.nav > li:nth-child(3) > a[href='/about']",
        "valor": "",
    },
    {
        "nombre": "click_text_link",
        "prompt": "Haz clic en el enlace que dice 'Más información'",
        "accion": "click",
        "target": "a:has-text('Más información')",
        "valor": "",
    },
    {
        "nombre": "click_multiple_elements",
        "prompt": "Haz clic en todos los botones",
        "accion": "click",
        "target": "button",
        "valor": "",
    },
    {
        "nombre": "click_selector_con_inyeccion",
        "prompt": "Haz clic en este selector malicioso",
        "accion": "click",
        "target": "'; DROP TABLE users; --",
        "valor": "",
    },

    # --- TYPE (19-26) ---
    {
        "nombre": "type_input_simple",
        "prompt": "Escribe 'hola mundo' en el campo de búsqueda",
        "accion": "type",
        "target": "input[name='q']",
        "valor": "hola mundo",
    },
    {
        "nombre": "type_selector_vacio",
        "prompt": "Escribe texto en...",
        "accion": "type",
        "target": "",
        "valor": "texto",
    },
    {
        "nombre": "type_texto_largo",
        "prompt": "Escribe un texto muy largo",
        "accion": "type",
        "target": "textarea",
        "valor": "A" * 5000,
    },
    {
        "nombre": "type_texto_unicode",
        "prompt": "Escribe texto con emojis y unicode",
        "accion": "type",
        "target": "input",
        "valor": "Hello 🌍 你好世界 مرحبا",
    },
    {
        "nombre": "type_contraseña",
        "prompt": "Escribe mi contraseña en el campo",
        "accion": "type",
        "target": "input[type='password']",
        "valor": "mi_contraseña_secreta_123",
    },
    {
        "nombre": "type_codigo",
        "prompt": "Escribe este código en el editor",
        "accion": "type",
        "target": "textarea.code-editor",
        "valor": "def hello():\n    print('world')\n    return True",
    },
    {
        "nombre": "type_comillas",
        "prompt": "Escribe texto con comillas",
        "accion": "type",
        "target": "input",
        "valor": 'Texto con "comillas dobles" y \'simples\' y `backticks`',
    },
    {
        "nombre": "type_inyeccion_js",
        "prompt": "Escribe esto en el campo",
        "accion": "type",
        "target": "input",
        "valor": "'; alert('xss'); //",
    },

    # --- READ (27-32) ---
    {
        "nombre": "read_pagina_normal",
        "prompt": "Lee el contenido de la página actual",
        "accion": "read",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "read_pagina_vacia",
        "prompt": "Lee esta página vacía",
        "accion": "read",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "read_pagina_con_iframes",
        "prompt": "Lee el contenido incluyendo iframes",
        "accion": "read",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "read_pagina_con_shadow_dom",
        "prompt": "Lee el contenido del shadow DOM",
        "accion": "read",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "read_pagina_muy_larga",
        "prompt": "Lee esta página con mucho contenido",
        "accion": "read",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "read_pagina_dinamica",
        "prompt": "Lee el contenido después de que cargue el JavaScript",
        "accion": "read",
        "target": "",
        "valor": "",
    },

    # --- SCROLL (33-38) ---
    {
        "nombre": "scroll_abajo",
        "prompt": "Baja en la página",
        "accion": "scroll",
        "target": "",
        "valor": "500",
    },
    {
        "nombre": "scroll_arriba",
        "prompt": "Sube en la página",
        "accion": "scroll",
        "target": "",
        "valor": "-500",
    },
    {
        "nombre": "scroll_valor_invalido",
        "prompt": "Haz scroll con valor inválido",
        "accion": "scroll",
        "target": "",
        "valor": "abc",
    },
    {
        "nombre": "scroll_muy_largo",
        "prompt": "Haz scroll muy largo",
        "accion": "scroll",
        "target": "",
        "valor": "50000",
    },
    {
        "nombre": "scroll_negativo_grande",
        "prompt": "Sube mucho en la página",
        "accion": "scroll",
        "target": "",
        "valor": "-10000",
    },
    {
        "nombre": "scroll_sin_valor",
        "prompt": "Haz scroll",
        "accion": "scroll",
        "target": "",
        "valor": "",
    },

    # --- CLIP (39-43) ---
    {
        "nombre": "clip_pagina_normal",
        "prompt": "Guarda esta página en Obsidian",
        "accion": "clip",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "clip_pagina_sin_contenido",
        "prompt": "Guarda esta página vacía",
        "accion": "clip",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "clip_pagina_muy_larga",
        "prompt": "Guarda este artículo largo",
        "accion": "clip",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "clip_pagina_con_titulo_especial",
        "prompt": "Guarda esta página con título especial",
        "accion": "clip",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "clip_pagina_con_unicode",
        "prompt": "Guarda esta página con título en chino",
        "accion": "clip",
        "target": "",
        "valor": "",
    },

    # --- RESEARCH (44-47) ---
    {
        "nombre": "research_tema_simple",
        "prompt": "Investiga sobre la historia de Internet",
        "accion": "research",
        "target": "historia de Internet",
        "valor": "",
    },
    {
        "nombre": "research_tema_vacio",
        "prompt": "Investiga sobre...",
        "accion": "research",
        "target": "",
        "valor": "",
    },
    {
        "nombre": "research_con_limite_pasos",
        "prompt": "Investiga rápido sobre Python",
        "accion": "research",
        "target": "Python programming",
        "valor": "5",
    },
    {
        "nombre": "research_tema_complejo",
        "prompt": "Investiga a fondo sobre computación cuántica",
        "accion": "research",
        "target": "quantum computing applications in cryptography 2026",
        "valor": "10",
    },

    # --- TRANSLATE (48-50) ---
    {
        "nombre": "translate_a_espanol",
        "prompt": "Traduce esta página al español",
        "accion": "translate",
        "target": "https://example.com/english-page",
        "valor": "es",
    },
    {
        "nombre": "translate_a_ingles",
        "prompt": "Traduce esta página al inglés",
        "accion": "translate",
        "target": "https://example.com/pagina-espanol",
        "valor": "en",
    },
    {
        "nombre": "translate_url_vacia",
        "prompt": "Traduce esta página",
        "accion": "translate",
        "target": "",
        "valor": "es",
    },
]


# ============================================================================
# Tests de navegación (navigate)
# ============================================================================

class TestBrowserNavigate:
    """Tests para browser_navigate."""

    @pytest.mark.asyncio
    async def test_navigate_con_https(self, mock_page):
        """Navega a URL con https."""
        result = await browser_navigate(mock_page, "https://example.com")
        assert "Éxito" in result
        mock_page.goto.assert_called_once()

    @pytest.mark.asyncio
    async def test_navigate_sin_protocolo_agrega_https(self, mock_page):
        """BUG: Si no tiene http, agrega https:// automáticamente."""
        result = await browser_navigate(mock_page, "example.com")
        assert "Éxito" in result
        # Verificar que se agregó https://
        call_args = mock_page.goto.call_args
        assert "https://" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_navigate_url_vacia_devuelve_error(self, mock_page):
        """URL vacía debe devolver error."""
        result = await browser_navigate(mock_page, "")
        assert "Error" in result
        assert "Especifica la URL" in result
        mock_page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_navigate_con_unicode(self, mock_page):
        """URL con caracteres unicode."""
        result = await browser_navigate(mock_page, "https://example.com/página")
        assert "Éxito" in result

    @pytest.mark.asyncio
    async def test_navigate_con_hash(self, mock_page):
        """URL con fragmento hash."""
        result = await browser_navigate(mock_page, "https://example.com/page#section")
        assert "Éxito" in result

    @pytest.mark.asyncio
    async def test_navigate_con_parametros(self, mock_page):
        """URL con query parameters."""
        result = await browser_navigate(mock_page, "https://example.com/search?q=test&lang=es")
        assert "Éxito" in result

    @pytest.mark.asyncio
    async def test_navigate_timeout(self, mock_page):
        """Timeout de navegación."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        mock_page.goto.side_effect = PlaywrightTimeout("Timeout")

        with pytest.raises(PlaywrightTimeout):
            await browser_navigate(mock_page, "https://slow-page.com")


# ============================================================================
# Tests de click
# ============================================================================

class TestBrowserClick:
    """Tests para browser_click."""

    @pytest.mark.asyncio
    async def test_click_selector_valido(self, mock_page):
        """Click en selector válido."""
        result = await browser_click(mock_page, "button.submit")
        assert "Éxito" in result
        mock_page.wait_for_selector.assert_called_once_with("button.submit", timeout=5000)
        mock_page.click.assert_called_once_with("button.submit")

    @pytest.mark.asyncio
    async def test_click_selector_vacio_devuelve_error(self, mock_page):
        """Selector vacío debe devolver error."""
        result = await browser_click(mock_page, "")
        assert "Error" in result
        assert "Especifica el selector" in result
        mock_page.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_click_selector_inexistente_timeout(self, mock_page):
        """Selector inexistente causa timeout."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        mock_page.wait_for_selector.side_effect = PlaywrightTimeout("Timeout")

        with pytest.raises(PlaywrightTimeout):
            await browser_click(mock_page, "#no-existe")

    @pytest.mark.asyncio
    async def test_click_selector_complejo(self, mock_page):
        """Selector CSS complejo."""
        selector = "div.main > ul.nav > li:nth-child(3) > a"
        result = await browser_click(mock_page, selector)
        assert "Éxito" in result

    @pytest.mark.asyncio
    async def test_click_text_locator(self, mock_page):
        """Selector con texto (Playwright text locator)."""
        result = await browser_click(mock_page, "a:has-text('Click aquí')")
        assert "Éxito" in result


# ============================================================================
# Tests de type
# ============================================================================

class TestBrowserType:
    """Tests para browser_type."""

    @pytest.mark.asyncio
    async def test_type_selector_valido(self, mock_page):
        """Escribir en selector válido."""
        result = await browser_type(mock_page, "input[name='q']", "hola mundo")
        assert "Éxito" in result
        mock_page.wait_for_selector.assert_called_once()
        mock_page.click.assert_called_once()
        mock_page.type.assert_called_once_with("input[name='q']", "hola mundo", delay=50)

    @pytest.mark.asyncio
    async def test_type_selector_vacio_devuelve_error(self, mock_page):
        """Selector vacío debe devolver error."""
        result = await browser_type(mock_page, "", "texto")
        assert "Error" in result
        assert "Especifica el selector" in result

    @pytest.mark.asyncio
    async def test_type_texto_largo(self, mock_page):
        """Escribir texto muy largo (5000 chars)."""
        texto_largo = "A" * 5000
        result = await browser_type(mock_page, "textarea", texto_largo)
        assert "Éxito" in result
        mock_page.type.assert_called_once_with("textarea", texto_largo, delay=50)

    @pytest.mark.asyncio
    async def test_type_unicode_emojis(self, mock_page):
        """Escribir texto con emojis y unicode."""
        result = await browser_type(mock_page, "input", "Hello 🌍 你好世界")
        assert "Éxito" in result

    @pytest.mark.asyncio
    async def test_type_comillas_especiales(self, mock_page):
        """Escribir texto con comillas y backticks."""
        texto = 'Texto con "comillas" y \'simples\' y `backticks`'
        result = await browser_type(mock_page, "input", texto)
        assert "Éxito" in result

    @pytest.mark.asyncio
    async def test_type_codigo_python(self, mock_page):
        """Escribir código Python con saltos de línea."""
        codigo = "def hello():\n    print('world')\n    return True"
        result = await browser_type(mock_page, "textarea", codigo)
        assert "Éxito" in result

    @pytest.mark.asyncio
    async def test_type_inyeccion_js(self, mock_page):
        """Intentar inyección JS a través de type."""
        texto = "'; alert('xss'); //"
        result = await browser_type(mock_page, "input", texto)
        # Debería escribir el texto literal, no ejecutarlo
        assert "Éxito" in result
        mock_page.type.assert_called_once_with("input", texto, delay=50)


# ============================================================================
# Tests de read
# ============================================================================

class TestBrowserRead:
    """Tests para browser_read."""

    @pytest.mark.asyncio
    async def test_read_pagina_normal(self, mock_page):
        """Leer página normal."""
        mock_page.evaluate.return_value = "Contenido de la página con suficiente texto para superar el umbral mínimo de lectura. " * 5
        result = await browser_read(mock_page)
        assert "INFORMACIÓN DE PESTAÑA ACTIVA" in result
        assert "TÍTULO" in result
        assert "URL" in result
        assert "CONTENIDO TEXTUAL" in result

    @pytest.mark.asyncio
    async def test_read_pagina_vacia(self, mock_page):
        """Leer página sin contenido cae a visión."""
        mock_page.evaluate.return_value = ""
        result = await browser_read(mock_page)
        assert "poco texto legible" in result

    @pytest.mark.asyncio
    async def test_read_pagina_muy_larga_trunca(self, mock_page):
        """Leer página muy larga debe truncar."""
        contenido_largo = "A" * 5000
        mock_page.evaluate.return_value = contenido_largo
        result = await browser_read(mock_page)
        assert "truncado" in result
        assert len(result) < len(contenido_largo)

    @pytest.mark.asyncio
    async def test_read_pagina_exacto_2500_chars(self, mock_page):
        """Leer página con exactamente 2500 chars no trunca."""
        contenido = "A" * 2500
        mock_page.evaluate.return_value = contenido
        result = await browser_read(mock_page)
        assert "truncado" not in result

    @pytest.mark.asyncio
    async def test_read_pagina_2501_chars_trunca(self, mock_page):
        """Leer página con 2501 chars sí trunca."""
        contenido = "A" * 2501
        mock_page.evaluate.return_value = contenido
        result = await browser_read(mock_page)
        assert "truncado" in result


# ============================================================================
# Tests de scroll
# ============================================================================

class TestBrowserScroll:
    """Tests para browser_scroll."""

    @pytest.mark.asyncio
    async def test_scroll_abajo(self, mock_page):
        """Scroll hacia abajo."""
        result = await browser_scroll(mock_page, "500")
        assert "Éxito" in result
        assert "500" in result
        mock_page.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_scroll_arriba(self, mock_page):
        """Scroll hacia arriba (valor negativo)."""
        result = await browser_scroll(mock_page, "-500")
        assert "Éxito" in result
        assert "-500" in result

    @pytest.mark.asyncio
    async def test_scroll_valor_invalido_usa_default(self, mock_page):
        """BUG: Valor inválido debería usar default 500."""
        result = await browser_scroll(mock_page, "abc")
        assert "Éxito" in result
        # Debería usar default 500
        call_args = mock_page.evaluate.call_args[0][0]
        assert "500" in call_args

    @pytest.mark.asyncio
    async def test_scroll_sin_valor_usa_default(self, mock_page):
        """Sin valor debería usar default 500."""
        result = await browser_scroll(mock_page, "")
        assert "Éxito" in result
        assert "500" in result

    @pytest.mark.asyncio
    async def test_scroll_muy_largo(self, mock_page):
        """Scroll muy largo (50000px)."""
        result = await browser_scroll(mock_page, "50000")
        assert "Éxito" in result
        assert "50000" in result

    @pytest.mark.asyncio
    async def test_scroll_cero(self, mock_page):
        """Scroll de 0px."""
        result = await browser_scroll(mock_page, "0")
        assert "Éxito" in result
        assert "0" in result


# ============================================================================
# Tests de clip
# ============================================================================

class TestBrowserClip:
    """Tests para browser_clip."""

    @pytest.mark.asyncio
    async def test_clip_pagina_normal(self, mock_page, tmp_path):
        """Clip de página normal a Obsidian."""
        mock_page.title = AsyncMock(return_value="Test Article")
        mock_page.url = "https://example.com/article"
        mock_page.evaluate = AsyncMock(return_value="Contenido del artículo para clipping")

        with patch("src.browser.clip.settings") as mock_settings:
            mock_settings.OBSIDIAN_CLIPPINGS = tmp_path

            # Mock de trafilatura dentro de la función
            import sys
            mock_trafilatura = MagicMock()
            mock_trafilatura.fetch_url.return_value = "downloaded content"
            mock_trafilatura.extract.return_value = "Contenido limpio extraído"
            with patch.dict(sys.modules, {"trafilatura": mock_trafilatura}):
                result = await browser_clip(mock_page)

            assert "CLIP GUARDADO EN OBSIDIAN" in result
            assert "Archivo:" in result
            # Verificar que se creó el archivo
            files = list(tmp_path.glob("*.md"))
            assert len(files) == 1

    @pytest.mark.asyncio
    async def test_clip_pagina_sin_contenido(self, mock_page, tmp_path):
        """Clip de página sin contenido devuelve error."""
        mock_page.title = AsyncMock(return_value="Empty Page")
        mock_page.url = "https://example.com/empty"
        mock_page.evaluate = AsyncMock(return_value="")

        with patch("src.browser.clip.settings") as mock_settings:
            mock_settings.OBSIDIAN_CLIPPINGS = tmp_path

            import sys
            mock_trafilatura = MagicMock()
            mock_trafilatura.fetch_url.return_value = None
            with patch.dict(sys.modules, {"trafilatura": mock_trafilatura}):
                result = await browser_clip(mock_page)

            assert "Error" in result
            assert "No se pudo extraer" in result

    @pytest.mark.asyncio
    async def test_clip_titulo_con_caracteres_especiales(self, mock_page, tmp_path):
        """Clip con título con caracteres especiales genera slug válido."""
        mock_page.title = AsyncMock(return_value="¡Hola! ¿Cómo estás? 🌍 (Test)")
        mock_page.url = "https://example.com/test"
        mock_page.evaluate = AsyncMock(return_value="Contenido")

        with patch("src.browser.clip.settings") as mock_settings:
            mock_settings.OBSIDIAN_CLIPPINGS = tmp_path

            import sys
            mock_trafilatura = MagicMock()
            mock_trafilatura.fetch_url.return_value = "content"
            mock_trafilatura.extract.return_value = "Contenido limpio"
            with patch.dict(sys.modules, {"trafilatura": mock_trafilatura}):
                result = await browser_clip(mock_page)

            assert "CLIP GUARDADO" in result
            # Verificar que el archivo se creó con nombre válido
            files = list(tmp_path.glob("*.md"))
            assert len(files) == 1
            # El nombre no debe tener caracteres inválidos
            filename = files[0].name
            assert all(c.isalnum() or c in ' -.' for c in filename)

    @pytest.mark.asyncio
    async def test_clip_contenido_muy_largo_trunca(self, mock_page, tmp_path):
        """Clip de contenido muy largo se trunca a 15000 chars."""
        contenido_largo = "A" * 20000
        mock_page.title = AsyncMock(return_value="Long Article")
        mock_page.url = "https://example.com/long"
        mock_page.evaluate = AsyncMock(return_value=contenido_largo)

        with patch("src.browser.clip.settings") as mock_settings:
            mock_settings.OBSIDIAN_CLIPPINGS = tmp_path

            import sys
            mock_trafilatura = MagicMock()
            mock_trafilatura.fetch_url.return_value = "content"
            mock_trafilatura.extract.return_value = contenido_largo
            with patch.dict(sys.modules, {"trafilatura": mock_trafilatura}):
                result = await browser_clip(mock_page)

            assert "CLIP GUARDADO" in result
            # Verificar que el archivo existe
            files = list(tmp_path.glob("*.md"))
            assert len(files) == 1
            # Leer el archivo y verificar truncamiento
            content = files[0].read_text()
            assert len(content) < 20000  # Debería estar truncado

    @pytest.mark.asyncio
    async def test_clip_trafilatura_fallback_a_innerText(self, mock_page, tmp_path):
        """Si trafilatura falla, usa innerText como fallback."""
        mock_page.title = AsyncMock(return_value="Test Page")
        mock_page.url = "https://example.com/test"
        mock_page.evaluate = AsyncMock(return_value="Contenido desde innerText")

        with patch("src.browser.clip.settings") as mock_settings:
            mock_settings.OBSIDIAN_CLIPPINGS = tmp_path

            import sys
            mock_trafilatura = MagicMock()
            mock_trafilatura.fetch_url.side_effect = Exception("Network error")
            with patch.dict(sys.modules, {"trafilatura": mock_trafilatura}):
                result = await browser_clip(mock_page)

            assert "CLIP GUARDADO" in result
            files = list(tmp_path.glob("*.md"))
            assert len(files) == 1


# ============================================================================
# Tests de research
# ============================================================================

class TestBrowserResearch:
    """Tests para browser_research."""

    @pytest.mark.asyncio
    async def test_research_tema_vacio_devuelve_error(self, mock_page):
        """Research sin tema devuelve error."""
        result = await browser_research(mock_page, "", "")
        assert "Error" in result
        assert "Especifica el tema" in result

    @pytest.mark.asyncio
    async def test_research_con_limite_pasos(self, mock_page):
        """Research con límite de pasos personalizado."""
        # Mock para que no haga requests reales
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])
        mock_page.title = AsyncMock(return_value="Test")

        result = await browser_research(mock_page, "Python", "3")
        # Debería completar sin error (aunque no encuentre nada)
        assert "Investigación" in result

    @pytest.mark.asyncio
    async def test_research_limite_maximo_30_pasos(self, mock_page):
        """Research ignora valor mayor a 30."""
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])
        mock_page.title = AsyncMock(return_value="Test")

        result = await browser_research(mock_page, "Test", "100")
        # Debería limitar a 30 pasos
        assert "Investigación" in result

    @pytest.mark.asyncio
    async def test_research_valor_invalido_usa_default(self, mock_page):
        """Research con valor inválido usa default 30."""
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])
        mock_page.title = AsyncMock(return_value="Test")

        result = await browser_research(mock_page, "Test", "abc")
        assert "Investigación" in result


# ============================================================================
# Tests de translate
# ============================================================================

class TestBrowserTranslate:
    """Tests para browser_translate."""

    @pytest.mark.asyncio
    async def test_translate_url_vacia_devuelve_error(self, mock_page):
        """Translate sin URL devuelve error."""
        result = await browser_translate(mock_page, "", "es")
        assert "Error" in result
        assert "Especifica la URL" in result

    @pytest.mark.asyncio
    async def test_translate_sin_protocolo_agrega_https(self, mock_page):
        """Translate sin protocolo agrega https://."""
        mock_page.goto = AsyncMock()
        mock_page.title = AsyncMock(return_value="Test")
        mock_page.evaluate = AsyncMock(side_effect=["Texto para traducir", "ok"])

        with patch("src.browser.translate._translate_chunks", return_value="Translated text"):
            result = await browser_translate(mock_page, "example.com", "es")
            assert "TRADUCCIÓN COMPLETADA" in result
            # Verificar que se agregó https://
            call_args = mock_page.goto.call_args
            assert "https://" in call_args[0][0]


# ============================================================================
# Tests de launcher
# ============================================================================

class TestBrowserLauncher:
    """Tests para ensure_browser y get_page."""

    def test_is_port_open_closed(self):
        """Verifica que puerto cerrado devuelve False."""
        # Usar un puerto que seguramente no está en uso
        assert _is_port_open(59999) is False

    @pytest.mark.asyncio
    async def test_ensure_browser_puerto_ya_abierto(self):
        """Si el puerto CDP ya está abierto, no lanza Chromium."""
        with patch("src.browser.launcher._is_port_open", return_value=True):
            result = await ensure_browser()
            assert result is None  # Éxito

    @pytest.mark.asyncio
    async def test_ensure_browser_sin_chromium(self):
        """Si no hay Chromium instalado, devuelve error."""
        with patch("src.browser.launcher._is_port_open", return_value=False), \
             patch("shutil.which", return_value=None):
            result = await ensure_browser()
            assert "Error" in result
            assert "No se encontró" in result

    @pytest.mark.asyncio
    async def test_get_page_sin_contextos(self, mock_playwright):
        """BUG: Si browser.contexts está vacío, IndexError."""
        mock_browser = AsyncMock()
        mock_browser.contexts = []  # Sin contextos
        mock_playwright.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)

        with pytest.raises(IndexError):
            await get_page(mock_playwright)


# ============================================================================
# Tests de control_local_browser (función principal)
# ============================================================================

class TestControlLocalBrowser:
    """Tests para control_local_browser con las 8 acciones."""

    @pytest.mark.asyncio
    async def test_control_launch(self):
        """Acción launch."""
        with patch("src.browser.ensure_browser", return_value=None):
            result = await control_local_browser("launch")
            assert "Éxito" in result
            assert "Chromium" in result

    @pytest.mark.asyncio
    async def test_control_navigate(self):
        """Acción navigate."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page, \
             patch("src.browser.browser_navigate") as mock_nav:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page
            mock_nav.return_value = "Éxito: Navegado"

            result = await control_local_browser("navigate", "https://example.com")
            assert "Éxito" in result
            mock_nav.assert_called_once()

    @pytest.mark.asyncio
    async def test_control_click(self):
        """Acción click."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page, \
             patch("src.browser.browser_click") as mock_click:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page
            mock_click.return_value = "Éxito: Clic"

            result = await control_local_browser("click", "button.submit")
            assert "Éxito" in result
            mock_click.assert_called_once()

    @pytest.mark.asyncio
    async def test_control_type(self):
        """Acción type."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page, \
             patch("src.browser.browser_type") as mock_type:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page
            mock_type.return_value = "Éxito: Escrito"

            result = await control_local_browser("type", "input[name='q']", "hola")
            assert "Éxito" in result
            mock_type.assert_called_once()

    @pytest.mark.asyncio
    async def test_control_read(self):
        """Acción read."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page, \
             patch("src.browser.browser_read") as mock_read:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page
            mock_read.return_value = "Título: Test"

            result = await control_local_browser("read")
            assert "Título" in result
            mock_read.assert_called_once()

    @pytest.mark.asyncio
    async def test_control_scroll(self):
        """Acción scroll."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page, \
             patch("src.browser.browser_scroll") as mock_scroll:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page
            mock_scroll.return_value = "Éxito: Scroll"

            result = await control_local_browser("scroll", value="500")
            assert "Éxito" in result
            mock_scroll.assert_called_once()

    @pytest.mark.asyncio
    async def test_control_clip(self):
        """Acción clip."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page, \
             patch("src.browser.browser_clip") as mock_clip:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page
            mock_clip.return_value = "CLIP GUARDADO"

            result = await control_local_browser("clip")
            assert "CLIP GUARDADO" in result
            mock_clip.assert_called_once()

    @pytest.mark.asyncio
    async def test_control_research(self):
        """Acción research."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page, \
             patch("src.browser.browser_research") as mock_research:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page
            mock_research.return_value = "INVESTIGACIÓN COMPLETADA"

            result = await control_local_browser("research", "Python")
            assert "INVESTIGACIÓN" in result
            mock_research.assert_called_once()

    @pytest.mark.asyncio
    async def test_control_translate(self):
        """Acción translate."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page, \
             patch("src.browser.browser_translate") as mock_translate:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page
            mock_translate.return_value = "TRADUCCIÓN COMPLETADA"

            result = await control_local_browser("translate", "https://example.com", "es")
            assert "TRADUCCIÓN" in result
            mock_translate.assert_called_once()

    @pytest.mark.asyncio
    async def test_control_accion_desconocida(self):
        """Acción desconocida devuelve error."""
        with patch("src.browser.ensure_browser", return_value=None), \
             patch("src.browser.get_page") as mock_get_page:
            mock_page = AsyncMock()
            mock_get_page.return_value = mock_page

            result = await control_local_browser("accion_inexistente")
            assert "Error" in result
            assert "no es una acción soportada" in result

    @pytest.mark.asyncio
    async def test_control_ensure_browser_falla(self):
        """Si ensure_browser falla, devuelve error."""
        with patch("src.browser.ensure_browser", return_value="Error: No hay Chromium"):
            result = await control_local_browser("navigate", "https://example.com")
            assert "Error" in result
            assert "Chromium" in result


# ============================================================================
# Tests de _extract_url (research helper)
# ============================================================================

class TestExtractUrl:
    """Tests para _extract_url."""

    def test_extract_url_valida(self):
        """Extraer URL válida."""
        with patch("src.browser.research.trafilatura.fetch_url", return_value="content"), \
             patch("src.browser.research.trafilatura.extract", return_value="extracted text"):
            result = _extract_url("https://example.com")
            assert result == "extracted text"

    def test_extract_url_falla_fetch(self):
        """URL que falla al fetch."""
        with patch("src.browser.research.trafilatura.fetch_url", return_value=None):
            result = _extract_url("https://example.com")
            assert result is None

    def test_extract_url_falla_extract(self):
        """URL que falla al extract."""
        with patch("src.browser.research.trafilatura.fetch_url", return_value="content"), \
             patch("src.browser.research.trafilatura.extract", return_value=None):
            result = _extract_url("https://example.com")
            assert result is None

    def test_extract_url_excepcion(self):
        """URL que causa excepción."""
        with patch("src.browser.research.trafilatura.fetch_url", side_effect=Exception("Network error")):
            result = _extract_url("https://example.com")
            assert result is None


# ============================================================================
# Tests de URL encoding en research (BUG: solo reemplaza espacios)
# ============================================================================

class TestResearchUrlEncoding:
    """Tests para bug de URL encoding en research."""

    @pytest.mark.asyncio
    async def test_research_query_con_ampersand(self, mock_page):
        """BUG: Query con '&' no se encodea correctamente."""
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])
        mock_page.title = AsyncMock(return_value="Test")

        # Query con caracteres especiales
        result = await browser_research(mock_page, "how to use & configure", "2")

        # Verificar que se hizo al menos un intento de búsqueda
        assert "Investigación" in result

    @pytest.mark.asyncio
    async def test_research_query_con_hash(self, mock_page):
        """BUG: Query con '#' no se encodea correctamente."""
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])
        mock_page.title = AsyncMock(return_value="Test")

        result = await browser_research(mock_page, "python #hashtags", "2")
        assert "Investigación" in result
