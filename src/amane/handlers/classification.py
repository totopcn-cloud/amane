"""为已有 NFO 补写无码与素人分类."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..media import update_nfo_classification
from ..parsing import classification_tags, parse_file_info
from ..utils.path import is_descendant
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
        media = [
            item
            for item in await self._repo.list_media_files(library_id=payload.library_id, limit=None)
            if is_descendant(item.path, payload.path)
        ]
        scanned = updated = skipped = failed = 0
        total = len(media)
        await self.report_progress(0, total, "rebuild tags")
        for index, item in enumerate(media, start=1):
            scanned += 1
            nfo_path = Path(item.path).with_suffix(".nfo")
            if not nfo_path.is_file():
                skipped += 1
                await self.report_progress(index, total, nfo_path.name)
                continue
            info = parse_file_info(item.path)
            uncensored, amateur = classification_tags(info)
            metadata = await self._repo.get_metadata(item.metadata_id) if item.metadata_id is not None else None
            if metadata is not None and "素人" in metadata.tags:
                amateur = True
            if await update_nfo_classification(nfo_path, uncensored=uncensored, amateur=amateur):
                updated += 1
            else:
                failed += 1
            await self.report_progress(index, total, nfo_path.name)
        await self.report_progress(total, total, "done")
        return TaskResult(
            True,
            result=RebuildTagsResult(scanned=scanned, updated=updated, skipped=skipped, failed=failed),
        )
