"""Investigación web profunda (deep research)."""

import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Optional

import trafilatura

from src.config import settings

logger = logging.getLogger(__name__)

_JS_GOOGLE_LINKS = """
    () => {
        const results = [];
        const seen = new Set();
        const selectors = [
            '#search a[href^="https"]',
            '#rso a[href^="https"]',
            '.g a[href^="https"]',
            'a[href^="https"]'
        ];
        const skipDomains = ['google.com', 'google.es', 'youtube.com',
                            'accounts.google', 'support.google',
                            'maps.google', 'translate.google',
                            'webcache', 'policies.google'];
        for (const sel of selectors) {
            document.querySelectorAll(sel).forEach(a => {
                const href = a.href || '';
                const text = (a.innerText || a.textContent || '').trim();
                const skip = skipDomains.some(d => href.includes(d));
                if (href && !skip && !seen.has(href) && text.length > 5) {
                    seen.add(href);
                    results.push({url: href, title: text.substring(0, 120)});
                }
            });
            if (results.length >= 10) break;
        }
        return results.slice(0, 10);
    }
"""


def _extract_url(url: str) -> Optional[str]:
    """Extrae texto limpio de una URL usando trafilatura."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        return trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:
        return None


async def browser_research(page, target: str, value: str) -> str:
    """
    Investigación profunda y persistente sobre un tema.
    Navega, busca y recopila información de múltiples fuentes web
    de forma autónoma, hasta un máximo de 30 pasos.
    """
    if not target:
        return "Error: Especifica el tema o pregunta a investigar en el argumento 'target'."

    max_steps = 30
    if value:
        try:
            max_steps = min(int(value), 30)
        except ValueError:
            pass

    query = target
    logger.info(f"Iniciando investigación profunda ({max_steps} pasos máx): {query}")

    visited_urls: set[str] = set()
    gathered: list[dict] = []
    pending_urls: list[tuple[str, str]] = []
    step = 0
    search_attempts = 0
    search_variants = [
        query,
        query + " explicación detallada",
        query + " site:wikipedia.org",
        query + " tutorial guía",
        query + " cómo funciona",
    ]

    while step < max_steps:
        step += 1
        logger.info(f"[Research] Paso {step}/{max_steps} | Fuentes: {len(gathered)} | Pendientes: {len(pending_urls)}")

        # FASE 1: Si no hay URLs pendientes, lanzar nueva búsqueda
        if not pending_urls:
            if search_attempts >= len(search_variants):
                logger.info("[Research] Agotadas todas las variantes de búsqueda.")
                break

            current_query = search_variants[search_attempts]
            search_attempts += 1
            logger.info(f"[Research] Búsqueda #{search_attempts}: '{current_query}'")

            encoded_q = current_query.replace(' ', '+')
            search_url = f"https://www.google.com/search?q={encoded_q}&hl=es"
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2.0)
            except Exception as e:
                logger.warning(f"[Research] Error navegando a Google: {e}")
                try:
                    ddg_url = f"https://duckduckgo.com/?q={encoded_q}"
                    await page.goto(ddg_url, wait_until="domcontentloaded", timeout=12000)
                    await asyncio.sleep(2.0)
                except Exception as e2:
                    logger.warning(f"[Research] Fallback DDG también falló: {e2}")
                    continue

            links = await page.evaluate(_JS_GOOGLE_LINKS)
            logger.info(f"[Research] Links extraídos de búsqueda: {len(links)}")

            for link in links:
                url_item = link.get('url', '')
                title_item = link.get('title', url_item)
                if url_item and url_item not in visited_urls:
                    pending_urls.append((url_item, title_item))

            logger.info(f"[Research] {len(pending_urls)} URLs encoladas.")
            continue

        # FASE 2: Visitar siguiente URL pendiente
        next_url, next_title = pending_urls.pop(0)
        if next_url in visited_urls:
            continue
        visited_urls.add(next_url)

        logger.info(f"[Research] Visitando: {next_url}")
        try:
            await page.goto(next_url, wait_until="domcontentloaded", timeout=12000)
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.warning(f"[Research] No se pudo navegar a {next_url}: {e}")
            continue

        content = await asyncio.to_thread(_extract_url, next_url)
        if not content:
            try:
                content = await page.evaluate("document.body.innerText")
            except Exception:
                content = None

        if content and len(content.strip()) > 200:
            snippet = content.strip()[:2500]
            page_title = await page.title()
            gathered.append({
                "url": next_url,
                "titulo": page_title or next_title,
                "contenido": snippet,
                "paso": step,
            })
            logger.info(f"[Research] Fuente #{len(gathered)} añadida: '{page_title}' ({len(snippet)} chars)")

            if len(gathered) >= 8:
                logger.info("[Research] Suficientes fuentes recopiladas. Finalizando bucle.")
                break

    # Compilar informe
    if not gathered:
        return f"Investigación completada en {step} pasos pero no se encontró contenido útil sobre: {query}"

    now = datetime.now()
    report_lines = [
        f"INFORME DE INVESTIGACIÓN PROFUNDA",
        f"Tema: {query}",
        f"Pasos ejecutados: {step}/{max_steps}",
        f"Fuentes recopiladas: {len(gathered)}",
        f"Fecha: {now.strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
    ]
    for i, src in enumerate(gathered, 1):
        report_lines.append(f"--- FUENTE {i}: {src['titulo']} ---")
        report_lines.append(f"URL: {src['url']}")
        report_lines.append(f"Paso de captura: {src['paso']}")
        report_lines.append("")
        report_lines.append(src['contenido'])
        report_lines.append("")

    full_report = "\n".join(report_lines)

    # Guardar en Obsidian
    obsidian_note = "(no guardado)"
    try:
        vault_dir = settings.OBSIDIAN_CLIPPINGS
        os.makedirs(vault_dir, exist_ok=True)
        date_str = now.strftime("%Y-%m-%d")
        slug = re.sub(r'[^\w\s-]', '', query.lower())
        slug = re.sub(r'[\s_-]+', '-', slug).strip('-')[:50]
        filename = f"{date_str} - investigacion - {slug}.md"
        filepath = os.path.join(vault_dir, filename)

        md_lines = [
            f"---",
            f"título: \"Investigación: {query}\"",
            f"tipo: investigacion",
            f"fecha_captura: {date_str} {now.strftime('%H:%M')}",
            f"fuentes: {len(gathered)}",
            f"pasos: {step}",
            f"etiquetas: [investigacion, por-revisar]",
            f"---",
            f"",
            f"# Investigación: {query}",
            f"",
            f"**Pasos ejecutados:** {step} | **Fuentes:** {len(gathered)} | **Fecha:** {now.strftime('%Y-%m-%d %H:%M')}",
            f"",
            f"---",
            f"",
        ]
        for i, src in enumerate(gathered, 1):
            md_lines.append(f"## Fuente {i}: {src['titulo']}")
            md_lines.append(f"> [{src['url']}]({src['url']})")
            md_lines.append(f"")
            md_lines.append(src['contenido'])
            md_lines.append(f"")
            md_lines.append(f"---")
            md_lines.append(f"")

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("\n".join(md_lines))
        logger.info(f"[Research] Informe guardado en Obsidian: {filename}")
        obsidian_note = filename
    except Exception as e:
        logger.warning(f"[Research] No se pudo guardar en Obsidian: {e}")

    # Resumen compacto para el LLM
    chars_budget = 900
    compact_lines = [
        f"INVESTIGACIÓN COMPLETADA: '{query}'",
        f"Pasos: {step} | Fuentes: {len(gathered)} | Nota Obsidian: {obsidian_note}",
        "",
    ]
    for i, src in enumerate(gathered, 1):
        entry = f"[{i}] {src['titulo']} ({src['url']})\n{src['contenido'][:120]}..."
        if sum(len(l) for l in compact_lines) + len(entry) > chars_budget:
            compact_lines.append(f"... y {len(gathered)-i+1} fuentes más en Obsidian.")
            break
        compact_lines.append(entry)
        compact_lines.append("")

    return "\n".join(compact_lines)
