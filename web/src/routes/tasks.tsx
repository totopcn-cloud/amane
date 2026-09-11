import {
  Badge,
  Box,
  Button,
  Code,
  Collapse,
  Group,
  Paper,
  ScrollArea,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
  Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconArchive,
  IconChevronDown,
  IconChevronUp,
  IconListDetails,
  IconPlayerPause,
  IconPlayerPlay,
  IconPlayerStop,
  IconPlus,
  IconRefresh,
  IconTrash,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, stripSearchParams } from "@tanstack/react-router";
import { Liquid } from "liquidjs";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { z } from "zod";
import {
  batchTasksMutation,
  getTaskWorkerOptions,
  getTaskWorkerQueryKey,
  listTasksOptions,
  pauseTaskWorkerMutation,
  resumeTaskWorkerMutation,
} from "@/client/@tanstack/react-query.gen";
import { client } from "@/client/client.gen";
import { submitTask } from "@/client/sdk.gen";
import type { TaskBatchAction, TaskStatus, TaskType } from "@/client/types.gen";
import { ListToolbar } from "@/components/common/list-toolbar";
import { PageSizeSelect } from "@/components/common/page-size-select";
import { APP_SHELL_MAIN_HEIGHT } from "@/components/layout/app-shell-metrics";
import { TaskSubmitModal } from "@/components/task/task-submit-modal";
import { TaskTree } from "@/components/task/task-tree";
import { useResettingState } from "@/hooks/use-resetting-state";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { invalidateTaskQueries } from "@/lib/connection";
import { TASK_STATUSES, TASK_TYPES } from "@/lib/exhaustive-maps";
import { childCountOf } from "@/lib/task/display";
import { useProgressStore } from "@/stores/progress";
import { useUIStore } from "@/stores/ui";

/**
 * LiquidJS 批量刮削模板引擎, 用于将模板渲染为一组番号预览. 语法: https://liquidjs.com/
 * 例: {% for i in (1..50) %}\nMIDV-{{ i | plus: 100 }}\n{% endfor %}
 */
const engine = new Liquid();

const tasksSearchSchema = z.object({
  status: z.array(z.enum(TASK_STATUSES)).optional(),
  type: z.array(z.enum(TASK_TYPES)).optional(),
  page: z.coerce.number().int().min(1).catch(1).default(1),
});

export const Route = createFileRoute("/tasks")({
  validateSearch: tasksSearchSchema,
  search: { middlewares: [stripSearchParams({ page: 1 })] },
  component: TasksPage,
});

const CANCELABLE_STATUSES: TaskStatus[] = ["queued", "running"];
const FAILED_STATUS: TaskStatus[] = ["failed"];
const DONE_STATUS: TaskStatus[] = ["done"];

function intersectStatuses(
  current: TaskStatus[] | undefined,
  allowed: TaskStatus[],
): TaskStatus[] | null {
  if (current == null || current.length === 0) {
    return allowed;
  }
  const next = current.filter((status) => allowed.includes(status));
  return next.length === 0 ? null : next;
}

function filterBatchBody(
  search: { status?: TaskStatus[]; type?: TaskType[] },
  allowed: TaskStatus[],
): { status: TaskStatus[]; type?: TaskType[] } | null {
  const status = intersectStatuses(search.status, allowed);
  if (status == null) {
    return null;
  }
  return search.type != null && search.type.length > 0 ? { status, type: search.type } : { status };
}

