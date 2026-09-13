"""为已有 NFO 补写无码与素人分类."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..db.models import Metadata
from ..library import LibraryFileKind, LibraryScan
from ..media import update_nfo_classification, write_nfo
from ..parsing import classification_tags, parse_file_info
from ._common import scan_library
from .models import RebuildTagsPayload, RebuildTagsResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..db.repository import Repository

logger = structlog.get_logger()


class RebuildTagsHandler(TaskHandler[RebuildTagsPayload, RebuildTagsResult]):
    def __init__(self, repo: Repository) -> None:
        super().__init__(payload_t=RebuildTagsPayload, result_t=RebuildTagsResult)
        self._repo = repo

    async def handle(self, payload: RebuildTagsPayload) -> TaskResult[RebuildTagsResult]:
        library = await self._repo.get_library(payload.library_id)
        if library is None:
            return TaskResult(False, error=f"Library {payload.library_id} not found")

        # 直接走磁盘，不能依赖 Amane 已登记的 MediaFile；这样旧库也能一次补齐 NFO。
        scan = LibraryScan(
            patterns=payload.patterns,
            trailer_pattern=library.trailer_pattern,
            blacklist_patterns=library.blacklist_patterns,
            min_file_size=library.min_file_size,
        )
        hits = await scan_library(Path(payload.path), recursive=bool(payload.recursive), scan=scan)
        files = [hit.path for hit in hits if hit.kind is LibraryFileKind.MEDIA]

        scanned = created = updated = skipped = failed = 0
        total = len(files)
        await self.report_progress(0, total, "rebuild tags")
        for index, file_path in enumerate(files, start=1):
            scanned += 1
            nfo_path = file_path.with_suffix(".nfo")
            info = parse_file_info(file_path)
            media_file = await self._repo.get_media_file_by_path(str(file_path))
            metadata = (
                await self._repo.get_metadata(media_file.metadata_id)
                if media_file is not None and media_file.metadata_id is not None
                else None
            )
            uncensored, amateur = classification_tags(info)
            if metadata is not None and "素人" in metadata.tags:
                amateur = True

            if not nfo_path.is_file():
                # 没有库记录时也可按番号补最小 NFO；不会执行整理、移动或资源下载。
                if metadata is None and info.number is None:
                    skipped += 1
                else:
                    nfo_metadata = metadata or Metadata(number=info.number or file_path.stem)
                    if await write_nfo(nfo_metadata, nfo_path, file_info=info):
                        created += 1
                    else:
                        failed += 1
            elif await update_nfo_classification(nfo_path, uncensored=uncensored, amateur=amateur):
                updated += 1
            else:
                failed += 1
            await self.report_progress(index, total, nfo_path.name)
        await self.report_progress(total, total, "done")
        return TaskResult(
            True,
            result=RebuildTagsResult(scanned=scanned, created=created, updated=updated, skipped=skipped, failed=failed),
        )
