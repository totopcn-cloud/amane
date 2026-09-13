"""Kodi ``<movie>`` NFO, 供 Emby / Jellyfin / Kodi 读取."""

from __future__ import annotations

import re
from io import StringIO
from typing import TYPE_CHECKING

import aiofiles
import structlog

from ..parsing import classification_tags

if TYPE_CHECKING:
    from pathlib import Path

    from ..db.models import Metadata
    from ..parsing import FileInfo


logger = structlog.get_logger()


_XML_ESCAPE_MAP: dict[str, str] = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&apos;",
    '"': "&quot;",
}


def _escape_xml(text: str) -> str:
    for char, entity in _XML_ESCAPE_MAP.items():
        text = text.replace(char, entity)
    return text


async def update_nfo_classification(nfo_path: Path, *, uncensored: bool, amateur: bool) -> bool:
    """仅向已有 NFO 补充分类节点, 保留原文本与所有资源字段."""
    try:
        async with aiofiles.open(nfo_path, "r", encoding="UTF-8") as f:
            xml = await f.read()
        additions: list[str] = []
        if uncensored and not re.search(r"<tag>\s*(?:无码|無碼)\s*</tag>", xml, re.IGNORECASE):
            additions.append("  <tag>无码</tag>")
        if amateur and not re.search(r"<tag>\s*素人\s*</tag>", xml):
            additions.append("  <tag>素人</tag>")
        if uncensored and not re.search(r"<genre>\s*无码专区\s*</genre>", xml):
            additions.append("  <genre>无码专区</genre>")
        if not additions:
            return True
        marker = "</movie>"
        if marker not in xml:
            return False
        updated = xml.replace(marker, "\n".join(additions) + "\n" + marker, 1)
        async with aiofiles.open(nfo_path, "w", encoding="UTF-8") as f:
            await f.write(updated)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        logger.exception("nfo classification update failed", path=str(nfo_path))
        return False


async def write_nfo(metadata: Metadata, nfo_path: Path, *, file_info: FileInfo | None = None) -> bool:
    try:
        nfo_path.parent.mkdir(parents=True, exist_ok=True)

        code = StringIO()
        code.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
        code.write("<movie>\n")

        if metadata.plot:
            plot_escaped = _escape_xml(metadata.plot)
            code.write(f"  <plot><![CDATA[{metadata.plot}]]></plot>\n")
            code.write(f"  <outline>{plot_escaped}</outline>\n")

        if metadata.release:
            code.write(f"  <premiered>{metadata.release}</premiered>\n")
            code.write(f"  <releasedate>{metadata.release}</releasedate>\n")
            year_match = re.match(r"(\d{4})", metadata.release)
            if year_match:
                code.write(f"  <year>{year_match.group(1)}</year>\n")

        code.write(f"  <num>{_escape_xml(metadata.number)}</num>\n")

        if metadata.title:
            code.write(f"  <title>{_escape_xml(metadata.number)} {_escape_xml(metadata.title)}</title>\n")
            code.write(
                f"  <originaltitle>{_escape_xml(metadata.number)} {_escape_xml(metadata.title)}</originaltitle>\n"
            )
            code.write(f"  <sorttitle>{_escape_xml(metadata.number)} {_escape_xml(metadata.title)}</sorttitle>\n")

        code.write("  <mpaa>JP-18+</mpaa>\n")

        if metadata.actors:
            for actor in metadata.actors:
                code.write("  <actor>\n")
                code.write(f"    <name>{_escape_xml(actor)}</name>\n")
                code.write("    <type>Actor</type>\n")
                code.write("  </actor>\n")

        if metadata.score is not None:
            code.write(f"  <rating>{metadata.score}</rating>\n")
            code.write(f"  <criticrating>{int(metadata.score * 10)}</criticrating>\n")

        if metadata.runtime is not None:
            code.write(f"  <runtime>{metadata.runtime}</runtime>\n")

        if metadata.series:
            code.write(f"  <series>{_escape_xml(metadata.series)}</series>\n")
            code.write("  <set>\n")
            code.write(f"    <name>{_escape_xml(metadata.series)}</name>\n")
            code.write("  </set>\n")

        if metadata.studio:
            code.write(f"  <studio>{_escape_xml(metadata.studio)}</studio>\n")
            code.write(f"  <maker>{_escape_xml(metadata.studio)}</maker>\n")

        if metadata.publisher:
            code.write(f"  <publisher>{_escape_xml(metadata.publisher)}</publisher>\n")
            code.write(f"  <label>{_escape_xml(metadata.publisher)}</label>\n")

        tags = list(metadata.tags)
        if file_info is not None:
            uncensored, amateur = classification_tags(file_info)
            if uncensored and "无码" not in tags:
                tags.append("无码")
            if amateur and "素人" not in tags:
                tags.append("素人")

        if tags:
            for tag in tags:
                if tag:
                    code.write(f"  <tag>{_escape_xml(tag)}</tag>\n")
                    if tag != "无码":
                        code.write(f"  <genre>{_escape_xml(tag)}</genre>\n")

        if file_info is not None:
            uncensored, _ = classification_tags(file_info)
            if uncensored:
                code.write("  <genre>无码专区</genre>\n")

        if metadata.poster_url:
            code.write(f"  <poster>{_escape_xml(metadata.poster_url)}</poster>\n")

        if metadata.thumb_url:
            code.write(f"  <cover>{_escape_xml(metadata.thumb_url)}</cover>\n")

        if metadata.trailer_url:
            code.write(f"  <trailer>{_escape_xml(metadata.trailer_url)}</trailer>\n")

        if metadata.directors:
            for director in metadata.directors:
                code.write(f"  <director>{_escape_xml(director)}</director>\n")

        if metadata.external_ids:
            for site, ext_id in metadata.external_ids.items():
                if ext_id:
                    code.write(f"  <{site}id>{_escape_xml(str(ext_id))}</{site}id>\n")

        code.write("</movie>\n")

        async with aiofiles.open(nfo_path, "w", encoding="UTF-8") as f:
            await f.write(code.getvalue())

        logger.debug("nfo written", path=str(nfo_path))
        return True

    except Exception:
        logger.exception("nfo write failed", path=str(nfo_path))
        return False
