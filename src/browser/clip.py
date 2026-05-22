"""Clipping de páginas web a Obsidian."""

import asyncio
import logging
import os
import re
from datetime import datetime

from src.config import settings

logger = logging.getLogger(__name__)


async def browser_clip(page) -> str:
    """
    Guarda la pestaña activa como nota Markdown en Obsidian Vault.
    Usa trafilatura para extracción limpia, fallback a innerText.
    """
    title = await page.title()
    url = page.url
    logger.info(f"Guardando clip de '{title}' en Obsidian...")

    # 1. Extraer texto limpio usando trafilatura
    clean_content = None
    try:
        import trafilatura
        def _fetch_clean():
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                return trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=True,
                    favor_recall=True,
                )
            return None
        clean_content = await asyncio.to_thread(_fetch_clean)
    except Exception as e:
        logger.warning(f"trafilatura falló ({e}), usando innerText como fallback.")

    if not clean_content:
        clean_content = await page.evaluate("document.body.innerText")

    if not clean_content:
        return "Error: No se pudo extraer contenido textual de la página actual."

    # 2. Nombre del archivo
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    slug = re.sub(r'[^\w\s-]', '', title.lower())
    slug = re.sub(r'[\s_-]+', '-', slug).strip('-')[:60]
    filename = f"{date_str} - {slug}.md"

    vault_dir = settings.OBSIDIAN_CLIPPINGS
    filepath = os.path.join(vault_dir, filename)

    # 3. Frontmatter YAML
    frontmatter = (
        f"---\n"
        f"título: \"{title}\"\n"
        f"url: {url}\n"
        f"fecha_captura: {date_str} {time_str}\n"
        f"etiquetas: [clipping, por-revisar]\n"
        f"---\n\n"
    )

    # 4. Contenido Markdown (truncar a 15.000 chars)
    max_chars = 15000
    body = clean_content
    truncated = False
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated = True

    md_content = (
        frontmatter
        + f"# {title}\n\n"
        + f"> **Fuente:** [{url}]({url})\n\n"
        + f"---\n\n"
        + f"{body}\n"
    )
    if truncated:
        md_content += "\n\n---\n*[Contenido truncado — artículo completo disponible en la URL fuente]*\n"

    # 5. Escribir archivo
    os.makedirs(vault_dir, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(md_content)

    # 6. Resumen compacto para el LLM
    word_count = len(clean_content.split())
    summary_preview = clean_content[:400].strip()

    return (
        f"CLIP GUARDADO EN OBSIDIAN:\n"
        f"- Archivo: {filename}\n"
        f"- Palabras extraídas: {word_count}\n"
        f"EXTRACTO INICIAL (para tu resumen oral al usuario):\n{summary_preview}\n"
    )
