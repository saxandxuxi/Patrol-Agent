# 数据库访问方式调整

> 日期：2026-09-11
> 状态：已实现并验证

## 背景

原设计假设先直连 `10.168.210.78:3307/5436`，直连失败后使用 SSH 本地端口转发。真实联调发现：

1. VPN 挂载后数据库端口仍不可直连；
2. SSH 服务禁用了 TCP 转发，建立隧道时报 `Administratively prohibited`；
3. 服务器上的数据库都运行在 Docker 容器中，容器内客户端可用。

## 调整

数据库访问改为 SSH 命令执行：

```text
MySQL:
docker exec -e MYSQL_PWD=... mysql mysql -h127.0.0.1 -P3307 -u... -D ... --batch --raw -e "<SQL>"

PostgreSQL:
docker exec -e PGPASSWORD=... postgres psql -h127.0.0.1 -p5436 -U ... -d gpsdb --csv -c "<SQL>"
```

SeaweedFS 也改为在服务器上执行 `curl http://127.0.0.1:28888/<path>`，图片再由后端转发给前端。

## 验证结果

- MySQL `SELECT 1`：通过；
- PostgreSQL `SELECT 1`：通过；
- MySQL `ai_problem` 正常数据数：1702；
- PostgreSQL `gps_points`：0（当前空库，按未来有数据设计）；
- SeaweedFS Filer HTTP：200；
- RAGFlow 健康状态：ok；
- Qwen 连接测试：成功；
- HTTP `/api/chat`：成功返回分析结果；
- CSV 下载：成功；
- 前端首页：成功返回。
