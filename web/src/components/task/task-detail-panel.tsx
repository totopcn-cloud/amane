import {
  ActionIcon,
  Badge,
  Button,
  Code,
  Collapse,
  Group,
  Progress,
  ScrollArea,
  Stack,
  Text,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import {
  IconChevronDown,
  IconChevronRight,
  IconCopy,
  IconDownload,
  IconFolderSearch,
  IconPlayerStop,
  IconRefresh,
  IconTrash,
} from "@tabler/icons-react";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { client } from "@/client/client.gen";
import { getMedia } from "@/client/sdk.gen";
import type { TaskResponse } from "@/client/types.gen";
import { TaskLogView } from "@/components/log/task-log-view";
import { TaskReportPanel } from "@/components/task/task-report-panel";
import { formatDuration, statusColor } from "@/lib/task/display";
import { useProgressStore } from "@/stores/progress";

export interface TaskNodeActions {
  onCancel: (taskId: number) => void;
  onRetry: (taskId: number) => void;
  onDelete: (taskId: number) => void;
  pending: boolean;
}

interface TaskDetailPanelProps {
  task: TaskResponse;
  linkKey: string | null;
  actions: TaskNodeActions;
}

function CollapsibleJson({ title, value }: { title: string; value: unknown }) {
  const [opened, setOpened] = useState(false);

  return (
    <div>
      <UnstyledButton
        onClick={() => setOpened((v) => !v)}
        style={{ display: "block", width: "100%" }}
      >
        <Group gap={6} wrap="nowrap">
          {opened ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          <Text size="sm" c="dimmed" fw={500}>
            {title}
          </Text>
        </Group>
      </UnstyledButton>
      <Collapse expanded={opened}>
        <ScrollArea.Autosize mah={220} type="auto" mt={6}>
          <Code block>{JSON.stringify(value, null, 2)}</Code>
        </ScrollArea.Autosize>
      </Collapse>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text size="sm" mt={2} ff="monospace">
        {children}
      </Text>
    </div>
  );
}

/** JSON 默认折叠. */
export function TaskDetailPanel({ task, linkKey, actions }: TaskDetailPanelProps) {
  const { t } = useTranslation(["tasks", "common"]);
  const progress = useProgressStore((s) => s.byTask[task.id]);
  const duration = formatDuration(task.started_at, task.finished_at);
  const isTerminal = task.status === "done" || task.status === "failed";
  const hasPayload = task.payload != null && Object.keys(task.payload).length > 0;
  const hasResult = task.result != null && Object.keys(task.result).length > 0;

  return (
    <Stack gap="sm" p="sm">
      <Group gap="lg" wrap="wrap">
        <Fact label={t("detail.id")}>#{task.id}</Fact>
        {task.root_task_id != null && task.root_task_id !== task.id ? (
          <Fact label={t("detail.root")}>#{task.root_task_id}</Fact>
        ) : null}
        {linkKey != null ? <Fact label={t("detail.linkKey")}>{linkKey}</Fact> : null}
        <Fact label={t("detail.priority")}>{task.priority ?? 0}</Fact>
        <Fact label={t("detail.retries")}>{task.retries ?? 0}</Fact>
        <Fact label={t("detail.created")}>
          {task.created_at ? new Date(task.created_at).toLocaleString() : "-"}
        </Fact>
        <Fact label={t("detail.started")}>
          {task.started_at ? new Date(task.started_at).toLocaleString() : "-"}
        </Fact>
        <Fact label={t("detail.duration")}>{duration ?? "-"}</Fact>
        <div>
          <Text size="xs" c="dimmed">
            {t("detail.status")}
          </Text>
          <Badge size="sm" variant="light" color={statusColor(task.status)} mt={2}>
            {t(`status.${task.status}`)}
          </Badge>
        </div>
      </Group>

      {task.status === "running" && (
        <div>
          <Text size="sm" fw={600} mb={4}>
            {t("detail.progress")}
          </Text>
          <Progress
            size="sm"
            value={progress && progress.total > 0 ? (progress.current / progress.total) * 100 : 100}
            animated={!progress || progress.total === 0}
          />
          {progress && (progress.total > 0 || progress.message) && (
            <Text size="xs" c="dimmed" mt={4}>
              {progress.total > 0 ? `${progress.current} / ${progress.total}` : ""}
              {progress.total > 0 && progress.message ? " · " : ""}
              {progress.message}
            </Text>
          )}
        </div>
      )}

      {isTerminal && (
        <TaskReportPanel taskId={task.id} failed={task.status === "failed"} taskType={task.type} />
      )}

      {hasPayload && <CollapsibleJson title={t("detail.payload")} value={task.payload} />}

      {hasResult && task.status === "done" && (
        <CollapsibleJson title={t("detail.result")} value={task.result} />
      )}

      {(task.status === "queued" || task.status === "running") && (
        <div>
          <Text size="sm" fw={600} mb={4}>
            {t("detail.liveLogs")}
          </Text>
          <TaskLogView taskId={task.id} />
        </div>
      )}

      <Group gap="xs">
        {(task.status === "queued" || task.status === "running") && (
          <Button
            size="xs"
            variant="light"
            color="orange"
            loading={actions.pending}
            onClick={() => actions.onCancel(task.id)}
          >
            {t("actions.cancelTask")}
          </Button>
        )}
        {task.status === "failed" && (
          <Button
            size="xs"
            variant="light"
            loading={actions.pending}
            onClick={() => actions.onRetry(task.id)}
          >
            {t("common:actions.retry")}
          </Button>
        )}
        {isTerminal && (
          <Button
            size="xs"
            variant="light"
            leftSection={<IconDownload size={14} />}
            onClick={() =>
              window.open(`${client.getConfig().baseUrl}/api/tasks/${task.id}/record`, "_blank")
            }
          >
            {t("actions.record")}
          </Button>
        )}
        {isTerminal && (
          <Button
            size="xs"
            variant="light"
            color="red"
            loading={actions.pending}
            onClick={() => void actions.onDelete(task.id)}
          >
            {t("common:actions.delete")}
          </Button>
        )}
      </Group>
    </Stack>
  );
}

/** 行上的取消 / 重试 / 删除; 点击不触发行展开. */
export function TaskRowActions({
  task,
  actions,
}: {
  task: TaskResponse;
  actions: TaskNodeActions;
}) {
  const { t } = useTranslation(["tasks", "common"]);
  const isTerminal = task.status === "done" || task.status === "failed";
  const mediaFileId =
    task.type === "scrape" && typeof task.payload?.media_file_id === "number"
      ? task.payload?.media_file_id ?? null
      : null;

  async function copySourcePath() {
    if (mediaFileId == null) return;
    const { data } = await getMedia({ path: { media_id: mediaFileId }, throwOnError: true });
    await navigator.clipboard.writeText(data.path);
  }

  async function revealSourceFile() {
    if (mediaFileId == null) return;
    await fetch(`${client.getConfig().baseUrl}/api/media/${mediaFileId}/reveal`, { method: "POST" });
  }

  return (
    <Group
      gap={4}
      wrap="nowrap"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      {(task.status === "queued" || task.status === "running") && (
        <Tooltip label={t("actions.cancelTask")}>
          <ActionIcon
            variant="subtle"
            color="orange"
            loading={actions.pending}
            onClick={() => actions.onCancel(task.id)}
          >
            <IconPlayerStop size={16} />
          </ActionIcon>
        </Tooltip>
      )}
      {task.status === "failed" && (
        <Tooltip label={t("common:actions.retry")}>
          <ActionIcon
            variant="subtle"
            loading={actions.pending}
            onClick={() => actions.onRetry(task.id)}
          >
            <IconRefresh size={16} />
          </ActionIcon>
        </Tooltip>
      )}
      {mediaFileId != null && (
        <>
          <Tooltip label={t("actions.copySourcePath")}>
            <ActionIcon variant="subtle" onClick={() => void copySourcePath()}>
              <IconCopy size={16} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label={t("actions.revealSourceFile")}>
            <ActionIcon variant="subtle" onClick={() => void revealSourceFile()}>
              <IconFolderSearch size={16} />
            </ActionIcon>
          </Tooltip>
        </>
      )}
      {isTerminal && (
        <Tooltip label={t("common:actions.delete")}>
          <ActionIcon
            variant="subtle"
            color="red"
            loading={actions.pending}
            onClick={() => void actions.onDelete(task.id)}
          >
            <IconTrash size={16} />
          </ActionIcon>
        </Tooltip>
      )}
    </Group>
  );
}
