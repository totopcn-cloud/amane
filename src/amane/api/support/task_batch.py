"""按 ID 或与列表同形的 status/type 筛选."""

import contextlib
import shutil
from collections.abc import Sequence
from pathlib import Path

from ...db.models import Library, MediaFile, MediaFileStatus, Task, TaskStatus, TaskType
from ...db.repository import Repository
from ...observability import remove_task_dir
from ...library import FAILED_ARCHIVE_DIRNAME, FAILED_DIRNAME, LEGACY_FAILED_ARCHIVE_DIRNAME, LibraryFileKind, LibraryScan
from ...parsing import parse_file_info
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


@in_thread
def _find_legacy_failed_sources(
    root: Path,
    numbers: frozenset[str],
    *,
    patterns: list[str] | None,
    trailer_pattern: str | None,
    blacklist_patterns: Sequence[str],
    min_file_size: int,
) -> dict[str, list[Path]]:
    """从旧任务的番号找回仍在磁盘上的源文件。

    旧版本任务只保存 media_file_id。扫描清理了这条数据库记录后，任务仍在而路径已丢；
    此处按该媒体库自己的匹配规则单次遍历（因此也支持用户配置的 .m2ts）。
    """
    found: dict[str, list[Path]] = {}
    scan = LibraryScan(
        patterns=patterns,
        trailer_pattern=trailer_pattern,
        blacklist_patterns=blacklist_patterns,
        min_file_size=min_file_size,
    )
    try:
        paths = root.rglob("*")
        for path in paths:
            if not path.is_file():
                continue
            try:
                if scan.classify(path) is not LibraryFileKind.MEDIA:
                    continue
                number = parse_file_info(path).number
            except (OSError, ValueError):
                continue
            if number is None or number.casefold() not in numbers:
                continue
            found.setdefault(number.casefold(), []).append(path)
    except OSError:
        return found
    return found


async def _library_for_task(repo: Repository, task: Task) -> Library | None:
    """从失败 SCRAPE 的链根 Refresh 任务找到所属媒体库。"""
    root_id = task.root_task_id
    if root_id is None:
        return None
    root_task = await repo.get_task(root_id)
    if root_task is None:
        return None
    library_id = (root_task.payload or {}).get("library_id")
    if not isinstance(library_id, int) or isinstance(library_id, bool):
        return None
    return await repo.get_library(library_id)


async def archive_failed_scrape_files(repo: Repository) -> ArchiveFailedResponse:
    """Archive failed media, accepting both failed status and failed scrape tasks."""
    failed_media_by_id = {
        media.id: media
        for media in await repo.list_media_files(status=[MediaFileStatus.FAILED], limit=None)
        if media.id is not None
    }
    failed_tasks = await repo.find_tasks(statuses=[TaskStatus.FAILED], task_types=[TaskType.SCRAPE])
    legacy_numbers_by_library: dict[int, set[str]] = {}
    legacy_libraries: dict[int, Library] = {}
    explicit_legacy_sources: dict[tuple[int, Path], tuple[Path, Path]] = {}
    for task in failed_tasks:
        media_id = (task.payload or {}).get("media_file_id")
        if not isinstance(media_id, int) or isinstance(media_id, bool) or media_id in failed_media_by_id:
            continue
        media = await repo.get_media_file(media_id)
        # A later successful retry takes precedence over a historical failed task.
        if media is not None and media.status != MediaFileStatus.SCRAPED:
            failed_media_by_id[media_id] = media
            continue
        # 旧任务里的 MediaFile 已被后续“清理失效记录”删除。优先使用新版本
        # 保存下来的路径；若还是旧任务，则稍后按番号在同一媒体库内找回。
        library = await _library_for_task(repo, task)
        if library is None or library.id is None:
            continue
        source_path = (task.payload or {}).get("source_path")
        source = Path(source_path) if isinstance(source_path, str) and source_path else None
        root = Path(library.path)
        if source is not None:
            try:
                source.relative_to(root)
            except ValueError:
                continue
            explicit_legacy_sources[(library.id, source)] = (root, source)
            continue
        number = (task.payload or {}).get("number")
        if isinstance(number, str) and number.strip():
            legacy_libraries[library.id] = library
            legacy_numbers_by_library.setdefault(library.id, set()).add(number.casefold())
    failed_media_ids = frozenset(failed_media_by_id)
    folders: dict[tuple[int, Path], tuple[Path, list[tuple[int, Path]]]] = {}
    root_files: dict[tuple[int, Path], tuple[Path, int]] = {}
    archived = skipped = missing = 0
    for media in failed_media_by_id.values():
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

    # 对旧任务找回的文件不能安全判断整个目录是否“全失败”，所以始终只移动该源文件。
    # 这样不会把已成功作品或其它内容错误地带走。
    legacy_sources = set(explicit_legacy_sources.values())
    for library_id, numbers in legacy_numbers_by_library.items():
        library = legacy_libraries[library_id]
        root = Path(library.path)
        found = await _find_legacy_failed_sources(
            root,
            frozenset(numbers),
            patterns=library.patterns,
            trailer_pattern=library.trailer_pattern,
            blacklist_patterns=library.blacklist_patterns or [],
            min_file_size=library.min_file_size,
        )
        for paths in found.values():
            for source in paths:
                legacy_sources.add((root, source))

    for root, source in sorted(legacy_sources, key=lambda item: str(item[1])):
        try:
            relative_parent = source.parent.relative_to(root)
        except ValueError:
            skipped += 1
            continue
        if relative_parent.parts and relative_parent.parts[0] in {
            FAILED_DIRNAME,
            FAILED_ARCHIVE_DIRNAME,
            LEGACY_FAILED_ARCHIVE_DIRNAME,
        }:
            skipped += 1
            continue
        destination, error = await _move_failed_file(source, root / FAILED_ARCHIVE_DIRNAME / relative_parent)
        if destination is None:
            missing += 1 if error and "not found" in error.lower() else 0
            skipped += 0 if error and "not found" in error.lower() else 1
            continue
        archived += 1

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
        # 同目录存在未失败的视频时，不得移动整目录；只移失败源文件.
        if any(item.id not in failed_media_ids for item in contained):
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
