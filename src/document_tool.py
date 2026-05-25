"""Genera documentos .odt formateados (LibreOffice) a partir de Markdown.

Un .odt es solo un ZIP con XML dentro (mimetype, content.xml, styles.xml, meta.xml,
META-INF/manifest.xml). Los construimos a mano con la stdlib (zipfile) y validamos que
el XML es bien-formado con lxml antes de empaquetar. No requiere instalar nada en
LibreOffice ni dependencias externas nuevas.

Soporta un subconjunto de Markdown suficiente para cartas/notas/informes:
- encabezados (#, ##, ###), párrafos, viñetas (-, *), listas numeradas (1.),
  negrita (**...**) y cursiva (*...*).
"""

import asyncio
import logging
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from lxml import etree

logger = logging.getLogger(__name__)

DOCS_DIR = Path.home() / "Documentos"
_MIMETYPE = "application/vnd.oasis.opendocument.text"

# Documento pendiente de redactar en la 2ª pasada (solo el título). Lo fija create_document
# y lo consume AssistantService, que genera el CONTENIDO como texto normal en streaming, no
# como argumento de tool (el parser de tool-calls del modelo se rompe con strings largas).
_pending_document: str | None = None


def stage_document(title: str) -> None:
    """Registra una petición de documento para que AssistantService la redacte (2ª pasada)."""
    global _pending_document
    _pending_document = title


def get_pending_document() -> str | None:
    """Devuelve y limpia el título del documento pendiente, o None."""
    global _pending_document
    pending = _pending_document
    _pending_document = None
    return pending


class DocumentError(Exception):
    """Errores al generar el documento."""


# --- XML estático (namespaces declarados en cada raíz) ---

_MANIFEST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
    '<manifest:file-entry manifest:full-path="/" manifest:version="1.3" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
    '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
    '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
    '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>'
    '</manifest:manifest>'
)

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document-styles '
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.3">'
    '<office:styles>'
    '<style:style style:name="Standard" style:family="paragraph" style:class="text">'
    '<style:paragraph-properties fo:margin-bottom="0.212cm"/>'
    '<style:text-properties fo:font-size="12pt"/></style:style>'
    '<style:style style:name="Title" style:family="paragraph" style:parent-style-name="Standard">'
    '<style:paragraph-properties fo:margin-bottom="0.5cm" fo:text-align="center"/>'
    '<style:text-properties fo:font-size="24pt" fo:font-weight="bold"/></style:style>'
    '<style:style style:name="Heading_20_1" style:display-name="Heading 1" style:family="paragraph" style:parent-style-name="Standard">'
    '<style:paragraph-properties fo:margin-top="0.4cm" fo:margin-bottom="0.2cm"/>'
    '<style:text-properties fo:font-size="18pt" fo:font-weight="bold"/></style:style>'
    '<style:style style:name="Heading_20_2" style:display-name="Heading 2" style:family="paragraph" style:parent-style-name="Standard">'
    '<style:paragraph-properties fo:margin-top="0.35cm" fo:margin-bottom="0.2cm"/>'
    '<style:text-properties fo:font-size="16pt" fo:font-weight="bold"/></style:style>'
    '<style:style style:name="Heading_20_3" style:display-name="Heading 3" style:family="paragraph" style:parent-style-name="Standard">'
    '<style:paragraph-properties fo:margin-top="0.3cm" fo:margin-bottom="0.2cm"/>'
    '<style:text-properties fo:font-size="14pt" fo:font-weight="bold"/></style:style>'
    '</office:styles></office:document-styles>'
)

_CONTENT_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document-content '
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" office:version="1.3">'
    '<office:automatic-styles>'
    '<style:style style:name="T_bold" style:family="text">'
    '<style:text-properties fo:font-weight="bold"/></style:style>'
    '<style:style style:name="T_italic" style:family="text">'
    '<style:text-properties fo:font-style="italic"/></style:style>'
    '<text:list-style style:name="L_bullet">'
    '<text:list-level-style-bullet text:level="1" text:bullet-char="•">'
    '<style:list-level-properties text:space-before="0.6cm" text:min-label-width="0.6cm"/>'
    '</text:list-level-style-bullet></text:list-style>'
    '<text:list-style style:name="L_number">'
    '<text:list-level-style-number text:level="1" style:num-format="1" text:num-suffix=".">'
    '<style:list-level-properties text:space-before="0.6cm" text:min-label-width="0.6cm"/>'
    '</text:list-level-style-number></text:list-style>'
    '</office:automatic-styles>'
    '<office:body><office:text>'
)
_CONTENT_TAIL = '</office:text></office:body></office:document-content>'

