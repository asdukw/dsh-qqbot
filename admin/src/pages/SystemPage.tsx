import { Clock, Database, Server, Settings2 } from "lucide-react";

import {
  ErrorBanner,
  InfoRow,
  SectionCard,
  StatCard,
} from "@/components/common/Bits";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { SystemData } from "@/lib/analytics";
import { formatBytes, formatDuration, formatTs } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

const DB_LABELS: Record<string, string> = {
  memory: "记忆 / 会话",
  profile: "用户画像",
  models: "模型注册表",
  cards: "角色卡",
  tasks: "定时任务",
  feeds: "订阅",
  stickers: "表情库",
  usage: "Token 用量",
};

export default function SystemPage({ refreshKey }: { refreshKey: number }) {
  const { data, loading, error } = useApi<SystemData>("/api/system", refreshKey);

  const totalSize = (data?.databases ?? []).reduce(
    (sum, database) => sum + (database.size ?? 0),
    0
  );

  return (
    <div className={cn("space-y-6", loading && data && "opacity-70 transition-opacity")}>
      {error ? <ErrorBanner message={error} /> : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={Clock}
          label="面板运行时长"
          value={data ? formatDuration(data.uptime_seconds) : "-"}
          hint={data ? `启动于 ${formatTs(data.started_at)}` : undefined}
        />
        <StatCard
          icon={Database}
          label="数据库总大小"
          value={data ? formatBytes(totalSize) : "-"}
          hint={`${data?.databases.length ?? 0} 个 SQLite 文件`}
        />
        <StatCard
          icon={Server}
          label="监听地址"
          value={data ? `${data.admin.host}:${data.admin.port}` : "-"}
          hint="只绑定本机，通过端口转发访问"
        />
        <StatCard
          icon={Settings2}
          label="DSH profile"
          value={data?.dsh.profile ?? "-"}
          hint={data?.dsh.image_mode ? `image_mode=${data.dsh.image_mode}` : undefined}
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <SectionCard icon={Server} title="管理面板" description="模型页可写，其余只读；密码来自 bot.env">
          <div className="divide-y divide-blue-50">
            <InfoRow label="监听" value={data ? `${data.admin.host}:${data.admin.port}` : "-"} mono />
            <InfoRow
              label="Session 有效期"
              value={data ? `${data.admin.session_hours} 小时` : "-"}
            />
            <InfoRow
              label="来源白名单"
              value={
                data?.admin.allow_cidrs.length ? (
                  <span className="font-mono text-xs">{data.admin.allow_cidrs.join(", ")}</span>
                ) : (
                  <Badge variant="muted">未限制</Badge>
                )
              }
            />
            <InfoRow label="静态文件" value={data?.static_dir || "-"} mono />
            <InfoRow label="Python" value={data?.python ?? "-"} />
          </div>
        </SectionCard>

        <SectionCard icon={Settings2} title="DSH 与数据保留" description="运行时路径与清理策略">
          <div className="divide-y divide-blue-50">
            <InfoRow label="profile" value={data?.dsh.profile ?? "-"} />
            <InfoRow label="home" value={data?.dsh.home ?? "-"} mono />
            <InfoRow label="workspace" value={data?.dsh.workspace ?? "-"} mono />
            <InfoRow label="图片模式" value={data?.dsh.image_mode || "-"} />
            <InfoRow
              label="用量保留"
              value={data ? `${data.usage.retention_days} 天` : "-"}
            />
            <InfoRow
              label="事件保留"
              value={data ? `${data.usage.event_retention_days} 天` : "-"}
            />
          </div>
        </SectionCard>
      </div>

      <SectionCard
        icon={Database}
        title="数据库"
        description="面板读取的 SQLite 文件；文件大小取自面板进程可见的路径"
        badge={data?.databases.length ?? 0}
      >
        <Table className="min-w-[720px]">
          <TableHeader>
            <TableRow>
              <TableHead>数据库</TableHead>
              <TableHead>路径</TableHead>
              <TableHead>状态</TableHead>
              <TableHead className="text-right">大小</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(data?.databases ?? []).map((database) => (
              <TableRow key={database.key}>
                <TableCell>
                  <div className="font-medium text-slate-800">
                    {DB_LABELS[database.key] || database.key}
                  </div>
                  <div className="font-mono text-[11px] text-slate-400">{database.key}.db</div>
                </TableCell>
                <TableCell className="max-w-[26rem] truncate font-mono text-[11px] text-slate-500" title={database.path}>
                  {database.path}
                </TableCell>
                <TableCell>
                  {database.exists ? (
                    <Badge variant="success">可用</Badge>
                  ) : (
                    <Badge variant="muted">未创建</Badge>
                  )}
                </TableCell>
                <TableCell className="text-right text-xs tabular-nums">
                  {database.exists ? formatBytes(database.size) : "-"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </SectionCard>
    </div>
  );
}
