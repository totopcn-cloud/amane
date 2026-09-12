"""按 ID 或与列表同形的 status/type 筛选."""

import contextlib
import shutil
from collections.abc import Sequence
from pathlib import Path

from ...db.models import MediaFile, MediaFileStatus, Task, TaskStatus, TaskType
from ...db.repository import Repository
from ...observability import remove_task_dir
from ...library import FAILED_ARCHIVE_DIRNAME, FAILED_DIRNAME, LEGACY_FAILED_ARCHIVE_DIRNAME
from ...scheduler.worker import AsyncWorker
from ...utils.threads import in_thread
from ..models.tasks import ArchiveFailedResponse, TaskBatchAction, TaskBatchResponse

CANCEL_ERROR = "Cancelled by user"

_CANCELABLE = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})
_DELETABLE = frozenset({TaskStatus.DONE, TaskStatus.FAILED})
_RETRYABLE = frozenset({TaskStatus.FAILED})

_ACTION_STATUSES: dict[TaskBatchAction, frozenset[TaskStatus]] = {
    TaskBatchAction.CANCEL: _CANCELABLE,
    TaskBatchAction.DELETE: _DELETABLE,
    TaskBatchAction.RETRY: _RETRYABLE,
}


@in_thread
def _move_failed_folder(source: Path, target_parent: Path) -> tuple[Path | None, str | None]:
    """Move a complete source directory, suffixing the directory name on collision."""
    if not source.is_dir():
        return None, f"Source directory not found: {source}"
    try:
        target_parent.mkdir(parents=True, exist_ok=True)
        destination = target_parent / source.name
        index = 1
        while destination.exists():
            destination = target_parent / f"{source.name}({index})"
            index += 1
        shutil.move(str(source), str(destination))
        return destination, None
    except OSError as exc:
        return None, str(exc)


@in_thread
def _move_failed_file(source: Path, target_parent: Path) -> tuple[Path | None, str | None]:
    """Move a root-level source file while retaining its extension on collision."""
    if not source.is_file():
        return None, f"Source file not found: {source}"
    try:
        target_parent.mkdir(parents=True, exist_ok=True)
        destination = target_parent / source.name
        index = 1
        while destination.exists():
            destination = target_parent / f"{source.stem}({index}){source.suffix}"
            index += 1
        shutil.move(str(source), str(destination))
        return destination, None
    except OSError as exc:
        return None, str(exc)


async def archive_failed_scrape_files(repo: Repository) -> ArchiveFailedResponse:
    """Archive failed media, even when its historical task records were cleared."""
    failed_media = await repo.list_media_files(status=[MediaFileStatus.FAILED], limit=None)
    folders: dict[tuple[int, Path], tuple[Path, list[tuple[int, Path]]]] = {}
    root_files: dict[tuple[int, Path], tuple[Path, int]] = {}
    archived = skipped = missing = 0
    for media in failed_media:
        if media.id is None:
            skipped += 1
            continue
        library = await repo.get_library(media.library_id)
        if library is None:
            skipped += 1
            continue

        source = Path(media.path)
        root = Path(library.path)
        folder = source.parent
        try:
            relative_folder = folder.relative_to(root)
        except ValueError:
            skipped += 1
            continue
        # Never move the media library root itself. A root-level video is moved by itself.
        if not relative_folder.parts:
            root_files[(media.library_id, source)] = (root, media.id)
            continue
        if relative_folder.parts and relative_folder.parts[0] in {
            FAILED_DIRNAME,
            FAILED_ARCHIVE_DIRNAME,
            LEGACY_FAILED_ARCHIVE_DIRNAME,
        }:
            skipped += 1
            continue
        key = (media.library_id, folder)
        if key not in folders:
            folders[key] = (root, [])
        folders[key][1].append((media.id, source))

    for (_, source), (root, media_id) in root_files.items():
        destination, error = await _move_failed_file(source, root / FAILED_ARCHIVE_DIRNAME)
        if destination is None:
            missing += 1 if error and "not found" in error.lower() else 0
            skipped += 0 if error and "not found" in error.lower() else 1
            continue
        await repo.update_media_file(media_id, path=str(destination))
        archived += 1

    media_by_library: dict[int, Sequence[MediaFile]] = {}
    moved_folders: list[Path] = []
    for (library_id, folder), (root, failed_sources) in sorted(
        folders.items(), key=lambda item: len(item[0][1].parts)
    ):
        if any(folder.is_relative_to(moved_folder) for moved_folder in moved_folders):
            continue
        relative_folder = folder.relative_to(root)
        if library_id not in media_by_library:
            media_by_library[library_id] = await repo.list_media_files(library_id=library_id, limit=None)
        contained = [
            item
            for item in media_by_library[library_id]
            if Path(item.path).is_relative_to(folder)
        ]
        # 同目录存在已成功、待处理或跳过的视频时，不得移动整目录；只移失败源文件.
        if any(item.status != MediaFileStatus.FAILED for item in contained):
            for media_id, source in failed_sources:
                destination, error = await _move_failed_file(
                    source,
                    root / FAILED_ARCHIVE_DIRNAME / relative_folder,
                )
                if destination is None:
                    missing += 1 if error and "not found" in error.lower() else 0
                    skipped += 0 if error and "not found" in error.lower() else 1
                    continue
                await repo.update_media_file(media_id, path=str(destination))
                archived += 1
            continue
        destination, error = await _move_failed_folder(
            folder,
            root / FAILED_ARCHIVE_DIRNAME / relative_folder.parent,
        )
        if destination is None:
            missing += 1 if error and "not found" in error.lower() else 0
            skipped += 0 if error and "not found" in error.lower() else 1
            continue
        moved_folders.append(folder)
        for contained in media_by_library[library_id]:
            try:
                relative_path = Path(contained.path).relative_to(folder)
            except ValueError:
                continue
            if contained.id is not None:
                await repo.update_media_file(contained.id, path=str(destination / relative_path))
        archived += 1
    return ArchiveFailedResponse(archived=archived, skipped=skipped, missing=missing)


