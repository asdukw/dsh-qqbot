import { Clock, Database, Server, Settings2, Wifi } from "lucide-react";

import {
  EmptyState,
  InfoList,
  InfoRow,
  Notice,
  PageStack,
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

  const databases = data?.databases ?? [];
  const totalSize = databases.reduce(
    (sum, database) => sum + (database.size ?? 0),
    0
  );
  const transport = data?.transport;
  const qqValue = !data
    ? "-"
    : transport
      ? transport.ready
        ? "在线"
        : "离线"
      : "未检查";

  return (
    <PageStack error={error} loading={loading} hasData={!!data}>
      {transport && !transport.ready ? (
        <Notice tone="danger">
          QQ 消息链路不可用：进程运行不代表账号在线。检查 NapCat 登录状态，必要时重新扫码；单纯重启 bot 不能恢复失效登录。
        </Notice>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={Clock}
          label="运行时长"
          value={data ? formatDuration(data.uptime_seconds) : "-"}
          hint={data ? `启动于 ${formatTs(data.started_at)}` : undefined}
        />
        <StatCard
          icon={Database}
          label="数据库总大小"
          value={data ? formatBytes(totalSize) : "-"}
          hint={`${databases.length} 个数据库`}
        />
        <StatCard
          icon={Server}
          label="监听地址"
          value={data ? `${data.admin.host}:${data.admin.port}` : "-"}
          hint="只绑定本机，通过端口转发访问"
        />
        <StatCard
          icon={Wifi}
          label="QQ 链路"
          value={qqValue}
          tone={transport?.ready ? "emerald" : transport ? "red" : "slate"}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <SectionCard icon={Server} title="管理面板">
          <InfoList>
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
          </InfoList>
        </SectionCard>

        <SectionCard icon={Settings2} title="DSH 与数据保留">
          <InfoList>
            <InfoRow label="profile" value={data?.dsh.profile ?? "-"} />
            <InfoRow label="图片模式" value={data?.dsh.image_mode || "-"} />
            <InfoRow
              label="用量保留"
              value={data ? `${data.usage.retention_days} 天` : "-"}
            />
            <InfoRow
              label="事件保留"
              value={data ? `${data.usage.event_retention_days} 天` : "-"}
            />
          </InfoList>
        </SectionCard>
      </div>

      <SectionCard icon={Database} title="数据库" badge={databases.length}>
        {databases.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>数据库</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">大小</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {databases.map((database) => (
                <TableRow key={database.key}>
                  <TableCell className="font-medium text-slate-800">
                    {DB_LABELS[database.key] || database.key}
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
        ) : (
          <EmptyState icon={Database} text="暂无数据库信息" />
        )}
      </SectionCard>
    </PageStack>
  );
}