# Markdown inline: **negrita** o *cursiva* (y __negrita__ / _cursiva_).
_INLINE_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|\*(.+?)\*|_(.+?)_")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_RULE_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_FENCE_RE = re.compile(r"^\s*```")


def _inline(text: str) -> str:
    """Convierte negrita/cursiva Markdown a <text:span> y escapa el resto."""
    out = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        out.append(escape(text[pos:m.start()]))
        bold = m.group(1) if m.group(1) is not None else m.group(2)
        ital = m.group(3) if m.group(3) is not None else m.group(4)
        if bold is not None:
            out.append(f'<text:span text:style-name="T_bold">{escape(bold)}</text:span>')
        else:
            out.append(f'<text:span text:style-name="T_italic">{escape(ital)}</text:span>')
        pos = m.end()
    out.append(escape(text[pos:]))
    return "".join(out)


def _markdown_to_body(content: str) -> str:
    """Parsea el subconjunto de Markdown y devuelve el XML del cuerpo del documento."""
    blocks: list[str] = []
    para: list[str] = []
    list_items: list[str] = []
    list_kind: str | None = None  # 'bullet' | 'number'
    in_fence = False

    def flush_para():
        if para:
            text = " ".join(para).strip()
            if text:
                blocks.append(f'<text:p text:style-name="Standard">{_inline(text)}</text:p>')
            para.clear()

    def flush_list():
        nonlocal list_kind
        if list_items:
            style = "L_bullet" if list_kind == "bullet" else "L_number"
            items = "".join(
                f'<text:list-item><text:p text:style-name="Standard">{it}</text:p></text:list-item>'
                for it in list_items
            )
            blocks.append(f'<text:list text:style-name="{style}">{items}</text:list>')
            list_items.clear()
        list_kind = None

    for raw in content.split("\n"):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            flush_para()
            flush_list()
            continue
        if in_fence:
            if raw.strip():
                blocks.append(f'<text:p text:style-name="Standard">{escape(raw)}</text:p>')
            continue

        stripped = raw.strip()
        if not stripped or _RULE_RE.match(raw):
            flush_para()
            flush_list()
            continue

        h = _HEADING_RE.match(stripped)
        if h:
            flush_para()
            flush_list()
            level = min(len(h.group(1)), 3)
            blocks.append(
                f'<text:h text:style-name="Heading_20_{level}" text:outline-level="{level}">'
                f'{_inline(h.group(2).strip())}</text:h>'
            )
            continue

        b = _BULLET_RE.match(raw)
        if b:
            flush_para()
            if list_kind == "number":
                flush_list()
            list_kind = "bullet"
            list_items.append(_inline(b.group(1).strip()))
            continue

        n = _NUMBER_RE.match(raw)
        if n:
            flush_para()
            if list_kind == "bullet":
                flush_list()
            list_kind = "number"
            list_items.append(_inline(n.group(1).strip()))
            continue

        flush_list()
        para.append(stripped)

    flush_para()
    flush_list()
    return "".join(blocks)


def _build_content(title: str, content: str) -> str:
    body = ""
    if title.strip():
        body += f'<text:p text:style-name="Title">{_inline(title.strip())}</text:p>'
    body += _markdown_to_body(content or "")
    if not body:
        body = '<text:p text:style-name="Standard"></text:p>'
    return _CONTENT_HEAD + body + _CONTENT_TAIL


def _build_meta(title: str) -> str:
    iso = datetime.now().astimezone().replace(microsecond=0).isoformat()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-meta '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" office:version="1.3"><office:meta>'
        '<meta:generator>AsistenteIA</meta:generator>'
        f'<dc:title>{escape(title.strip() or "Documento")}</dc:title>'
        f'<meta:creation-date>{iso}</meta:creation-date>'
        f'<dc:date>{iso}</dc:date>'
        '</office:meta></office:document-meta>'
    )


def _safe_filename(filename: str, title: str) -> str:
    base = (filename or title or "documento").strip()
    if base.lower().endswith(".odt"):
        base = base[:-4]
    base = re.sub(r"[^\w\s.\-]", "", base, flags=re.UNICODE).strip()
    base = re.sub(r"\s+", "_", base)
    return (base or "documento") + ".odt"


def _unique_path(filename: str) -> Path:
    path = DOCS_DIR / filename
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        candidate = DOCS_DIR / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    return DOCS_DIR / f"{stem}_{datetime.now():%H%M%S}{suffix}"


def _write_odt(path: Path, content_xml: str, meta_xml: str) -> None:
    # Validar que el XML generado es bien-formado antes de empaquetar.
    for name, xml in (("content.xml", content_xml), ("styles.xml", _STYLES), ("meta.xml", meta_xml)):
        try:
            etree.fromstring(xml.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            raise DocumentError(f"XML inválido en {name}: {e}")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype: primer elemento y SIN comprimir (requisito ODF).
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, _MIMETYPE)
        z.writestr("content.xml", content_xml)
        z.writestr("styles.xml", _STYLES)
        z.writestr("meta.xml", meta_xml)
        z.writestr("META-INF/manifest.xml", _MANIFEST)


def _open_in_libreoffice(path: Path) -> bool:
    for cmd in (["soffice", str(path)], ["xdg-open", str(path)]):
        try:
            subprocess.Popen(cmd, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f"No se pudo abrir el documento con {cmd[0]}: {e}")
    return False


async def build_and_save_document(title: str, markdown: str) -> str:
    """Genera el .odt desde el Markdown ya redactado, lo guarda en ~/Documentos y lo abre.

    La llama AssistantService tras la 2ª pasada (no el modelo). Devuelve el mensaje de
    confirmación que se le dirá al usuario.
    """
    title = (title or "Documento").strip()
    try:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        path = _unique_path(_safe_filename("", title))
        content_xml = _build_content(title, markdown or "")
        meta_xml = _build_meta(title)
        await asyncio.to_thread(_write_odt, path, content_xml, meta_xml)
    except DocumentError as e:
        logger.error(f"Error generando el documento: {e}")
        return f"No pude generar el documento: {e}"
    except Exception as e:
        logger.error(f"Error inesperado generando el documento: {e}", exc_info=True)
        return f"Error creando el documento: {e}"

    opened = _open_in_libreoffice(path)
    estado = "creado y abierto en LibreOffice" if opened else f"guardado en {path}"
    return f"Documento '{path.name}' {estado} (en ~/Documentos)."


async def create_document(title: str) -> str:
    """Crea un documento .odt formateado (LibreOffice) y lo abre; el contenido se redacta solo. Para "hazme un documento/carta/informe/CV", "guárdame esto en un documento". Pasa solo un título breve.

    Args:
        title: título breve del documento (ej. "CV desarrollador", "Carta de presentación").
    """
    stage_document((title or "").strip() or "Documento")
    return "Preparando el documento."