def _intersect_statuses(requested: Sequence[TaskStatus] | None, allowed: frozenset[TaskStatus]) -> list[TaskStatus]:
    if requested is None:
        return list(allowed)
    return [status for status in requested if status in allowed]


def cleanup_task_artifacts(task: Task, log_dir: Path) -> None:
    if task.id is not None:
        remove_task_dir(log_dir, task.id)
    if task.log_file:
        log_path = log_dir / task.log_file
        if log_path.is_file():
            with contextlib.suppress(OSError):
                log_path.unlink(missing_ok=True)


async def _cancel_running(worker: AsyncWorker, repo: Repository, tasks: Sequence[Task]) -> int:
    """取消失败的记录为 failed (CANCEL_ERROR)."""
    affected = 0
    for task in tasks:
        if task.id is None:
            continue
        cancelled = await worker.cancel_task(task.id)
        if not cancelled:
            await repo.fail_task(task.id, error=CANCEL_ERROR)
        affected += 1
    return affected


async def execute_task_batch(
    *,
    action: TaskBatchAction,
    repo: Repository,
    worker: AsyncWorker,
    log_dir: Path,
    task_ids: Sequence[int] | None,
    statuses: Sequence[TaskStatus] | None,
    task_types: Sequence[TaskType] | None,
) -> TaskBatchResponse:
    """有 ``task_ids`` 则精确匹配, 否则按状态/类型筛选; CANCEL 区分 queued/running."""
    allowed = _ACTION_STATUSES[action]
    if task_ids is not None:
        unique_ids = list(dict.fromkeys(task_ids))
        found = await repo.find_tasks(task_ids=unique_ids)
        missing = len(unique_ids) - len(found)
        return await _apply_found(
            action=action,
            repo=repo,
            worker=worker,
            log_dir=log_dir,
            found=found,
            missing=missing,
        )

    effective = _intersect_statuses(statuses, allowed)
    if not effective:
        return TaskBatchResponse()

    # CANCEL 区分 queued / running
    if action == TaskBatchAction.CANCEL:
        queued_n = 0
        if TaskStatus.QUEUED in effective:
            queued_n = await repo.fail_queued_tasks(error=CANCEL_ERROR, task_types=task_types)
        running: list[Task] = []
        if TaskStatus.RUNNING in effective:
            running = await repo.find_tasks(statuses=[TaskStatus.RUNNING], task_types=task_types)
        running_n = await _cancel_running(worker, repo, running) if running else 0
        return TaskBatchResponse(affected=queued_n + running_n)

    found = await repo.find_tasks(statuses=effective, task_types=task_types)
    return await _apply_found(
        action=action,
        repo=repo,
        worker=worker,
        log_dir=log_dir,
        found=found,
        missing=0,
    )


async def _apply_found(
    *,
    action: TaskBatchAction,
    repo: Repository,
    worker: AsyncWorker,
    log_dir: Path,
    found: Sequence[Task],
    missing: int,
) -> TaskBatchResponse:
    allowed = _ACTION_STATUSES[action]
    eligible = [task for task in found if task.status in allowed]
    skipped = len(found) - len(eligible)

    if action == TaskBatchAction.CANCEL:
        queued_ids = [task.id for task in eligible if task.status == TaskStatus.QUEUED and task.id is not None]
        running = [task for task in eligible if task.status == TaskStatus.RUNNING]
        affected = 0
        if queued_ids:
            affected += await repo.fail_queued_tasks(error=CANCEL_ERROR, task_ids=queued_ids)
        if running:
            affected += await _cancel_running(worker, repo, running)
        return TaskBatchResponse(affected=affected, skipped=skipped, missing=missing)

    if action == TaskBatchAction.DELETE:
        ids = [task.id for task in eligible if task.id is not None]
        deleted = await repo.delete_tasks(ids)
        leftover_ids: set[int] = set()
        if deleted and deleted < len(ids):
            leftover_ids = {task.id for task in await repo.find_tasks(task_ids=ids) if task.id is not None}
        if deleted:
            for task in eligible:
                if task.id is not None and task.id not in leftover_ids:
                    cleanup_task_artifacts(task, log_dir)
        # 有集合外后裔的行被跳过, 计入 skipped (与 status 不合格的 skipped 相加).
        return TaskBatchResponse(affected=deleted, skipped=skipped + (len(eligible) - deleted), missing=missing)

    created = await repo.retry_tasks(eligible)
    new_ids = [task.id for task in created if task.id is not None]
    return TaskBatchResponse(
        affected=len(new_ids),
        skipped=skipped,
        missing=missing,
        submitted=len(new_ids),
        task_ids=new_ids,
    )
