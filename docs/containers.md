# Docker Compose 安装

根目录的 `compose.yaml` 同时管理 bot、admin、NapCat 和一次性配置初始化器。
不需要宿主机 Python/Node；镜像包含 Python 3.12、Node 22、DSH SDK/runtime、ffmpeg、中文字体与管理前端。
目前验证平台为 Linux amd64（Windows Docker Desktop 的 Linux 引擎同样可用）。NapCat 固定上游多架构镜像 digest，更新需显式替换；QQ 协议兼容性不由本项目保证。NapCat 的 arm64 manifest 虽存在，但整套流程尚未实测；本项目发布镜像仅构建 linux/amd64。镜像和部署包是否已发布，以 [GitHub Releases](https://github.com/asdukw/dsh-qqbot/releases) 的公开版本为准。

## 首次启动

需要 Docker Engine、Compose **2.24.4+**（测试覆盖文件使用 `!reset`）和一个 QQ 小号。
本方式与原有 systemd 安装二选一，避免两个 bot 同时接管同一账号。

1. 推荐下载目标 [Release](https://github.com/asdukw/dsh-qqbot/releases) 的 `qqbot-deploy.tar.gz` 与 `SHA256SUMS` 到同一空目录，校验后解压：

   ```bash
   sha256sum -c SHA256SUMS
   tar -xzf qqbot-deploy.tar.gz
   cd qqbot-deploy
   cp .env.example .env
   ```

   部署包的 `.env.example` 已固定该版本的 `ghcr.io/asdukw/dsh-qqbot:v*` 镜像，不需要 git clone、源码或宿主机 Python/Node。
   编辑 `.env`，填入三个独立随机值：`ADMIN_UI_PASSWORD`、`ONEBOT_TOKEN`、`NAPCAT_WEBUI_TOKEN`。
   `ADMIN_UI_SECRET` 可选；留空时使用管理密码签名 cookie。不要在命令行、日志或版本控制中暴露真实凭据。
   `ADMIN_QQ_IDS` 填管理员 QQ，多个号码用逗号分隔。长期记忆默认开启：聊天样本会发送给配置的模型整理；历史回填默认关闭。
2. 在解压后的 `qqbot-deploy` 目录执行（不要执行 build；部署包不含 Dockerfile）：

   ```bash
   docker compose pull
   docker compose up -d --no-build
   docker compose logs --tail=100 bot admin napcat
   ```

3. 浏览 `http://127.0.0.1:6099`，使用 `NAPCAT_WEBUI_TOKEN` 登录 NapCat WebUI，按提示扫码登录小号。
4. 浏览 `http://127.0.0.1:8080`，使用 `ADMIN_UI_PASSWORD` 登录管理面板。
   在「模型」页添加供应商（兼容协议、API 地址与 key）、添加模型并激活。空注册表期间 bot 等待而非退出；完成后自动启动。
5. 修改持久化配置中的 `onebot.self_id` 为小号 QQ；管理员号码优先使用 `.env` 的 `ADMIN_QQ_IDS`，也可修改 `[bot] admins`。可用容器内 Python 修改，无需在宿主机安装 Python：

   ```bash
   docker compose exec bot python -c 'from pathlib import Path; p=Path("/etc/qqbot/config.toml"); p.write_text(p.read_text().replace("self_id = 0", "self_id = 你的QQ号"))'
   docker compose restart bot
   ```

### 从源码构建（可选）

只有需要修改代码时才 clone 仓库。在仓库根目录复制 `.env.example` 为 `.env`、填写上述配置并保持 `QQBOT_IMAGE=qqbot:local`，然后执行：

```bash
docker compose build bot
docker compose up -d --no-build
docker compose logs --tail=100 bot admin napcat
```

源码安装的后续扫码、面板配置与端口保护方式相同。

只向宿主机 **127.0.0.1** 发布 admin 8080 与 NapCat WebUI 6099；OneBot 3001 不发布。
远程机器通过 SSH 端口转发访问，例如 `ssh -L 8080:127.0.0.1:8080 -L 6099:127.0.0.1:6099 user@host`。
不要直接暴露公网；localhost 页面使用 HTTP，公网访问应另加受信任 HTTPS 入口。

## 持久化与更新

- `qqbot-data`：SQLite（含供应商 key）、DSH home/session、工作区、语音/图片等。
- `qqbot-config`：`config.toml` 与 bootstrap 角色卡。初次并行启动通过原子创建避免覆盖；后续启动不会覆盖已有文件。
- `napcat-qq` / `napcat-config` / `napcat-plugins`：QQ 登录态和 NapCat 配置。
- `video-outbox`：bot `/var/lib/qqbot/video-media/outbox` 可写；NapCat `/app/qqbot-video` 只读；配置的 `container_directory` 与之匹配。

bot/admin 以 UID/GID 999 运行，命名卷首次挂载继承镜像所有权；数据、模型库和配置由容器用户写入。
自行使用 bind mount 时，需先设置目录权限，入口不会以 root 修复任意宿主机目录。
NapCat 为兼容上游保持 root，与 bot 用户分离。

`napcat-init` 仅首次生成 `onebot11.json`；发现已有账号配置时验证启用的 WS 地址、端口和 token，不覆盖已有文件。
改变 `.env` 的 `ONEBOT_TOKEN` 而未同步 NapCat 配置会阻止启动；请在备份后手动协调两端，不要删除登录卷重试。

发布版更新：先备份，阅读目标版本的变更说明并检查部署包的 Compose/配置变更；保留原项目名、卷和私有 `.env`，将 `QQBOT_IMAGE` 改为目标版本，然后执行 `docker compose pull && docker compose up -d --no-build`。
源码版更新：`docker compose build bot && docker compose up -d --no-build`。
镜像启动时初始化缺失的 DSH profile、刷新仓库维护的 profile patch；SDK/runtime 在构建阶段安装，不在聊天时临时下载。
可选 Office/authoring 资源未打包，不属于本 bot 的聊天功能。
停止：`docker compose down` 保留命名卷。**不要执行 `down -v`，除非明确要删除全部账号、模型凭据和历史。**
备份前先 `docker compose stop bot admin napcat`，再归档上述命名卷，避免复制正在写入的 SQLite 文件；恢复需保留所有权与权限。

## 本地隔离验证

```bash
docker build -t qqbot:local .
bash deploy/test-container.sh
```

脚本不会读项目 `.env`，使用独立随机项目名、dummy 凭据和 internal 网络，替换 NapCat 为本地 OneBot/兼容模型模拟器。
覆盖真实 admin HTTP 健康检查、静态页面、401、错误密码、登录、模型设置、OneBot 鉴权、真实 DSH 回复、Node SQLite、非 root 写入、重启持久化和重复初始化。
结束只删除本次生成的测试项目及命名卷，不拉取 NapCat 镜像或连接真实 QQ/模型。
这不能替代真实账号扫码、QQ 消息/视频发送、外部模型服务与 arm64 平台验收。

## 构建下载故障

`PIP_INDEX_URL` 是 Docker build 参数，可按网络选择国内镜像，例如：

```bash
docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bot
```

默认仍用 PyPI；哈希校验失败时保留完整 `--progress=plain` 日志，核对具体文件的 PyPI JSON SHA256，再重试可信来源。
不要关闭哈希校验或把下载到的错误散列当作新基准。Docker/npm 下载受 Docker daemon 的代理配置影响；不要把代理或供应商密钥写进镜像。

## 备份、恢复与回滚

固定 Compose 项目名（例如始终使用 `docker compose -p myqqbot ...`），避免升级时误创建另一组空卷。
停止全部服务后，可用本镜像内的 tar 备份实际卷；先用 `docker volume ls` 确认项目对应名称。以下对一个卷操作，其余卷同样处理：

```bash
# 示例项目名 myqqbot；备份文件落在命名卷内，不要求宿主机安装 tar/Python。
docker volume create myqqbot-backups
docker run --rm --user 0 --entrypoint sh -v myqqbot_qqbot-data:/source:ro -v myqqbot-backups:/backup ghcr.io/asdukw/dsh-qqbot:v0.1.0 -c 'tar -C /source -czpf /backup/qqbot-data.tar.gz .'
```

同样备份 qqbot-config、napcat-qq、napcat-config、napcat-plugins、video-outbox，记录镜像 digest/版本，并单独安全保存 `.env`。上例镜像应替换为实际部署的版本；源码安装可用 `qqbot:local`。
恢复时先停服务，将备份解压到**新建的空卷**（反向挂载后 `tar -xzpf`），保留 uid/gid；不要直接覆盖正在使用的数据库。
本地构建升级前用 `docker image tag qqbot:local qqbot:rollback` 保存旧镜像；发布版在 `.env` 固定版本，不使用 latest。
回滚把 `QQBOT_IMAGE` 改回旧版本并 `docker compose up -d`；如果新版本包含不兼容的数据迁移，必须同时恢复升级前匹配的数据备份。

## 维护者发布

`.github/workflows/container.yml` 在 push/PR 上构建 amd64 并运行隔离 smoke。
真正发布需要维护者先建立 `v*` tag，再在该 tag 上手动运行 workflow 并启用 `publish`；这会推送 GHCR 版本镜像，并创建含部署包与 SHA256SUMS 的 **draft release**。普通 push 不发布。
测试阶段不使用真实 QQ/模型凭据；发布前仍需实际 NapCat 扫码验收。CI 发布本身尚未在线运行验证。

## 许可与第三方组件

本仓库为 MIT，镜像内 `/opt/qqbot/LICENSE` 和部署包内 `LICENSE` 保留本项目声明；这不把整个发行镜像变为 MIT。

- 已检查的 `deepseek-harness-sdk==0.2.1rc2` 与 `deepseek-harness-runtime-bin==0.2.1rc2` wheel 元数据声明 MIT；pip 安装保留各自 `.dist-info/licenses/LICENSE`，runtime 还保留 `THIRD_PARTY_NOTICES.md`。该声明包含适用独立条款的组件列表，不代表列出的每个可选组件都装入本镜像，也不能把上游项目内部的分发决定当作本项目的授权。
- Node 声明位于 `/usr/local/LICENSE`，Python 声明位于 `/usr/local/lib/python3.12/LICENSE.txt`；Python wheel 的随附许可保留在各包安装目录。
- FFmpeg 使用 Debian 二进制包，实际版权/许可和依赖声明位于 `/usr/share/doc/ffmpeg/copyright` 及其他 `/usr/share/doc/*/copyright`；该构建启用 GPL，并非只适用 LGPL。公开再分发前还需落实对应版本源码的提供义务，不能只附本项目 MIT。
- NapCat 通过固定上游镜像引用，并未合并到 qqbot 镜像；其软件、QQ 客户端及相关依赖适用各自条款，引用镜像不赋予额外分发或使用权。

以上是已安装文件检查记录，不是完整许可审计或法律意见。正式发布前仍需复核打包二进制的实际组件、相应条款与源码/声明提供义务。
