"""Traducción de páginas web con panel overlay."""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_JS_EXTRACT = """() => {
    const skipTags = new Set(['SCRIPT','STYLE','NOSCRIPT','NAV','FOOTER','HEADER','ASIDE','IFRAME']);
    const sel = 'h1,h2,h3,h4,h5,h6,p,li,td,th,figcaption,blockquote,dd,dt,article,section';
    const blocks = [];
    document.querySelectorAll(sel).forEach(el => {
        if (skipTags.has(el.tagName)) return;
        const t = el.innerText.trim();
        if (t.length > 2) {
            const tag = el.tagName.toLowerCase();
            if (tag.length === 2 && tag.charAt(0) === 'h' && '123456'.indexOf(tag.charAt(1)) !== -1) {
                blocks.push('\\n## ' + t + '\\n');
            } else if (tag === 'li') {
                blocks.push('- ' + t);
            } else {
                blocks.push(t);
            }
        }
    });
    return blocks.join('\\n\\n');
}"""

_JS_OVERLAY = """(data) => {
    const overlay = document.createElement('div');
    overlay.id = '_asistenteia_tr';
    overlay.style.cssText = 'position:fixed;top:0;right:0;width:50%;height:100vh;background:rgba(255,255,255,0.97);overflow-y:auto;z-index:2147483647;font-family:system-ui,sans-serif;font-size:15px;line-height:1.75;color:#1a1a1a;padding:28px 32px;border-left:4px solid #1a73e8;box-shadow:-6px 0 30px rgba(0,0,0,0.18)';
    const existing = document.getElementById('_asistenteia_tr');
    if (existing) existing.remove();
    if (!document.getElementById('_trStyle')) {
        const style = document.createElement('style');
        style.id = '_trStyle';
        style.textContent = '@keyframes _trSlideIn{from{transform:translateX(100%)}to{transform:translateX(0)}}';
        document.head.appendChild(style);
        overlay.style.animation = '_trSlideIn 0.35s ease-out';
    } else {
        overlay.style.animation = '_trSlideIn 0.35s ease-out';
    }
    const closeBtn = document.createElement('button');
    closeBtn.textContent = '\\u2715';
    closeBtn.style.cssText = 'position:sticky;top:0;float:right;background:#1a73e8;color:white;border:none;border-radius:50%;width:36px;height:36px;font-size:20px;cursor:pointer;box-shadow:0 2px 10px rgba(26,115,232,0.4);z-index:2;display:flex;align-items:center;justify-content:center';
    closeBtn.onmouseover = function() { closeBtn.style.background = '#1557b0'; };
    closeBtn.onmouseout = function() { closeBtn.style.background = '#1a73e8'; };
    closeBtn.onclick = function() { overlay.remove(); };
    overlay.appendChild(closeBtn);
    const header = document.createElement('div');
    header.style.cssText = 'clear:both;margin-bottom:20px';
    header.innerHTML = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px"><span style="font-size:28px">\\uD83C\\uDF10</span><h2 style="color:#1a73e8;margin:0;font-size:22px">Traduccion al ' + data.lang.toUpperCase() + '</h2></div><p style="color:#666;margin:0;font-size:13px"><strong>' + data.title + '</strong><br><a href="' + data.url + '" style="color:#1a73e8">' + data.url + '</a></p><hr style="border:none;border-top:1px solid #e0e0e0;margin:14px 0">';
    overlay.appendChild(header);
    const contentEl = document.createElement('div');
    contentEl.style.cssText = 'max-width:100%;word-wrap:break-word';
    const safe = data.translated.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const lines = safe.split('\\n');
    let html = '';
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.startsWith('## ')) {
            html += '<h3 style="color:#1a73e8;margin:18px 0 8px;font-size:18px">' + line.substring(3) + '</h3>';
        } else if (line.startsWith('- ')) {
            html += '<li style="margin-left:20px;margin-bottom:4px">' + line.substring(2) + '</li>';
        } else if (line.trim() === '') {
            html += '<br>';
        } else {
            html += line;
        }
    }
    contentEl.innerHTML = '<p style="margin:0 0 12px">' + html + '</p>';
    if (data.truncated) {
        contentEl.innerHTML += '<p style="color:#999;font-style:italic;margin-top:16px">[Contenido truncado]</p>';
    }
    overlay.appendChild(contentEl);
    document.body.appendChild(overlay);
    return 'ok';
}"""


def _get_translator():
    """Obtiene GoogleTranslator, instalando si es necesario."""
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator
    except ImportError:
        import subprocess as _sp
        _sp.run(
            ["pip3", "install", "deep-translator", "--break-system-packages", "-q"],
            check=True, timeout=60,
        )
        from deep_translator import GoogleTranslator
        return GoogleTranslator


def _translate_chunks(text: str, target_lang: str) -> str:
    """Traduce texto en chunks para evitar límites de API."""
    GoogleTranslator = _get_translator()
    translator = GoogleTranslator(source='auto', target=target_lang)
    CHUNK_SIZE = 4500
    parts = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end]
        if end < len(text):
            nl = chunk.rfind('\n')
            if nl > CHUNK_SIZE // 2:
                chunk = chunk[:nl]
                start += len(chunk) + 1
            else:
                start = end
        else:
            start = end
        try:
            t = translator.translate(chunk)
            parts.append(t if t else chunk)
        except Exception:
            parts.append(chunk)
    return '\n\n'.join(parts)


async def browser_translate(page, target: str, value: str) -> str:
    """
    Traduce una página web completa e inyecta un panel overlay.
    """
    if not target:
        return "Error: Especifica la URL de la página a traducir en el argumento 'target'."

    url = target if target.startswith("http") else "https://" + target
    lang = value if value else "es"
    logger.info(f"Traduciendo página a '{lang}': {url}")

    # 1. Navegar
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(1.5)
    except Exception as e:
        return f"Error navegando a {url}: {e}"

    page_title = await page.title()
    logger.info(f"Página cargada: {page_title}")

    # 2. Extraer texto
    raw_text = await page.evaluate(_JS_EXTRACT)
    if not raw_text or len(raw_text.strip()) < 10:
        raw_text = await page.evaluate("document.body.innerText")
        if not raw_text or len(raw_text.strip()) < 10:
            return f"No se pudo extraer texto traducible de {url}."

    MAX_CHARS = 12000
    truncated = False
    if len(raw_text) > MAX_CHARS:
        raw_text = raw_text[:MAX_CHARS]
        truncated = True
        logger.info(f"Texto truncado a {MAX_CHARS} caracteres para traducción")

    # 3. Traducir
    translated = await asyncio.to_thread(_translate_chunks, raw_text, lang)
    if not translated or not translated.strip():
        return "Error: La traducción devolvió un resultado vacío."

    # 4. Inyectar panel overlay
    await page.evaluate(
        _JS_OVERLAY,
        {"translated": translated, "title": page_title, "url": url, "lang": lang, "truncated": truncated},
    )

    word_count = len(translated.split())
    logger.info(f"Traducción completada: {word_count} palabras, idioma: {lang}")

    return (
        f"TRADUCCIÓN COMPLETADA\n"
        f"Página: {page_title}\n"
        f"URL: {url}\n"
        f"Palabras traducidas: {word_count}\n"
        f"Idioma destino: {lang}\n\n"
        f"Se ha inyectado un panel de traducción en el lado derecho del navegador.\n"
        f"El usuario puede ver la traducción directamente en la página.\n"
        f"Pulsa el botón ✕ del panel para cerrarlo."
    )