function TasksPage() {
  const { t } = useTranslation(["tasks", "common"]);
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const queryClient = useQueryClient();
  const progressByTask = useProgressStore((s) => s.byTask);
  const limit = useUIStore((s) => s.pageSizes.tasks);

  const [submitOpen, setSubmitOpen] = useState(false);
  const statusKey = (search.status ?? []).join(",");
  const typeKey = (search.type ?? []).join(",");
  const listKey = `${search.page}:${statusKey}:${typeKey}`;
  const [opened, setOpened] = useResettingState(() => new Set<number>(), listKey);
  const [focusId, setFocusId] = useResettingState<number | null>(() => null, listKey);
  const [batchOpen, setBatchOpen] = useState(false);
  const [template, setTemplate] = useState("");
  const [preview, setPreview] = useState<string[]>([]);
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const offset = (search.page - 1) * limit;
  const { data, isLoading } = useQuery(
    listTasksOptions({
      query: {
        status: search.status,
        type: search.type,
        offset,
        limit,
        sort_by: "created_at",
        order: "desc",
      },
    }),
  );

  const { data: worker } = useQuery(getTaskWorkerOptions());
  const paused = worker?.paused === true;

  const invalidate = () => {
    invalidateTaskQueries(queryClient);
  };

  const batchMutation = useMutation({
    ...batchTasksMutation(),
    onSuccess: (res, vars) => {
      const action: TaskBatchAction = vars.body.action;
      notifications.show({
        message: t(`batch.${action}`, { count: res.affected ?? 0 }),
        color: "blue",
      });
      invalidate();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });
  const pauseMutation = useMutation({
    ...pauseTaskWorkerMutation(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: getTaskWorkerQueryKey() });
    },
  });
  const resumeMutation = useMutation({
    ...resumeTaskWorkerMutation(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: getTaskWorkerQueryKey() });
    },
  });

  const cancelFilter = filterBatchBody(search, CANCELABLE_STATUSES);
  const retryFilter = filterBatchBody(search, FAILED_STATUS);
  const deleteDoneFilter = filterBatchBody(search, DONE_STATUS);
  const deleteFailedFilter = filterBatchBody(search, FAILED_STATUS);

  async function handleDeleteTask(taskId: number) {
    const ok = await confirm({
      title: t("confirm.deleteTitle"),
      message: t("confirm.deleteMessage"),
      confirmLabel: t("common:actions.delete"),
    });
    if (!ok) return;
    batchMutation.mutate({ body: { action: "delete", task_ids: [taskId] } });
  }

  async function handleFilterBatch(
    action: TaskBatchAction,
    body: { status: TaskStatus[]; type?: TaskType[] },
    title: string,
    message: string,
    confirmLabel: string,
  ) {
    const ok = await confirm({ title, message, confirmLabel });
    if (!ok) return;
    batchMutation.mutate({ body: { action, ...body } });
  }

  async function handleArchiveFailed() {
    const ok = await confirm({
      title: t("confirm.archiveFailedTitle"),
      message: t("confirm.archiveFailedMessage"),
      confirmLabel: t("toolbar.archiveFailed"),
    });
    if (!ok) return;
    try {
      const response = await fetch(`${client.getConfig().baseUrl}/api/tasks/archive-failed`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = (await response.json()) as { archived?: number; skipped?: number; missing?: number };
      notifications.show({
        message: t("batch.archiveFailed", { count: result.archived ?? 0 }),
        color: "blue",
      });
      invalidate();
    } catch (error) {
      notifications.show({
        message: extractErrorMessage(error, t("common:toast.operationFailed")),
        color: "red",
      });
    }
  }

  function toggleStatus(status: TaskStatus) {
    const current = search.status ?? [];
    const next = current.includes(status)
      ? current.filter((s) => s !== status)
      : [...current, status];
    void navigate({
      search: (prev) => ({ ...prev, status: next.length ? next : undefined, page: 1 }),
    });
  }

  function toggleType(type: TaskType) {
    const current = search.type ?? [];
    const next = current.includes(type) ? current.filter((tt) => tt !== type) : [...current, type];
    void navigate({
      search: (prev) => ({ ...prev, type: next.length ? next : undefined, page: 1 }),
    });
  }

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      if (!template.trim()) {
        setPreview([]);
        return;
      }
      engine
        .parseAndRender(template)
        .then((result: string) => {
          const lines = result
            .split("\n")
            .map((l: string) => l.trim())
            .filter((l: string) => l.length > 0);
          setPreview(lines);
        })
        .catch(() => setPreview([]));
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [template]);

  async function handleBatchSubmit() {
    setBatchSubmitting(true);
    const results = await Promise.allSettled(
      preview.map((number) => submitTask({ body: { type: "scrape", number }, throwOnError: true })),
    );
    setBatchSubmitting(false);
    const failed = results.filter((r) => r.status === "rejected").length;
    const ok = preview.length - failed;
    if (ok > 0) {
      notifications.show({
        message: t("common:toast.batchScrapeStarted", { count: ok }),
        color: "blue",
      });
    }
    if (failed > 0) {
      notifications.show({
        message: t("common:toast.batchScrapeFailed", { count: failed }),
        color: "red",
      });
    }
    setTemplate("");
    setPreview([]);
    setBatchOpen(false);
    invalidate();
  }

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const expandableIds = items.filter((task) => childCountOf(task) > 0).map((task) => task.id);
  const canExpand = expandableIds.some((id) => !opened.has(id));

  return (
    <Stack gap="md" style={{ height: APP_SHELL_MAIN_HEIGHT, overflow: "hidden" }}>
      <Group justify="space-between" wrap="wrap" style={{ flexShrink: 0 }}>
        <Title order={2}>{t("title")}</Title>
        <Group gap="sm">
          <Text size="sm" c="dimmed">
            {t("total", { count: total })}
          </Text>
          <Button
            size="sm"
            variant={paused ? "filled" : "light"}
            leftSection={paused ? <IconPlayerPlay size={14} /> : <IconPlayerPause size={14} />}
            loading={pauseMutation.isPending || resumeMutation.isPending}
            onClick={() => (paused ? resumeMutation.mutate({}) : pauseMutation.mutate({}))}
          >
            {paused ? t("worker.resume") : t("worker.pause")}
          </Button>
          <Button
            size="sm"
            variant={batchOpen ? "filled" : "light"}
            leftSection={<IconListDetails size={14} />}
            onClick={() => setBatchOpen((v) => !v)}
          >
            {t("batchScrape.toggle")}
          </Button>
          <Button
            size="sm"
            leftSection={<IconPlus size={14} />}
            onClick={() => setSubmitOpen(true)}
          >
            {t("actions.submit")}
          </Button>
        </Group>
      </Group>

      <Group gap="xs" wrap="wrap" style={{ flexShrink: 0 }}>
        {TASK_STATUSES.map((status) => (
          <Button
            key={status}
            size="xs"
            variant={search.status?.includes(status) ? "filled" : "light"}
            onClick={() => toggleStatus(status)}
          >
            {t(`status.${status}`)}
          </Button>
        ))}
      </Group>
      <Group align="flex-end" justify="space-between" wrap="wrap" style={{ flexShrink: 0 }}>
        <Group gap="xs" wrap="wrap">
          {TASK_TYPES.map((type) => (
            <Button
              key={type}
              size="xs"
              variant={search.type?.includes(type) ? "filled" : "light"}
              color="grape"
              onClick={() => toggleType(type)}
            >
              {t(`filters.${type}`)}
            </Button>
          ))}
        </Group>
        <PageSizeSelect
          sizeKey="tasks"
          onChanged={() => void navigate({ search: (prev) => ({ ...prev, page: 1 }) })}
        />
      </Group>

      <Box style={{ flexShrink: 0 }}>
        <Collapse expanded={batchOpen}>
          <Paper withBorder radius="md" p="md">
            <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
              <Stack gap="xs">
                <Text size="sm" fw={600}>
                  {t("batchScrape.templateLabel")}
                </Text>
                <Textarea
                  value={template}
                  onChange={(e) => setTemplate(e.currentTarget.value)}
                  placeholder={"{% for i in (1..50) %}\nMIDV-{{ i | plus: 100 }}\n{% endfor %}"}
                  autosize
                  minRows={8}
                  maxRows={8}
                  ff="monospace"
                />
              </Stack>
              <Stack gap="xs">
                <Group gap="xs">
                  <Text size="sm" fw={600}>
                    {t("batchScrape.previewLabel")}
                  </Text>
                  {preview.length > 0 && (
                    <Badge size="sm" variant="light">
                      {preview.length}
                    </Badge>
                  )}
                </Group>
                <ScrollArea.Autosize
                  mah={220}
                  type="auto"
                  style={{
                    border: "1px solid var(--mantine-color-default-border)",
                    borderRadius: "var(--mantine-radius-sm)",
                  }}
                >
                  {preview.length > 0 ? (
                    <Code block style={{ border: "none" }}>
                      {preview.join("\n")}
                    </Code>
                  ) : (
                    <Text size="sm" c="dimmed" p="sm">
                      {t("batchScrape.previewEmpty")}
                    </Text>
                  )}
                </ScrollArea.Autosize>
              </Stack>
            </SimpleGrid>
            <Group justify="flex-end" mt="md">
              <Button
                onClick={handleBatchSubmit}
                loading={batchSubmitting}
                disabled={preview.length === 0}
              >
                {t("batchScrape.submit", { count: preview.length })}
              </Button>
            </Group>
          </Paper>
        </Collapse>
      </Box>

      <ListToolbar
        totalPages={totalPages}
        page={search.page}
        onChange={(page) => void navigate({ search: (prev) => ({ ...prev, page }) })}
        trailing={
          <Group gap="xs">
            <Button
              size="xs"
              variant="light"
              leftSection={<IconChevronDown size={14} />}
              disabled={!canExpand}
              onClick={() => {
                setFocusId(null);
                setOpened(new Set(expandableIds));
              }}
            >
              {t("toolbar.expandAll")}
            </Button>
            <Button
              size="xs"
              variant="light"
              leftSection={<IconChevronUp size={14} />}
              disabled={opened.size === 0}
              onClick={() => {
                setFocusId(null);
                setOpened(new Set());
              }}
            >
              {t("toolbar.collapseAll")}
            </Button>
          </Group>
        }
        header={
          cancelFilter != null ||
          retryFilter != null ||
          deleteDoneFilter != null ||
          deleteFailedFilter != null ? (
            <Group gap="xs">
              {cancelFilter != null && (
                <Button
                  size="xs"
                  variant="light"
                  color="orange"
                  leftSection={<IconPlayerStop size={14} />}
                  loading={batchMutation.isPending}
                  onClick={() =>
                    void handleFilterBatch(
                      "cancel",
                      cancelFilter,
                      t("confirm.cancelTitle"),
                      t("confirm.cancelFilterMessage"),
                      t("actions.cancelTask"),
                    )
                  }
                >
                  {t("toolbar.cancel")}
                </Button>
              )}
              {retryFilter != null && (
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<IconRefresh size={14} />}
                  loading={batchMutation.isPending}
                  onClick={() =>
                    void handleFilterBatch(
                      "retry",
                      retryFilter,
                      t("confirm.retryFilterTitle"),
                      t("confirm.retryFilterMessage"),
                      t("common:actions.retry"),
                    )
                  }
                >
                  {t("toolbar.retryFailed")}
                </Button>
              )}
              {deleteDoneFilter != null && (
                <Button
                  size="xs"
                  variant="light"
                  color="red"
                  leftSection={<IconTrash size={14} />}
                  loading={batchMutation.isPending}
                  onClick={() =>
                    void handleFilterBatch(
                      "delete",
                      deleteDoneFilter,
                      t("confirm.clearDoneTitle"),
                      t("confirm.clearDoneMessage"),
                      t("common:actions.delete"),
                    )
                  }
                >
                  {t("toolbar.clearDone")}
                </Button>
              )}
              {deleteFailedFilter != null && (
                <>
                  <Button
                    size="xs"
                    variant="light"
                    color="red"
                    leftSection={<IconTrash size={14} />}
                    loading={batchMutation.isPending}
                    onClick={() =>
                      void handleFilterBatch(
                        "delete",
                        deleteFailedFilter,
                        t("confirm.clearFailedTitle"),
                        t("confirm.clearFailedMessage"),
                        t("common:actions.delete"),
                      )
                    }
                  >
                    {t("toolbar.clearFailed")}
                  </Button>
                  <Button
                    size="xs"
                    variant="light"
                    leftSection={<IconArchive size={14} />}
                    loading={batchMutation.isPending}
                    onClick={() => void handleArchiveFailed()}
                  >
                    {t("toolbar.archiveFailed")}
                  </Button>
                </>
              )}
            </Group>
          ) : undefined
        }
      >
        <TaskTree
          tasks={items}
          progressByTask={progressByTask}
          opened={opened}
          focusId={focusId}
          onToggle={(id) => {
            const closing = opened.has(id);
            setOpened((prev) => {
              const next = new Set(prev);
              if (closing) next.delete(id);
              else next.add(id);
              return next;
            });
            setFocusId(closing ? null : id);
          }}
          actions={{
            onCancel: (taskId) =>
              batchMutation.mutate({ body: { action: "cancel", task_ids: [taskId] } }),
            onRetry: (taskId) =>
              batchMutation.mutate({ body: { action: "retry", task_ids: [taskId] } }),
            onDelete: (taskId) => void handleDeleteTask(taskId),
            pending: batchMutation.isPending,
          }}
        />

        {!isLoading && items.length === 0 && (
          <Text c="dimmed" size="sm" ta="center" py="xl">
            {t("empty")}
          </Text>
        )}
      </ListToolbar>

      <TaskSubmitModal opened={submitOpen} onClose={() => setSubmitOpen(false)} />
    </Stack>
  );
}
